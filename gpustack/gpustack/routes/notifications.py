"""In-app notification endpoints (user isolation, feature one).

Endpoints::

    GET    /v1/notifications          caller's notifications
    GET    /v1/notifications/unread    unread count (badge)
    POST   /v1/notifications/read-all  mark all read
    PUT    /v1/notifications/{id}/read mark one read

Every caller sees only rows addressed to their own principal.
"""

from fastapi import APIRouter, Depends
from sqlmodel import func, select

from gpustack.api.exceptions import NotFoundException
from gpustack.schemas.common import Pagination
from gpustack.schemas.notifications import (
    Notification,
    NotificationListParams,
    NotificationPublic,
    NotificationsPublic,
)
from gpustack.server.deps import SessionDep, TenantContextDep

router = APIRouter()


def _scope(stmt, ctx):
    return stmt.where(
        Notification.recipient_principal_id == ctx.user.id,
        Notification.deleted_at.is_(None),
    )


@router.get("", response_model=NotificationsPublic)
async def list_notifications(
    session: SessionDep,
    ctx: TenantContextDep,
    params: NotificationListParams = Depends(),
):
    stmt = _scope(select(Notification), ctx)
    order_by = params.order_by
    if order_by:
        for field, direction in order_by:
            col = getattr(Notification, field, None)
            if col is None:
                continue
            stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    else:
        stmt = stmt.order_by(Notification.id.desc())

    count_stmt = _scope(
        select(func.count()).select_from(Notification), ctx
    )
    total = (await session.exec(count_stmt)).one()

    rows = (
        await session.exec(
            stmt.offset((params.page - 1) * params.perPage).limit(params.perPage)
        )
    ).all()
    items = [
        NotificationPublic(
            id=n.id,
            recipient_principal_id=n.recipient_principal_id,
            kind=n.kind,
            title=n.title,
            body=n.body,
            source_id=n.source_id,
            read=n.read,
            created_at=n.created_at,
        )
        for n in rows
    ]
    total_page = (
        (total + params.perPage - 1) // params.perPage if params.perPage else 0
    )
    return NotificationsPublic(
        items=items,
        pagination=Pagination(
            page=params.page,
            perPage=params.perPage,
            total=total,
            totalPage=total_page,
        ),
    )


@router.get("/unread")
async def unread_count(session: SessionDep, ctx: TenantContextDep):
    stmt = _scope(
        select(func.count())
        .select_from(Notification)
        .where(Notification.read == False),  # noqa: E712
        ctx,
    )
    count = (await session.exec(stmt)).one()
    return {"unread": count}


@router.post("/read-all")
async def mark_all_read(session: SessionDep, ctx: TenantContextDep):
    stmt = _scope(
        select(Notification).where(Notification.read == False),  # noqa: E712
        ctx,
    )
    rows = (await session.exec(stmt)).all()
    for n in rows:
        n.read = True
        await n.update(session, auto_commit=False)
    await session.commit()
    return {"updated": len(rows)}


@router.put("/{id}/read")
async def mark_read(session: SessionDep, ctx: TenantContextDep, id: int):
    n = await Notification.one_by_id(session, id)
    if n is None or n.recipient_principal_id != ctx.user.id:
        raise NotFoundException(message="Notification not found")
    if not n.read:
        n.read = True
        await n.update(session)
    return {"id": n.id, "read": True}
