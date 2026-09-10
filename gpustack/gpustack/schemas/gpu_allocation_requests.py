"""GPU allocation request workflow — user isolation, feature one.

A non-admin user files a :class:`GpuAllocationRequest` asking for a
quantity of GPUs. Platform admins review it; on approval the request
carries a **quota grant** (``granted_gpu_count``) that downstream
scheduling enforces per-principal, and — optionally — the admin
designates specific workers that get opened to the requesting user via
:class:`gpustack.schemas.worker_access.WorkerAccess`.

Lifecycle::

    PENDING -> APPROVED (granted_gpu_count > 0)
            -> REJECTED (admin_comment)
    PENDING -> CANCELLED (by the requester)

State machine is enforced in the route layer, not the DB, so partial
updates stay a single code path.
"""

from datetime import datetime
from enum import Enum
from typing import ClassVar, List, Optional

from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, SQLModel

from gpustack.mixins import BaseModelMixin
from gpustack.schemas.common import ListParams, PaginatedList


class GpuRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class GpuAllocationRequestBase(SQLModel):
    # Free-text purpose, shown to the admin in the review UI / API.
    reason: Optional[str] = Field(default=None, max_length=2000)
    requested_gpu_count: int = Field(ge=1, le=1024)
    # Optional model name the user intends to serve — helps the admin
    # size the grant.
    model_name: Optional[str] = Field(default=None, max_length=255)


class GpuAllocationRequest(
    GpuAllocationRequestBase, BaseModelMixin, table=True
):
    """A user's ask for GPUs, and the admin's decision on it."""

    __tablename__ = 'gpu_allocation_requests'
    # NOTE(二开): no DB-level natural-key uniqueness — the "one live
    # PENDING request per user" rule is enforced in the route layer
    # (create_gpu_request), which keeps the audit trail intact: a user
    # may re-request the same count/reason after a previous request was
    # approved / rejected / cancelled.

    id: Optional[int] = Field(default=None, primary_key=True)
    # The requesting user's USER-principal id (== principals.id).
    user_principal_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    status: str = Field(
        default=GpuRequestStatus.PENDING.value, index=True, max_length=16
    )
    # Filled at approval time; the per-user quota the scheduler honours.
    granted_gpu_count: Optional[int] = Field(default=None)
    granted_by: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("principals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    admin_comment: Optional[str] = Field(default=None, max_length=2000)
    decided_at: Optional[datetime] = Field(default=None)


class GpuAllocationRequestCreate(GpuAllocationRequestBase):
    pass


class GpuAllocationRequestDecision(SQLModel):
    """Admin decision payload for POST /gpu-requests/{id}/approve|reject."""
    granted_gpu_count: Optional[int] = Field(default=None, ge=0, le=1024)
    admin_comment: Optional[str] = Field(default=None, max_length=2000)


class GpuAllocationRequestUpdate(SQLModel):
    reason: Optional[str] = Field(default=None, max_length=2000)
    model_name: Optional[str] = Field(default=None, max_length=255)


class GpuAllocationRequestPublic(GpuAllocationRequestBase):
    id: int
    user_principal_id: int
    # Resolved server-side for list rendering (user name of requester).
    username: Optional[str] = None
    status: str
    granted_gpu_count: Optional[int] = None
    granted_by: Optional[int] = None
    granted_by_name: Optional[str] = None
    admin_comment: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class GpuAllocationRequestListParams(ListParams):
    sortable_fields: ClassVar[List[str]] = [
        "status",
        "requested_gpu_count",
        "granted_gpu_count",
        "created_at",
        "updated_at",
    ]


GpuAllocationRequestsPublic = PaginatedList[GpuAllocationRequestPublic]


async def get_user_gpu_quota(session, user_principal_id: int) -> Optional[int]:
    """The user's currently-effective GPU quota.

    Effective quota = the ``granted_gpu_count`` of the user's most
    recent APPROVED request (None means "no quota on file" — for
    non-admin users the scheduler treats that as 0 allocatable GPUs
    unless the deployment runs in open mode).
    """
    from sqlmodel import select

    stmt = (
        select(GpuAllocationRequest)
        .where(
            GpuAllocationRequest.user_principal_id == user_principal_id,
            GpuAllocationRequest.status == GpuRequestStatus.APPROVED.value,
            GpuAllocationRequest.deleted_at.is_(None),
        )
        .order_by(GpuAllocationRequest.decided_at.desc())
        .limit(1)
    )
    row = (await session.exec(stmt)).first()
    if row is None or row.granted_gpu_count is None:
        return None
    return row.granted_gpu_count
