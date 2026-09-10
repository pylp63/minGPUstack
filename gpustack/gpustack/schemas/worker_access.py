"""Worker-level access grants — admin designates which GPU nodes a
non-admin user may see and schedule onto.

Complements the coarse :class:`ClusterAccess` grant: a user can be
granted a *cluster* (e.g. the default cluster) yet still only see the
specific *workers* the admin opened to them. Worker visibility and
schedulability both consult this table:

- ``routes/workers.py`` narrows the worker list to
  ``granted workers ∪ admin-visible`` workers.
- the scheduler's :class:`WorkerAccessFilter` removes non-granted
  workers before candidate selection, so a user's model instances can
  never land on a machine the admin did not open.
"""

from datetime import datetime
from typing import ClassVar, List, Optional, Set

from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, SQLModel

from gpustack.mixins import BaseModelMixin
from gpustack.schemas.common import ListParams, PaginatedList


class WorkerAccess(BaseModelMixin, SQLModel, table=True):
    """Grant a single principal access to a single worker (node)."""

    __tablename__ = 'worker_access'
    __table_args__ = (
        UniqueConstraint(
            'worker_id', 'principal_id', name='uix_worker_access_worker_principal'
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    worker_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("workers.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    principal_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    granted_by: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("principals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Free-form note the admin can attach (e.g. "granted for 2 weeks").
    note: Optional[str] = Field(default=None, max_length=512)


class WorkerAccessPublic(SQLModel):
    id: int
    worker_id: int
    worker_name: Optional[str] = None
    principal_id: int
    principal_name: Optional[str] = None
    principal_display_name: Optional[str] = None
    granted_by: Optional[int] = None
    note: Optional[str] = None
    created_at: datetime


class WorkerAccessListParams(ListParams):
    sortable_fields: ClassVar[List[str]] = [
        "worker_id",
        "principal_id",
        "created_at",
    ]


WorkerAccessPublicList = PaginatedList[WorkerAccessPublic]


async def granted_worker_ids_for(session, principal_id: int) -> List[int]:
    """Worker ids explicitly granted to ``principal_id`` (active rows)."""
    from sqlmodel import select

    stmt = select(WorkerAccess.worker_id).where(
        WorkerAccess.principal_id == principal_id,
        WorkerAccess.deleted_at.is_(None),
    )
    return list((await session.exec(stmt)).all())


async def granted_worker_ids_for_principals(
    session, principal_ids
) -> List[int]:
    """Worker ids granted to ANY of ``principal_ids`` (union, deduped).

    二开: 用户资源隔离要覆盖「用户本人 + 其所属用户组」两条授权路径 —
    admin 既可直接授权某个用户, 也可授权整个用户组, 组内用户都能用.
    """
    if not principal_ids:
        return []
    from sqlmodel import select

    stmt = select(WorkerAccess.worker_id).where(
        WorkerAccess.principal_id.in_(list(principal_ids)),
        WorkerAccess.deleted_at.is_(None),
    )
    return list(dict.fromkeys((await session.exec(stmt)).all()))


async def user_or_group_worker_ids(session, user_id: int) -> List[int]:
    """Worker ids a user can use: their own grants ∪ their groups' grants.

    Resolves the user's GROUP principals (via principals + membership),
    then unions the worker-access grants across the user and every
    group they belong to.
    """
    from sqlmodel import select

    from gpustack.schemas.principals import Principal, PrincipalMembership, PrincipalType

    principal_ids = {user_id}
    rows = await session.exec(
        select(PrincipalMembership.parent_principal_id).where(
            PrincipalMembership.member_principal_id == user_id,
            PrincipalMembership.deleted_at.is_(None),
        )
    )
    group_ids = list(rows.all())
    if group_ids:
        # 只保留 GROUP 类型的主体 (membership.parent 也可能是 ORG)
        grp_rows = await session.exec(
            select(Principal.id).where(
                Principal.id.in_(group_ids),
                Principal.kind == PrincipalType.GROUP,
                Principal.deleted_at.is_(None),
            )
        )
        principal_ids.update(grp_rows.all())

    return set(await granted_worker_ids_for_principals(session, principal_ids))


from typing import Set  # noqa: E402
async def filter_workers_by_access(
    session, principal_id: int, workers: List
) -> List:
    """Keep only workers this principal holds a grant for.

    Callers pass the full worker list; the returned list preserves
    order. Grants are all-or-nothing per worker (no per-GPU grants),
    which matches the admin mental model of "open these machines".
    """
    allowed = set(await granted_worker_ids_for(session, principal_id))
    return [w for w in workers if w.id in allowed]
