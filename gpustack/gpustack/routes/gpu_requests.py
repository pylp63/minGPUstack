"""GPU allocation request workflow (user isolation, feature one).

Endpoints::

    POST   /v1/gpu-requests                user files a request
    GET    /v1/gpu-requests                own requests; admin sees all
    GET    /v1/gpu-requests/{id}           detail (owner or admin)
    PUT    /v1/gpu-requests/{id}           requester edits a PENDING row
    DELETE /v1/gpu-requests/{id}           requester cancels (soft)
    POST   /v1/gpu-requests/{id}/approve   admin approves (+ quota)
    POST   /v1/gpu-requests/{id}/reject    admin rejects
    GET    /v1/gpu-requests/quota/me       caller's effective quota

Filing a request fans a notification out to every platform admin; the
decision notifies the requester. Approval records ``granted_gpu_count``
(the quota the scheduler enforces) and — via the separate
``/workers/{id}/access`` API — the admin can also open specific nodes
to the user.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from sqlmodel import func, select

from gpustack.api.exceptions import (
    AlreadyExistsException,
    InvalidException,
    NotFoundException,
)
from gpustack.schemas.common import Pagination
from gpustack.schemas.gpu_allocation_requests import (
    GpuAllocationRequest,
    GpuAllocationRequestCreate,
    GpuAllocationRequestDecision,
    GpuAllocationRequestListParams,
    GpuAllocationRequestPublic,
    GpuAllocationRequestsPublic,
    GpuRequestStatus,
    get_user_gpu_quota,
)
from gpustack.schemas.notifications import NotificationKind, notify, notify_admins
from gpustack.schemas.principals import Principal
from gpustack.server.deps import SessionDep, TenantContextDep

logger = logging.getLogger(__name__)

router = APIRouter()

_STATUS_VALUES = {s.value for s in GpuRequestStatus}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _to_public(session, row: GpuAllocationRequest) -> GpuAllocationRequestPublic:
    requester = await Principal.one_by_id(session, row.user_principal_id)
    approver = (
        await Principal.one_by_id(session, row.granted_by)
        if row.granted_by
        else None
    )
    pub = GpuAllocationRequestPublic(
        id=row.id,
        user_principal_id=row.user_principal_id,
        username=requester.name if requester else None,
        reason=row.reason,
        requested_gpu_count=row.requested_gpu_count,
        model_name=row.model_name,
        status=row.status,
        granted_gpu_count=row.granted_gpu_count,
        granted_by=row.granted_by,
        granted_by_name=approver.name if approver else None,
        admin_comment=row.admin_comment,
        decided_at=row.decided_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    return pub


def _is_admin(ctx) -> bool:
    return bool(ctx.is_platform_admin)


@router.get("", response_model=GpuAllocationRequestsPublic)
async def list_gpu_requests(
    session: SessionDep,
    ctx: TenantContextDep,
    params: GpuAllocationRequestListParams = Depends(),
    status: Optional[str] = None,
):
    """List requests. Non-admin callers only see their own rows."""
    stmt = select(GpuAllocationRequest).where(GpuAllocationRequest.deleted_at.is_(None))
    if not _is_admin(ctx):
        stmt = stmt.where(GpuAllocationRequest.user_principal_id == ctx.user.id)
    if status:
        if status not in _STATUS_VALUES:
            raise InvalidException(message=f"Invalid status filter: {status}")
        stmt = stmt.where(GpuAllocationRequest.status == status)

    # Sorting (whitelisted via ListParams.sortable_fields)
    order_by = params.order_by
    if order_by:
        for field, direction in order_by:
            col = getattr(GpuAllocationRequest, field, None)
            if col is None:
                continue
            stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    else:
        stmt = stmt.order_by(GpuAllocationRequest.id.desc())

    # Count for pagination (same visibility scope as the page query).
    count_stmt = (
        select(func.count())
        .select_from(GpuAllocationRequest)
        .where(GpuAllocationRequest.deleted_at.is_(None))
    )
    if not _is_admin(ctx):
        count_stmt = count_stmt.where(
            GpuAllocationRequest.user_principal_id == ctx.user.id
        )
    if status:
        count_stmt = count_stmt.where(GpuAllocationRequest.status == status)
    total = (await session.exec(count_stmt)).one()

    rows = (
        await session.exec(
            stmt.offset((params.page - 1) * params.perPage).limit(params.perPage)
        )
    ).all()

    items = [await _to_public(session, r) for r in rows]
    total_page = (
        (total + params.perPage - 1) // params.perPage if params.perPage else 0
    )
    return GpuAllocationRequestsPublic(
        items=items,
        pagination=Pagination(
            page=params.page,
            perPage=params.perPage,
            total=total,
            totalPage=total_page,
        ),
    )


@router.post("", response_model=GpuAllocationRequestPublic)
async def create_gpu_request(
    session: SessionDep,
    ctx: TenantContextDep,
    req_in: GpuAllocationRequestCreate,
):
    user = ctx.user
    if user.is_admin:
        raise InvalidException(
            message="Admins do not need to request GPUs; quota applies to regular users."
        )

    # One live PENDING request per user.
    existing_stmt = select(GpuAllocationRequest).where(
        GpuAllocationRequest.user_principal_id == user.id,
        GpuAllocationRequest.status == GpuRequestStatus.PENDING.value,
        GpuAllocationRequest.deleted_at.is_(None),
    )
    if (await session.exec(existing_stmt)).first() is not None:
        raise AlreadyExistsException(
            message="You already have a pending GPU request."
        )

    row = GpuAllocationRequest(
        user_principal_id=user.id,
        requested_gpu_count=req_in.requested_gpu_count,
        reason=req_in.reason,
        model_name=req_in.model_name,
        status=GpuRequestStatus.PENDING.value,
    )
    row = await GpuAllocationRequest.create(session, row)

    # Notify every platform admin.
    await notify_admins(
        session,
        NotificationKind.GPU_REQUEST_SUBMITTED,
        f"New GPU request from {user.name}",
        body=(
            f"User {user.name} requested {req_in.requested_gpu_count} GPU(s)"
            + (f" for model '{req_in.model_name}'" if req_in.model_name else "")
            + (f": {req_in.reason}" if req_in.reason else "")
        ),
        source_id=row.id,
    )

    return await _to_public(session, row)


async def _load_own_or_admin(session, ctx, request_id: int) -> GpuAllocationRequest:
    row = await GpuAllocationRequest.one_by_id(session, request_id)
    if row is None or row.deleted_at is not None:
        raise NotFoundException(message="GPU request not found")
    if not _is_admin(ctx) and row.user_principal_id != ctx.user.id:
        raise NotFoundException(message="GPU request not found")
    return row


@router.get("/quota/me")
async def get_my_quota(session: SessionDep, ctx: TenantContextDep):
    if ctx.user.is_admin:
        return {"quota": None, "is_admin": True}
    quota = await get_user_gpu_quota(session, ctx.user.id)
    return {"quota": quota, "is_admin": False}


@router.get("/{id}", response_model=GpuAllocationRequestPublic)
async def get_gpu_request(session: SessionDep, ctx: TenantContextDep, id: int):
    row = await _load_own_or_admin(session, ctx, id)
    return await _to_public(session, row)


@router.put("/{id}", response_model=GpuAllocationRequestPublic)
async def update_gpu_request(
    session: SessionDep,
    ctx: TenantContextDep,
    id: int,
    req_in: GpuAllocationRequestCreate,
):
    row = await _load_own_or_admin(session, ctx, id)
    if row.user_principal_id != ctx.user.id:
        raise NotFoundException(message="GPU request not found")
    if row.status != GpuRequestStatus.PENDING.value:
        raise InvalidException(message="Only pending requests can be edited.")
    row.reason = req_in.reason
    row.model_name = req_in.model_name
    row.requested_gpu_count = req_in.requested_gpu_count
    await row.update(session)
    return await _to_public(session, row)


@router.delete("/{id}", response_model=GpuAllocationRequestPublic)
async def cancel_gpu_request(session: SessionDep, ctx: TenantContextDep, id: int):
    row = await _load_own_or_admin(session, ctx, id)
    if not _is_admin(ctx) and row.user_principal_id != ctx.user.id:
        raise NotFoundException(message="GPU request not found")
    if row.status != GpuRequestStatus.PENDING.value:
        raise InvalidException(message="Only pending requests can be cancelled.")
    row.status = GpuRequestStatus.CANCELLED.value
    row.decided_at = _utcnow()
    await row.update(session)
    await notify_admins(
        session,
        NotificationKind.GPU_REQUEST_CANCELLED,
        f"GPU request #{row.id} cancelled",
        source_id=row.id,
    )
    return await _to_public(session, row)


@router.post("/{id}/approve", response_model=GpuAllocationRequestPublic)
async def approve_gpu_request(
    session: SessionDep,
    ctx: TenantContextDep,
    id: int,
    decision: Optional[GpuAllocationRequestDecision] = None,
):
    if not _is_admin(ctx):
        raise InvalidException(message="Only admins can approve GPU requests.")
    row = await GpuAllocationRequest.one_by_id(session, id)
    if row is None or row.deleted_at is not None:
        raise NotFoundException(message="GPU request not found")
    if row.status != GpuRequestStatus.PENDING.value:
        raise InvalidException(
            message=f"Request is {row.status}, only pending requests can be approved."
        )

    granted = (
        decision.granted_gpu_count
        if decision and decision.granted_gpu_count is not None
        else row.requested_gpu_count
    )
    if granted is not None and granted < 0:
        raise InvalidException(message="granted_gpu_count must be >= 0")

    row.status = GpuRequestStatus.APPROVED.value
    row.granted_gpu_count = granted
    row.granted_by = ctx.user.id
    row.admin_comment = decision.admin_comment if decision else row.admin_comment
    row.decided_at = _utcnow()
    await row.update(session)

    requester = await Principal.one_by_id(session, row.user_principal_id)
    await notify(
        session,
        [row.user_principal_id],
        NotificationKind.GPU_REQUEST_APPROVED,
        f"GPU request approved: {granted} GPU(s)",
        body=(
            f"Your request for {row.requested_gpu_count} GPU(s) was approved "
            f"with a quota of {granted} GPU(s)."
            + (f" Admin note: {row.admin_comment}" if row.admin_comment else "")
        ),
        source_id=row.id,
    )
    logger.info(
        f"GPU request {row.id} approved for user {requester.name if requester else row.user_principal_id}: "
        f"quota={granted}"
    )
    return await _to_public(session, row)


@router.post("/{id}/reject", response_model=GpuAllocationRequestPublic)
async def reject_gpu_request(
    session: SessionDep,
    ctx: TenantContextDep,
    id: int,
    decision: Optional[GpuAllocationRequestDecision] = None,
):
    if not _is_admin(ctx):
        raise InvalidException(message="Only admins can reject GPU requests.")
    row = await GpuAllocationRequest.one_by_id(session, id)
    if row is None or row.deleted_at is not None:
        raise NotFoundException(message="GPU request not found")
    if row.status != GpuRequestStatus.PENDING.value:
        raise InvalidException(
            message=f"Request is {row.status}, only pending requests can be rejected."
        )

    row.status = GpuRequestStatus.REJECTED.value
    row.granted_gpu_count = None
    row.granted_by = ctx.user.id
    row.admin_comment = decision.admin_comment if decision else None
    row.decided_at = _utcnow()
    await row.update(session)

    await notify(
        session,
        [row.user_principal_id],
        NotificationKind.GPU_REQUEST_REJECTED,
        "GPU request rejected",
        body=(
            "Your GPU request was rejected."
            + (f" Admin note: {row.admin_comment}" if row.admin_comment else "")
        ),
        source_id=row.id,
    )
    return await _to_public(session, row)
