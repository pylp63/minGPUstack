# 使用量页资源计量端点 (二开补充): 官方新版 UI 的「使用量」页
# 调用资源计量 API (summary / resource meta / resource breakdown /
# storage breakdown / resource events), 而二开后端只带 token 用量模型
# (model_usages, 见 usage.py 的 /usage/meta + /usage/breakdown)。
#
# 二开版没有 gpu_instance / volume 计量事件源 — 这里的实现是
# 「最小正确契约」: 端点存在、鉴权/分页/结构正确、数据如实为 0/空,
# 消除前端 404 弹窗, 页面可正常浏览。将来接入真实计量时只需替换
# 这几个 handler 的数据源。
#
# 契约来源: 官方 UI umi.js usage API 模块 (请求构造 T/H/U/j 函数) +
# p__usage 页组件的消费字段 (dataIndex 全集)。

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from gpustack.server.deps import SessionDep, CurrentUserDep, TenantContextDep
from gpustack.api.exceptions import InvalidException

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------- 响应模型 (新契约) ----------------


class Pagination(BaseModel):
    page: int = 1
    perPage: int = 20
    total: int = 0
    totalPages: int = 0


class ResourceSummaryMetrics(BaseModel):
    gpu_hours: float = 0
    instance_hours: float = 0
    unit_hours: float = 0
    resources: int = 0
    active_users: int = 0
    gb_days: float = 0
    gb_hours: float = 0
    last_active: Optional[str] = None


class ResourceSummary(BaseModel):
    """resource/storage breakdown 的 summary 块 (P() 适配器消费)。"""

    metrics: ResourceSummaryMetrics = ResourceSummaryMetrics()


class ResourceBreakdownItem(BaseModel):
    id: Optional[int] = None
    key: Optional[str] = None
    deleted: bool = False
    date: Optional[str] = None
    metrics: ResourceSummaryMetrics = ResourceSummaryMetrics()
    instance_type_name: Optional[str] = None
    organization_name: Optional[str] = None


class ResourceBreakdownResponse(BaseModel):
    summary: ResourceSummary = ResourceSummary()
    group_by: List[str] = []
    granularity: Optional[str] = None
    pagination: Pagination = Pagination()
    items: List[ResourceBreakdownItem] = []


class UsageSummaryResponse(BaseModel):
    """GET /usage/summary — 总览卡片 (H() 适配器消费)。"""

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    token_active_users: int = 0
    gpu_hours: float = 0
    unit_hours: float = 0
    instance_hours: float = 0
    storage_gb_days: float = 0
    active_users: int = 0


class MetaOption(BaseModel):
    label: str
    value: Any


class ResourceMetaResponse(BaseModel):
    """GET /usage/resource/meta — 过滤器选项 (U() 适配器消费)。"""

    creators: List[MetaOption] = []
    instances: List[MetaOption] = []
    volumes: List[MetaOption] = []
    organizations: List[MetaOption] = []
    user_groups: List[MetaOption] = []


class ResourceEvent(BaseModel):
    occurred_at: Optional[str] = None
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    event_type: Optional[str] = None
    event_message: Optional[str] = None


class ResourceEventsResponse(BaseModel):
    items: List[ResourceEvent] = []
    pagination: Pagination = Pagination()


# ---------------- 请求模型 ----------------


class ResourceBreakdownRequest(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    scope: str = "all"
    group_by: List[str] = Field(default_factory=lambda: ["resource_type"])
    granularity: str = "day"
    page: int = 1
    perPage: int = 20
    order_by: Optional[str] = None
    descending: Optional[bool] = None


# ---------------- 端点 ----------------


@router.get("/summary", response_model=UsageSummaryResponse)
async def get_usage_summary(
    session: SessionDep,
    user: CurrentUserDep,
    ctx: TenantContextDep,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    scope: str = "all",
):
    """总览卡片数据。二开版无资源计量源 — 返回 0 值 (token 部分将来可
    从 model_usages 聚合, 保持字段契约不变)。"""
    return UsageSummaryResponse()


@router.get("/resource/meta", response_model=ResourceMetaResponse)
async def get_resource_meta(
    session: SessionDep,
    user: CurrentUserDep,
    ctx: TenantContextDep,
    scope: str = "all",
):
    """资源计量页的过滤器选项。二开版无 GPU 实例/存储卷 — 全部空列表
    (前端对空数组的渲染是「暂无数据」, 不报错)。"""
    return ResourceMetaResponse()


def _breakdown_response(req: ResourceBreakdownRequest) -> ResourceBreakdownResponse:
    return ResourceBreakdownResponse(
        summary=ResourceSummary(),
        group_by=req.group_by,
        granularity=req.granularity,
        pagination=Pagination(page=req.page, perPage=req.perPage, total=0),
        items=[],
    )


@router.post("/resource/breakdown", response_model=ResourceBreakdownResponse)
async def resource_breakdown(
    session: SessionDep,
    user: CurrentUserDep,
    ctx: TenantContextDep,
    req: ResourceBreakdownRequest,
):
    """算力 (GPU 实例) 用量分解。二开版无计量数据 — 空表响应。"""
    return _breakdown_response(req)


@router.post("/gpu-instances/breakdown", response_model=ResourceBreakdownResponse)
async def gpu_instances_breakdown(
    session: SessionDep,
    user: CurrentUserDep,
    ctx: TenantContextDep,
    req: ResourceBreakdownRequest,
):
    """GPU 实例用量分解 (按机型)。二开版无计量数据 — 空表响应。"""
    return _breakdown_response(req)


@router.post("/storage/breakdown", response_model=ResourceBreakdownResponse)
async def storage_breakdown(
    session: SessionDep,
    user: CurrentUserDep,
    ctx: TenantContextDep,
    req: ResourceBreakdownRequest,
):
    """存储用量分解。二开版无计量数据 — 空表响应。"""
    return _breakdown_response(req)


@router.get("/resource-events", response_model=ResourceEventsResponse)
async def resource_events(
    session: SessionDep,
    user: CurrentUserDep,
    ctx: TenantContextDep,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    scope: str = "all",
    resource_type: Optional[str] = None,
    resource_name: Optional[str] = None,
    page: int = 1,
    perPage: int = 50,
):
    """资源事件流水 (创建/删除/启停)。二开版无计量事件 — 空列表响应。"""
    return ResourceEventsResponse(
        pagination=Pagination(page=page, perPage=perPage, total=0)
    )


# ---------------- 导出 (空数据集) ----------------
# 前端导出按钮调用; 二开版无计量数据 — 返回空 CSV (与数据页一致)。


@router.post("/gpu-instances/breakdown/export/estimate")
@router.post("/storage/breakdown/export/estimate")
async def export_estimate(
    session: SessionDep,
    user: CurrentUserDep,
    ctx: TenantContextDep,
):
    return {"rows": 0, "estimated": True}


@router.post("/gpu-instances/breakdown/export")
@router.post("/storage/breakdown/export")
async def export_breakdown(
    session: SessionDep,
    user: CurrentUserDep,
    ctx: TenantContextDep,
):
    from fastapi.responses import StreamingResponse
    import io
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["message"])
    writer.writerow(["no metered usage data"])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="usage-export.csv"'},
    )
