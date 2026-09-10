"""In-app notifications for GPUStack principals.

Backs the admin-review flow of GPU allocation requests (feature one):
when a user files / updates a request, every platform admin gets a
notification row; decisions notify the requester. Generic enough to
carry other future event types.
"""

from datetime import datetime
from enum import Enum
from typing import ClassVar, List, Optional

from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlmodel import Field, SQLModel

from gpustack.mixins import BaseModelMixin
from gpustack.schemas.common import ListParams, PaginatedList


class NotificationKind(str, Enum):
    GPU_REQUEST_SUBMITTED = "gpu_request_submitted"
    GPU_REQUEST_APPROVED = "gpu_request_approved"
    GPU_REQUEST_REJECTED = "gpu_request_rejected"
    GPU_REQUEST_CANCELLED = "gpu_request_cancelled"
    WORKER_ACCESS_GRANTED = "worker_access_granted"
    INFO = "info"


class Notification(BaseModelMixin, SQLModel, table=True):
    """A single notification addressed to one principal."""

    __tablename__ = 'notifications'

    id: Optional[int] = Field(default=None, primary_key=True)
    # Recipient principal id (USER for the requester; every admin gets
    # their own row on submission).
    recipient_principal_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    kind: str = Field(default=NotificationKind.INFO.value, max_length=48)
    title: str = Field(max_length=255)
    body: Optional[str] = Field(default=None, sa_column=Column(Text))
    # Polymorphic link back to the source row (e.g. gpu_allocation_requests.id).
    source_id: Optional[int] = Field(default=None, index=True)
    read: bool = Field(default=False, index=True)


class NotificationPublic(SQLModel):
    id: int
    recipient_principal_id: int
    kind: str
    title: str
    body: Optional[str] = None
    source_id: Optional[int] = None
    read: bool
    created_at: datetime


class NotificationListParams(ListParams):
    sortable_fields: ClassVar[List[str]] = [
        "kind",
        "read",
        "created_at",
    ]


NotificationsPublic = PaginatedList[NotificationPublic]


async def notify(
    session,
    recipient_principal_ids: List[int],
    kind: NotificationKind,
    title: str,
    body: Optional[str] = None,
    source_id: Optional[int] = None,
    auto_commit: bool = True,
):
    """Create one notification per recipient principal."""
    from gpustack.schemas.principals import Principal

    seen = set()
    for pid in recipient_principal_ids:
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        n = Notification(
            recipient_principal_id=pid,
            kind=kind.value,
            title=title,
            body=body,
            source_id=source_id,
        )
        await Notification.create(session, n, auto_commit=auto_commit)


async def notify_admins(
    session,
    kind: NotificationKind,
    title: str,
    body: Optional[str] = None,
    source_id: Optional[int] = None,
    auto_commit: bool = True,
):
    """Fan out a notification to every active platform admin."""
    from gpustack.schemas.principals import Principal, PrincipalType

    from sqlmodel import select

    stmt = select(Principal.id).where(
        Principal.is_admin == True,  # noqa: E712
        Principal.kind == PrincipalType.USER,
        Principal.is_active == True,  # noqa: E712
        Principal.deleted_at.is_(None),
    )
    admin_ids = list((await session.exec(stmt)).all())
    if not admin_ids:
        return
    await notify(
        session,
        admin_ids,
        kind,
        title,
        body=body,
        source_id=source_id,
        auto_commit=auto_commit,
    )
