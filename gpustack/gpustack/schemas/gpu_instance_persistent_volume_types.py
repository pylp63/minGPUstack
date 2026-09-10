from enum import Enum
from typing import Optional, ClassVar, List

from pydantic import ConfigDict, BaseModel, model_validator
from sqlalchemy import UniqueConstraint, Column, Integer, ForeignKey
from sqlmodel import SQLModel, Field

from gpustack.mixins import BaseModelMixin
from gpustack.schemas.common import (
    pydantic_camel_case_generator,
    pydantic_column_type,
    ListParams,
    PublicFields,
    PaginatedList,
)


class GPUInstancePersistentVolumeTypeSpec(BaseModel):
    """Specification for creating or updating a GPU instance persistent volume type.

    二开说明: 原版把 NFS / S3 的固定结构编译进 schema (``nfs`` / ``s3``
    两个强类型字段), 无法适配其它存储后端. 本版本改为 **自由结构 spec**
    (``driver`` + 任意键值), 存储后端完全由实际部署环境决定, 管理员可
    随时增删存储类型:

    - ``driver``: 存储驱动标识 (如 ``nfs`` / ``s3`` / ``cephfs`` / ``juicefs``
      / 任何 operator 支持的后端). 服务端不限制取值, 只透传给
      gpustack-operator / CSI.
    - 其余字段: 该 driver 所需的连接参数, 原样存储与下发 (敏感字段可加
      ``secret_`` 前缀, public 视图会遮蔽).

    保留 ``nfs`` / ``s3`` 键的兼容读取: 旧数据 / 旧客户端用
    ``{"nfs": {"server": ..., "share": ...}}`` 提交时自动归一为
    ``{"driver": "nfs", ...扁平字段}``.
    """

    model_config = ConfigDict(
        alias_generator=pydantic_camel_case_generator,
        populate_by_name=True,
        extra="allow",
    )

    driver: Optional[str] = None
    """
    Storage driver identifier (e.g. "nfs", "s3", "cephfs", "juicefs").
    Free-form — the server passes it through to the cluster operator;
    which drivers actually work depends on what the deployment
    environment provides.
    """

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_nfs_s3(cls, data):
        """Flatten legacy ``nfs`` / ``s3`` sub-objects into driver-form."""
        if not isinstance(data, dict):
            return data
        if data.get("driver") is None and ("nfs" in data or "s3" in data):
            normalized = {k: v for k, v in data.items() if k not in ("nfs", "s3")}
            legacy = data.get("nfs") or data.get("s3") or {}
            if isinstance(legacy, dict):
                normalized["driver"] = "nfs" if data.get("nfs") else "s3"
                for key, value in legacy.items():
                    normalized.setdefault(key, value)
            return normalized
        return data


class GPUInstancePersistentVolumeTypeSpecPublic(GPUInstancePersistentVolumeTypeSpec):
    """Public-view spec.

    二开说明: 原版通过强类型 ``s3`` 子模型剔除 ``secret_key``. 自由结构
    下改为通用遮蔽规则: 键名以 ``secret_`` 开头或命中常见敏感名
    (``secret_key`` / ``password`` / ``token``) 的字段在 public 视图中
    以 ``"***"`` 返回.
    """

    @model_validator(mode="after")
    def _mask_secrets(self):
        _SENSITIVE_KEYS = {
            "secret_key", "secretkey", "password", "passwd", "token",
            "access_key_secret", "client_secret",
        }

        def mask(value):
            if isinstance(value, dict):
                return {
                    k: ("***" if (k in _SENSITIVE_KEYS or k.startswith("secret_")) else mask(v))
                    for k, v in value.items()
                }
            if isinstance(value, list):
                return [mask(v) for v in value]
            return value

        masked = {"driver": self.driver}
        for key, value in (self.model_extra or {}).items():
            masked[key] = "***" if (
                key in _SENSITIVE_KEYS or key.startswith("secret_")
            ) else mask(value)
        # model_construct 跳过校验器, 避免递归; 手动重建 extras
        clone = self.__class__.model_construct(driver=self.driver)
        object.__setattr__(clone, "__pydantic_extra__", {
            k: v for k, v in masked.items() if k != "driver"
        })
        return clone


class GPUInstancePersistentVolumeTypePhase(str, Enum):
    """Lifecycle phase of a persistent volume type row.

    Managed entirely server-side (unlike GPUInstance phases, which mirror
    worker CR phases): a row is active while its status phase is unset, ``Ready``
    once created, and ``Deleting`` from soft-delete until the finalizer
    hard-deletes it.
    """

    READY = "Ready"
    DELETING = "Deleting"


