"""部署服务形态 API — /v2/deploy-topologies.

    POST /v2/deploy-topologies/plan    预览 (拓扑/单元/YAML, 不落库)
    POST /v2/deploy-topologies/deploy  一键部署 (创建 Model + ModelRoute)

deploy 复用现有 create_model / create_model_route 全链路 (校验/配额/租户/
路由注册), 单元失败回滚已创建对象, 不留半部署.
"""

import logging

from fastapi import APIRouter

from gpustack.api.exceptions import InvalidException
from gpustack.schemas.deploy_topologies import (
    DeployTopologyRequest,
    build_topology_plan,
)
from gpustack.server.deps import SessionDep, TenantContextDep

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/plan")
async def plan_topology(req: DeployTopologyRequest):
    """预览部署计划: 单元(角色/GPU/参数/端口/依赖) + 拓扑 + YAML."""
    try:
        return build_topology_plan(req)
    except ValueError as e:
        raise InvalidException(message=str(e))


@router.post("/deploy")
async def deploy_topology(
    session: SessionDep, ctx: TenantContextDep, req: DeployTopologyRequest
):
    """一键部署: 展开 -> 创建 Model(走现有链路) -> 创建 ModelRoute."""
    from gpustack.routes.model_routes import create_model_route
    from gpustack.routes.models import create_model
    from gpustack.schemas.models import Model, ModelCreate
    from gpustack.schemas.model_routes import ModelRouteCreate

    plan = build_topology_plan(req)

    # models.cluster_id 非空约束: 未显式指定时回落到默认集群
    # (deploy_presets 同款逻辑, 二开修复 — 否则单机部署必 500).
    if not req.cluster_id:
        from gpustack.schemas.clusters import Cluster
        default_cluster = await Cluster.first_by_field(
            session=session, field="is_default", value=True
        )
        if default_cluster is not None:
            req = req.model_copy(update={"cluster_id": default_cluster.id})

    created = []
    try:
        for unit in plan.units:
            payload = {
                "name": unit.name,
                "replicas": unit.replicas,
                "backend_parameters": unit.backend_parameters,
                "env": unit.env or None,
            }
            if unit.gpu_ids:
                payload["gpu_selector"] = {"gpu_ids": unit.gpu_ids}
                if unit.gpus_per_replica:
                    payload["gpu_selector"]["gpus_per_replica"] = (
                        unit.gpus_per_replica
                    )
            if unit.distributed:
                payload["distributed_inference_across_workers"] = True
            # 模型来源/引擎 (二开修复: 不再塞不存在的 model_source 字段;
            # source + repo 字段由 _base_fields 同款逻辑落位)
            source = req.source or "huggingface"
            payload["source"] = source
            if source == "model_scope":
                payload["model_scope_model_id"] = req.model_source
            elif source == "local_path":
                payload["local_path"] = req.model_source
            else:
                payload["huggingface_repo_id"] = req.model_source
            if req.backend:
                payload["backend"] = req.backend
            if req.cluster_id is not None:
                payload["cluster_id"] = req.cluster_id

            model = await create_model(
                session=session,
                ctx=ctx,
                model_in=ModelCreate(**payload),
            )
            created.append(model)

        # ModelRoute 聚合 (PD/custom 多单元形态)
        if plan.route and req.create_route:
            try:
                # 二开修复: ModelRouteCreate.targets 只接受
                # ModelRouteTargetUpdateItem (model_id: int 外键 + weight),
                # 不接受 {name, weight}. 模型创建后按名字查 id 组装.
                target_items = []
                for m in created:
                    if isinstance(m, Model) and m.name in (
                        plan.route.get("target_model_names") or []
                    ):
                        target_items.append(
                            {"model_id": m.id, "weight": 100}
                        )
                if target_items:
                    route = await create_model_route(
                        session=session,
                        ctx=ctx,
                        input=ModelRouteCreate(**{
                            "name": plan.route["name"],
                            "targets": target_items,
                        }),
                    )
                    created.append(route)
                else:
                    plan.warnings.append(
                        "路由创建跳过: 目标模型未在创建结果中找到"
                    )
            except Exception as e:  # noqa: BLE001
                # 路由失败不回滚模型, 只提示 (模型本身可用)
                logger.warning(f"route creation failed: {e}")
                plan.warnings.append(f"路由创建失败: {e}")

    except Exception:
        logger.warning(f"topology deploy failed after {len(created)} unit(s)")
        for m in created:
            try:
                await m.delete(session)
            except Exception:  # noqa: BLE001
                logger.warning(f"rollback failed for {getattr(m, 'name', '?')}")
        raise

    logger.info(
        f"topology '{req.shape.value}' deployed: "
        f"{[getattr(m, 'name', '?') for m in created]}"
    )
    return plan