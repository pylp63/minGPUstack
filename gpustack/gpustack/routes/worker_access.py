"""Worker-level access grants (user isolation, feature one).

Admin designates which specific nodes (workers) are opened to which
principal. Combined with the cluster grant this yields "the user sees
exactly the machines the admin chose".

Endpoints (nested under a worker, mirroring cluster_access)::

    GET    /v1/workers/{worker_id}/access              list grants
    POST   /v1/workers/{worker_id}/access              grant (admin)
    DELETE /v1/workers/{worker_id}/access/{principal_id} revoke (admin)
    GET    /v1/workers-access/me                       my granted workers

Permission gates mirror cluster_access: platform admin, or the owner of
the worker's cluster.
"""

from typing import List

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import select

from gpustack.api.exceptions import (
    AlreadyExistsException,
    InvalidException,
    NotFoundException,
)
from gpustack.api.tenant import assert_cluster_visible, assert_cluster_writable
from gpustack.schemas.notifications import NotificationKind, notify
from gpustack.schemas.principals import Principal, PrincipalType
from gpustack.schemas.worker_access import (
    WorkerAccess,
    WorkerAccessPublic,
)
from gpustack.schemas.workers import Worker
from gpustack.server.deps import SessionDep, TenantContextDep

router = APIRouter()


class WorkerAccessGrant(BaseModel):
    principal_id: int


async def _load_worker(session, ctx, worker_id: int) -> Worker:
    worker = await Worker.one_by_id(session, worker_id)
    if worker is None:
        raise NotFoundException(message="Worker not found")
    # Visibility rides the worker's cluster.
    if worker.cluster_id is not None:
        from gpustack.schemas.clusters import Cluster

        cluster = await Cluster.one_by_id(session, worker.cluster_id)
        if cluster is not None:
            assert_cluster_visible(
                ctx, cluster, not_found_message="Worker not found"
            )
    return worker


async def _resolve_rows(session, rows: List[WorkerAccess]) -> List[WorkerAccessPublic]:
    principal_ids = {r.principal_id for r in rows} | {
        r.granted_by for r in rows if r.granted_by
    }
    worker_ids = {r.worker_id for r in rows}
    pmap: dict[int, Principal] = {}
    wmap: dict[int, Worker] = {}
    if principal_ids:
        pmap = {
            p.id: p
            for p in (
                await session.exec(
                    select(Principal).where(Principal.id.in_(principal_ids))
                )
            ).all()
        }
    if worker_ids:
        wmap = {
            w.id: w
            for w in (
                await session.exec(select(Worker).where(Worker.id.in_(worker_ids)))
            ).all()
        }
    out = []
    for r in rows:
        p = pmap.get(r.principal_id)
        w = wmap.get(r.worker_id)
        out.append(
            WorkerAccessPublic(
                id=r.id,
                worker_id=r.worker_id,
                worker_name=w.name if w else None,
                principal_id=r.principal_id,
                principal_name=p.name if p else None,
                principal_display_name=p.display_name if p else None,
                granted_by=r.granted_by,
                note=r.note,
                created_at=r.created_at,
            )
        )
    return out


@router.get("/workers/{worker_id}/access", response_model=List[WorkerAccessPublic])
async def list_worker_access(
    session: SessionDep, ctx: TenantContextDep, worker_id: int
):
    await _load_worker(session, ctx, worker_id)
    rows = (
        await session.exec(
            select(WorkerAccess).where(
                WorkerAccess.worker_id == worker_id,
                WorkerAccess.deleted_at.is_(None),
            )
        )
    ).all()
    return await _resolve_rows(session, list(rows))


@router.post("/workers/{worker_id}/access", response_model=WorkerAccessPublic)
async def grant_worker_access(
    session: SessionDep,
    ctx: TenantContextDep,
    worker_id: int,
    body: WorkerAccessGrant,
):
    worker = await _load_worker(session, ctx, worker_id)

    # Admin (or cluster owner) gates who gets onto this machine.
    if worker.cluster_id is not None:
        from gpustack.schemas.clusters import Cluster

        cluster = await Cluster.one_by_id(session, worker.cluster_id)
        if cluster is not None:
            assert_cluster_writable(ctx, cluster)
    if not ctx.is_platform_admin:
        raise InvalidException(message="Only platform admins can grant worker access")

    target = await Principal.one_by_id(session, body.principal_id)
    if not target or target.deleted_at is not None:
        raise InvalidException(message=f"Principal {body.principal_id} not found")
    if target.kind == PrincipalType.SYSTEM:
        raise InvalidException(message="Cannot grant worker access to a system principal")

    existing = (
        await session.exec(
            select(WorkerAccess).where(
                WorkerAccess.worker_id == worker_id,
                WorkerAccess.principal_id == body.principal_id,
                WorkerAccess.deleted_at.is_(None),
            )
        )
    ).first()
    if existing is not None:
        raise AlreadyExistsException(message="Access already granted")

    grant = await WorkerAccess.create(
        session,
        WorkerAccess(
            worker_id=worker_id,
            principal_id=body.principal_id,
            granted_by=ctx.user.id,
        ),
    )
    await notify(
        session,
        [body.principal_id],
        NotificationKind.WORKER_ACCESS_GRANTED,
        f"Node '{worker.name}' is now available to you",
        body=f"An administrator opened worker '{worker.name}' for your use.",
        source_id=worker_id,
    )
    enriched = await _resolve_rows(session, [grant])
    return enriched[0]


@router.delete("/workers/{worker_id}/access/{principal_id}")
async def revoke_worker_access(
    session: SessionDep, ctx: TenantContextDep, worker_id: int, principal_id: int
):
    worker = await _load_worker(session, ctx, worker_id)
    if not ctx.is_platform_admin:
        raise InvalidException(message="Only platform admins can revoke worker access")
    row = (
        await session.exec(
            select(WorkerAccess).where(
                WorkerAccess.worker_id == worker_id,
                WorkerAccess.principal_id == principal_id,
                WorkerAccess.deleted_at.is_(None),
            )
        )
    ).first()
    if not row:
        raise NotFoundException(message="Access grant not found")
    await row.delete(session)
    return {"revoked": True, "worker_id": worker_id, "principal_id": principal_id}


@router.get("/workers-access/me", response_model=List[WorkerAccessPublic])
async def my_worker_access(session: SessionDep, ctx: TenantContextDep):
    """The caller's own grants — powers "my machines" in the UI."""
    rows = (
        await session.exec(
            select(WorkerAccess).where(
                WorkerAccess.principal_id == ctx.user.id,
                WorkerAccess.deleted_at.is_(None),
            )
        )
    ).all()
    return await _resolve_rows(session, list(rows))