class GPUInstancePersistentVolumeTypeStatus(BaseModel):
    """
    Represents the status of a GPU instance persistent volume type.
    """

    model_config = ConfigDict(
        alias_generator=pydantic_camel_case_generator,
        populate_by_name=True,
    )

    phase: Optional[str] = None
    """
    The current phase, e.g. "Deleting" during finalizer-driven soft delete;
    None means the type is active.
    """

    phase_message: Optional[str] = None
    """
    Optional message with detail about the current phase (e.g. why finalize is waiting).
    """

    finalizing: Optional[List[int]] = None
    """
    Cluster ids whose downstream object is not yet cleaned up; the row is
    hard-deleted once this is empty.
    """


class GPUInstancePersistentVolumeTypeBase(SQLModel):
    """
    Base model for GPU instance persistent volume types, containing common fields.
    """

    model_config = ConfigDict(
        alias_generator=pydantic_camel_case_generator,
        populate_by_name=True,
    )

    # For tenant scope.
    # Every object belongs to one Org. The route layer fills this with
    # ctx.current_principal_id (or platform_principal_id() for admin).
    owner_principal_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    display_name: Optional[str] = Field(
        nullable=True,
        default=None,
        max_length=63,
    )
    """
    Display name of the GPU instance persistent volume type, for easier identification by users.
    """

    description: Optional[str] = Field(
        nullable=True,
        default=None,
        max_length=1024,
    )
    """
    Description of the GPU instance persistent volume type.
    """


class GPUInstancePersistentVolumeType(
    GPUInstancePersistentVolumeTypeBase, BaseModelMixin, table=True
):
    """
    Represents a GPU Instance persistent volume type.
    """

    __tablename__ = 'gpu_instance_persistent_volume_types'
    __table_args__ = (
        # Enforce unique constraint on (owner_principal_id, name) to ensure
        # each principal can only have one key with a given name.
        # This allows different principals to have keys with the same name,
        # but prevents duplicates for the same principal.
        UniqueConstraint(
            'owner_principal_id',
            'name',
            name='uq_gpu_instance_persistent_volume_type_name_per_principal',
        ),
    )
    id: Optional[int] = Field(default=None, primary_key=True)

    # Record the creator of the GPU instance persistent volume type for
    # auditing and ownership purposes.
    creator_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("principals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    """
    Reference to the principal who created the GPU instance persistent volume type.
    """

    name: str = Field(
        max_length=63,
    )
    """
    Name of the GPU instance persistent volume type.
    Must be unique in the scope of the owning principal.
    """

    spec: GPUInstancePersistentVolumeTypeSpec = Field(
        sa_type=pydantic_column_type(GPUInstancePersistentVolumeTypeSpec),
    )
    """
    Specification for the GPU instance persistent volume type, including NFS or S3 configuration.
    """

    status: Optional[GPUInstancePersistentVolumeTypeStatus] = Field(
        sa_type=pydantic_column_type(GPUInstancePersistentVolumeTypeStatus),
        default=None,
    )
    """
    Status of the GPU instance persistent volume type, including the soft-delete
    phase and the clusters still being finalized.
    """

    def is_deleting(self) -> bool:
        """Whether the volume type is soft-deleted and awaiting finalization."""
        return (
            self.status is not None
            and self.status.phase == GPUInstancePersistentVolumeTypePhase.DELETING
        )


class GPUInstancePersistentVolumeTypeUpdate(GPUInstancePersistentVolumeTypeBase):
    """
    Represents the fields that can be updated for a GPU instance persistent volume type.
    """

    pass


class GPUInstancePersistentVolumeTypeCreate(GPUInstancePersistentVolumeTypeUpdate):
    """
    Represents the fields required to create a new GPU instance persistent volume type.
    """

    name: str
    """
    Created name of the GPU instance persistent volume type.
    Must be unique in the scope of the owning principal.
    """

    spec: GPUInstancePersistentVolumeTypeSpec
    """
    Specification for the GPU instance persistent volume type, including NFS or S3 configuration.
    """


class GPUInstancePersistentVolumeTypePublic(
    GPUInstancePersistentVolumeTypeCreate, PublicFields
):
    """
    Represents the public view of a GPU instance persistent volume type,
    containing only fields that are safe to expose to clients.
    """

    spec: GPUInstancePersistentVolumeTypeSpecPublic

    creator_id: Optional[int] = None
    """
    Reference to the principal who created the GPU instance persistent volume type.
    """

    status: Optional[GPUInstancePersistentVolumeTypeStatus] = None
    """
    Status of the GPU instance persistent volume type (soft-delete phase, finalizing clusters).
    """


class GPUInstancePersistentVolumeTypeListParams(ListParams):
    sortable_fields: ClassVar[List[str]] = [
        "id",
        "name",
        "created_at",
        "updated_at",
    ]


GPUInstancePersistentVolumeTypesPublic = PaginatedList[
    GPUInstancePersistentVolumeTypePublic
]
