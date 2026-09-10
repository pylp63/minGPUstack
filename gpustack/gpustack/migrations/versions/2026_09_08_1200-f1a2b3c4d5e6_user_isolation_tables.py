"""gpu allocation requests, worker access, notifications

Feature: user isolation for GPUStack.

* ``gpu_allocation_requests`` — a non-admin user's ask for GPUs and the
  admin's decision. APPROVED rows carry ``granted_gpu_count``, the
  per-user quota the scheduler honours.
* ``worker_access`` — admin designates specific workers (nodes) opened
  to a principal. Worker list visibility and scheduler candidate
  selection both consult this table.
* ``notifications`` — in-app notifications; backs the admin review
  flow (submission fans out to admins, decision notifies requester).

All three tables soft-delete via the shared ``deleted_at`` pattern.

Revision ID: f1a2b3c4d5e6
Revises: e4a1c8b7d0f3
Create Date: 2026-09-08 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

from gpustack.schemas.common import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e4a1c8b7d0f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _baseMixin_columns() -> list:
    # Mirror gpustack.mixins.BaseModelMixin column layout.
    return [
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('updated_at', UTCDateTime(), nullable=False),
        sa.Column('deleted_at', UTCDateTime(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        'gpu_allocation_requests',
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=True),
        sa.Column(
            'user_principal_id',
            sa.Integer(),
            sa.ForeignKey('principals.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=True),
        sa.Column('requested_gpu_count', sa.Integer(), nullable=False),
        sa.Column('model_name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('granted_gpu_count', sa.Integer(), nullable=True),
        sa.Column(
            'granted_by',
            sa.Integer(),
            sa.ForeignKey('principals.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('admin_comment', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=True),
        sa.Column('decided_at', UTCDateTime(), nullable=True),
        *_baseMixin_columns(),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_gpu_allocation_requests_status',
        'gpu_allocation_requests',
        ['status'],
    )

    op.create_table(
        'worker_access',
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=True),
        sa.Column(
            'worker_id',
            sa.Integer(),
            sa.ForeignKey('workers.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'principal_id',
            sa.Integer(),
            sa.ForeignKey('principals.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'granted_by',
            sa.Integer(),
            sa.ForeignKey('principals.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('note', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=True),
        *_baseMixin_columns(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'worker_id',
            'principal_id',
            name='uix_worker_access_worker_principal',
        ),
    )

    op.create_table(
        'notifications',
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=True),
        sa.Column(
            'recipient_principal_id',
            sa.Integer(),
            sa.ForeignKey('principals.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('kind', sa.String(length=48), nullable=False),
        sa.Column('title', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('read', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('body', sa.Text(), nullable=True),
        *_baseMixin_columns(),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_notifications_recipient_principal_id',
        'notifications',
        ['recipient_principal_id'],
    )
    op.create_index('ix_notifications_read', 'notifications', ['read'])
    op.create_index('ix_notifications_source_id', 'notifications', ['source_id'])


def downgrade() -> None:
    op.drop_index('ix_notifications_source_id', table_name='notifications')
    op.drop_index('ix_notifications_read', table_name='notifications')
    op.drop_index(
        'ix_notifications_recipient_principal_id', table_name='notifications'
    )
    op.drop_table('notifications')
    op.drop_table('worker_access')
    op.drop_index(
        'ix_gpu_allocation_requests_status', table_name='gpu_allocation_requests'
    )
    op.drop_table('gpu_allocation_requests')
