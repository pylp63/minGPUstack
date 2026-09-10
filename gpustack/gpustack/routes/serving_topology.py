"""Serving topology planning API — /v2/serving-topology.

Endpoints::

    POST /v2/serving-topology/plan     dry-run: level checks + group
                                       conversion + machine layout
    POST /v2/serving-topology/deploy   plan + create the models through
                                       the standard model-create path
                                       (quota gate, tenant stamping)

The planner implements the three-level hierarchy (resource topology →
per-group parallel strategy → service attributes) and the group/machine
conversion rules; see ``gpustack.schemas.deploy_topology``.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from gpustack.api.exceptions import InvalidException
from gpustack.schemas.deploy_topology import (
    PDServiceSpec,
    ResourceTopologyEnum,
    build_service_plan,
    suggest_topology_upgrade,
)
from gpustack.server.deps import SessionDep, TenantContextDep

logger = logging.getLogger(__name__)

router = APIRouter()


class TopologyPlanRequest(PDServiceSpec):
    """POST /v2/serving-topology/plan|deploy request body.

    Extends the service spec with the model identity, the resource
    topology (level 1) and the interaction mode (declarative vs
    resource-pool).
    """

    model_name: str
    model_source: str
    model_source_kind: Optional[str] = None
    backend: Optional[str] = None
    topology: ResourceTopologyEnum = ResourceTopologyEnum.SINGLE_NODE
    # resource_pool (machines given, groups derived) or declarative
    # (groups given, machines derived). Both converge on the same plan.
    mode: str = "resource_pool"
    cluster_id: Optional[int] = None


def _side_to_payloads(
    req: TopologyPlanRequest, side_name: str, group_count: int,
    parallel, distributed: bool,
) -> List[Dict[str, Any]]:
    """Expand one side's groups into ModelCreate payloads.

    Each group is one Model row: PD disaggregation is a service-level
    attribute realised as separate prefill/decode deployments, and the
    per-group parallel strategy lands in backend_parameters — the exact
    surface the inference backends already consume.
    """
    src_field = {
        "huggingface": "huggingface_repo_id",
        "model_scope": "model_scope_model_id",
    }
    payloads = []
    flags = []
    if parallel.tensor_parallel_size > 1:
        flags.append(f"--tensor-parallel-size={parallel.tensor_parallel_size}")
    if parallel.pipeline_parallel_size > 1:
        flags.append(
            f"--pipeline-parallel-size={parallel.pipeline_parallel_size}"
        )
    if parallel.expert_parallel_size > 1:
        flags.append(f"--enable-expert-parallel")
    # PD 互连参数按引擎生成 (vLLM kv-transfer / SGLang disaggregation)
    if req.enabled:
        engine = (req.backend or "vllm").lower()
        if side_name == "prefill":
            if engine == "sglang":
                flags.append("--disaggregation-mode=prefill")
            else:
                flags += [
                    "--kv-transfer-config",
                    '{"kv_connector":"PyNcclConnector","kv_role":'
                    '"kv_producer","kv_rank":0,"kv_parallel_size":2}',
                ]
        else:
            if engine == "sglang":
                flags.append("--disaggregation-mode=decode")
            else:
                flags += [
                    "--kv-transfer-config",
                    '{"kv_connector":"PyNcclConnector","kv_role":'
                    '"kv_consumer","kv_rank":1,"kv_parallel_size":2}',
                ]

    for i in range(group_count):
        name = req.model_name if group_count == 1 and side_name == "single" \
            else f"{req.model_name}-{side_name}-{i}"
        payload: Dict[str, Any] = {
            "name": name,
            "source": req.model_source_kind or "huggingface",
            "replicas": parallel.data_parallel_replicas,
            "backend_parameters": list(flags),
            "distributed_inference_across_workers": distributed,
        }
        kind = payload["source"]
        if kind in src_field:
            payload[src_field[kind]] = req.model_source
        else:
            payload["local_path"] = req.model_source
        if req.backend:
            payload["backend"] = req.backend
        if req.cluster_id is not None:
            payload["cluster_id"] = req.cluster_id
        if group_count > 1 or side_name != "single":
            payload["env"] = {
                "SERVING_ROLE": side_name,
                "GROUP_INDEX": str(i),
            }
        payloads.append(payload)
    return payloads


def _build_full_plan(req: TopologyPlanRequest) -> Dict[str, Any]:
    """Run the level checks and expand groups into payloads."""
    plan = build_service_plan(req.topology, req, mode=req.mode)

    if plan.errors:
        raise InvalidException(message="; ".join(plan.errors))

    result: Dict[str, Any] = {
        "topology": plan.topology.value,
        "pd_disaggregation": plan.pd_disaggregation,
        "prefill": None,
        "decode": None,
        "single": None,
        "payloads": [],
        "upgrade_hint": None,
    }

    if plan.single is not None:
        side = plan.single
        result["single"] = side.model_dump()
        result["payloads"] += _side_to_payloads(
            req, "single", side.groups_formed,
            req.prefill_parallel,
            distributed=side.cross_machine,
        )
        hint = suggest_topology_upgrade(
            req.prefill_parallel, req.prefill_pool
        )
        if hint:
            result["upgrade_hint"] = hint
    else:
        for name, side, parallel in (
            ("prefill", plan.prefill, req.prefill_parallel),
            ("decode", plan.decode, req.decode_parallel),
        ):
            result[name] = side.model_dump()
            result["payloads"] += _side_to_payloads(
                req, name, side.groups_formed, parallel,
                distributed=side.cross_machine,
            )
    return result


@router.post("/plan")
async def plan_serving_topology(req: TopologyPlanRequest):
    """Dry-run the three-level plan (no DB writes)."""
    try:
        return _build_full_plan(req)
    except InvalidException:
        raise
    except ValueError as e:
        raise InvalidException(message=str(e))


@router.post("/deploy")
async def deploy_serving_topology(
    session: SessionDep, ctx: TenantContextDep, req: TopologyPlanRequest
):
    """Create the planned models through the standard create path."""
    from gpustack.routes.models import create_model
    from gpustack.schemas.clusters import Cluster
    from gpustack.schemas.models import ModelCreate

    result = _build_full_plan(req)

    if not req.cluster_id:
        default_cluster = await Cluster.first_by_field(
            session=session, field="is_default", value=True
        )
        if default_cluster is not None:
            for payload in result["payloads"]:
                payload["cluster_id"] = default_cluster.id

    created = []
    try:
        for payload in result["payloads"]:
            model = await create_model(
                session=session, ctx=ctx, model_in=ModelCreate(**payload)
            )
            created.append(model)
    except Exception as e:
        logger.warning(
            f"serving topology deploy failed after {len(created)} model(s): {e}"
        )
        for m in created:
            try:
                await m.delete(session)
            except Exception:  # noqa: BLE001
                logger.warning(f"rollback failed for model {m.id}")
        raise

    logger.info(
        f"serving topology '{req.model_name}' deployed: "
        f"{[m.name for m in created]}"
    )
    result["created_models"] = [
        {"id": m.id, "name": m.name} for m in created
    ]
    return result
