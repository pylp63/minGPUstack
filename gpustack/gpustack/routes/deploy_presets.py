"""Deployment architecture presets v2 (feature two): one-click deploy.

Endpoints (v2 prefix, registered in routes.py)::

    GET  /deploy-presets                 list available architectures
    POST /deploy-presets/plan            expand to ModelCreate payloads
    POST /deploy-presets/preview         full plan: roles + topology + YAML
    POST /deploy-presets/deploy          plan + create the models

`deploy` runs the same expansion as `plan` and then creates the
resulting Model rows through the model-create path (including the
quota gate from feature one), so the presets respect user isolation.
"""

import logging
from typing import List

from fastapi import APIRouter

from gpustack.api.exceptions import InvalidException
from gpustack.schemas.deploy_presets import (
    DeploymentArchitectureEnum,
    PresetDeployPlan,
    PresetDeployRequest,
    PRESET_DESCRIPTIONS,
    build_preset_payloads,
    build_topology_graph,
)
from gpustack.server.deps import SessionDep, TenantContextDep

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=List[dict])
async def list_presets():
    """Enumerate the supported one-click deployment architectures."""
    out = []
    for arch, desc in PRESET_DESCRIPTIONS.items():
        params: List[str] = []
        if arch == DeploymentArchitectureEnum.PD_DISAGGREGATED:
            params = ["prefill_gpu_count", "decode_gpu_count",
                      "prefill_groups", "decode_groups",
                      "router_replicas", "kv_transfer"]
        elif arch == DeploymentArchitectureEnum.PIPELINE_PARALLEL:
            params = ["pipeline_parallel_size", "tensor_parallel_size"]
        elif arch == DeploymentArchitectureEnum.STANDALONE:
            params = ["replicas", "tensor_parallel_size"]
        elif arch == DeploymentArchitectureEnum.CUSTOM:
            params = ["custom_topology"]
        out.append({"architecture": arch.value, "description": desc,
                    "parameters": params})
    return out


@router.post("/plan", response_model=PresetDeployPlan)
async def plan_preset(req: PresetDeployRequest):
    """Expand an architecture request into ModelCreate payloads (dry-run)."""
    try:
        return build_preset_payloads(req)
    except ValueError as e:
        raise InvalidException(message=str(e))


@router.post("/preview")
async def preview_preset(req: PresetDeployRequest):
    """Full deploy plan preview: roles / topology graph / YAML.

    验收要求: 部署计划可预览, 包含节点角色、GPU、端口、环境变量、
    启动命令、依赖. 返回 topology(前端 SVG 用) + plan_yaml(可下载).
    """
    try:
        plan = build_preset_payloads(req)
    except ValueError as e:
        raise InvalidException(message=str(e))
    return {
        "architecture": plan.architecture,
        "description": plan.description,
        "roles": [r.model_dump() for r in plan.roles],
        "topology": build_topology_graph(plan),
        "plan_yaml": plan.plan_yaml,
        "payloads": plan.payloads,
    }


@router.post("/deploy", response_model=PresetDeployPlan)
async def deploy_preset(session: SessionDep, ctx: TenantContextDep,
                        req: PresetDeployRequest):
    """One-click deploy: expand the preset and create the models."""
    from gpustack.routes.models import create_model
    from gpustack.schemas.models import ModelCreate

    plan = build_preset_payloads(req)

    # models.cluster_id 非空约束: 未显式指定时回落到默认集群.
    if not req.cluster_id:
        from gpustack.schemas.clusters import Cluster
        default_cluster = await Cluster.first_by_field(
            session=session, field="is_default", value=True
        )
        if default_cluster is not None:
            for payload in plan.payloads:
                payload["cluster_id"] = default_cluster.id

    created = []
    try:
        for payload in plan.payloads:
            model_in = ModelCreate(**payload)
            # Reuse the full model-create path — validation, quota
            # enforcement (feature one), tenant stamping, route wiring.
            model = await create_model(session=session, ctx=ctx,
                                       model_in=model_in)
            created.append(model)
    except Exception as e:
        # Best effort rollback of already-created siblings so a failed
        # multi-part preset doesn't leave a half deployment behind.
        logger.warning(f"Preset deploy failed after {len(created)} model(s): {e}")
        for m in created:
            try:
                await m.delete(session)
            except Exception:  # noqa: BLE001
                logger.warning(f"Failed to roll back model {m.id}")
        raise

    plan.created_models = [
        {"id": m.id, "name": m.name} for m in created
    ]
    logger.info(
        f"Preset '{req.architecture.value}' deployed: "
        f"{[m.name for m in created]}"
    )
    return plan
