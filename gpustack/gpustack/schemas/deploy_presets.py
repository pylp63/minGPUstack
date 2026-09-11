"""Deployment architecture presets v2 (二开增强).

在原有 4 种形态基础上补全为完整的多机服务形态引擎:

* ``standalone``       单机(多卡 TP), 支持多实例 replicas.
* ``pipeline_parallel`` 多机流水线: N 节点各跑一个 PP stage, 每 stage 可配 TP,
                        stage 索引与 PP_SIZE 通过环境变量注入.
* ``pd_disaggregated``  PD 分离: P/D 各选节点数, 每组可配 TP.
* ``multi_pd``          多 P 多 D: P/D 各多副本.
* ``custom``            自定义拓扑: 调用方直接给出完整 roles 规格.

所有形态统一展开为 ``PresetDeployPlan``:
  - roles: 每个角色 (prefill/decode/router/pipeline-stage) 的
    节点数/GPU/TP/环境变量/启动命令/依赖 — 部署计划可预览 (验收要求).
  - plan_yaml: 完整计划 YAML (可下载).
  - payloads: 按顺序可 POST 的 ModelCreate payload 列表.

引擎互连参数 (PD/multi_pd, 二开修复 — 原版两个引擎都不认):
  - vLLM:   --kv-transfer-config '{"kv_role":"kv_producer"|"kv_consumer",...}'
            (官方 PD 文档语法; producer/consumer 两侧对称注入)
  - SGLang: --disaggregation-mode prefill|decode
            (官方 launch_server 语法, 替换不存在的 --server-type)

设计原则: 不改 Model/ModelInstance 表结构, 全部通过既有的
``backend_parameters`` + ``env`` + ``replicas`` +
``distributed_inference_across_workers`` 表达, 与官方调度器/worker 兼容.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field


class DeploymentArchitectureEnum(str, Enum):
    STANDALONE = "standalone"
    PD_DISAGGREGATED = "pd_disaggregated"
    MULTI_PD = "multi_pd"
    PIPELINE_PARALLEL = "pipeline_parallel"
    CUSTOM = "custom"


class RoleSpec(BaseModel):
    """一个角色 (prefill / decode / router / pipeline stage) 的部署规格."""

    role: str = Field(description="prefill | decode | router | pipeline")
    replicas: int = Field(default=1, ge=1, le=64)
    # 每副本占用的节点数 (跨节点 stage/角色组) 与每节点 GPU 数
    nodes_per_replica: int = Field(default=1, ge=1, le=64)
    gpus_per_node: int = Field(default=1, ge=1, le=64)
    # 每 stage 张量并行 (PP 形态下每 stage 内的 TP)
    tensor_parallel_size: int = Field(default=0, ge=0, le=64)
    env: Dict[str, str] = Field(default_factory=dict)
    extra_backend_parameters: List[str] = Field(default_factory=list)
    depends_on: List[str] = Field(default_factory=list, description="角色依赖")


class CustomTopology(BaseModel):
    """custom 形态: 调用方直接给出角色规格列表."""

    roles: List[RoleSpec]


class PresetDeployRequest(BaseModel):
    """POST /v2/deploy-presets/plan|deploy|preview 的请求体."""

    architecture: DeploymentArchitectureEnum = (
        DeploymentArchitectureEnum.STANDALONE
    )
    model_name: str = Field(min_length=1, max_length=255)
    model_source: str = Field(min_length=1, description="HF repo id or URL")
    # 模型来源类型: huggingface / model_scope / local_path
    model_source_kind: Optional[str] = None
    backend: Optional[str] = None
    # 单机多实例 (standalone)
    replicas: int = Field(default=1, ge=1, le=64)
    # PD / multi-PD
    prefill_gpu_count: int = Field(default=1, ge=1, le=1024)
    decode_gpu_count: int = Field(default=1, ge=1, le=1024)
    prefill_replicas: int = Field(default=1, ge=1, le=64)
    decode_replicas: int = Field(default=1, le=64, ge=1)
    router_replicas: int = Field(default=1, ge=1, le=64)
    # KV 传输 (KV-aware 路由的运行时开关, 透传给后端)
    kv_transfer: bool = False
    # 流水线并行: N 节点 N stage, 每 stage TP
    pipeline_parallel_size: int = Field(default=1, ge=1, le=64)
    tensor_parallel_size: int = Field(default=1, ge=1, le=64)
    backend_parameters: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    cluster_id: Optional[int] = None
    # custom 形态
    custom_topology: Optional[CustomTopology] = None


class RolePlan(BaseModel):
    """预览/拓扑图里的一个角色节点 (含验收要求的全字段)."""

    role: str
    name: str
    replicas: int
    nodes: int = Field(description="总节点数 = replicas * nodes_per_replica")
    gpus_per_node: int
    tensor_parallel_size: int
    port: int = Field(default=8000)
    env: Dict[str, str]
    backend_parameters: List[str]
    command: str = Field(default="", description="启动命令 (预览用)")
    depends_on: List[str] = Field(default_factory=list)


class PresetDeployPlan(BaseModel):
    """展开后的完整部署计划 (可预览)."""

    architecture: str
    description: str
    roles: List[RolePlan]
    payloads: List[Dict[str, Any]]
    plan_yaml: str = Field(default="", description="完整计划 YAML")
    # deploy 后回填: [{"id": .., "name": ..}] (plan 阶段为空)
    created_models: List[Dict[str, Any]] = Field(default_factory=list)


PRESET_DESCRIPTIONS = {
    DeploymentArchitectureEnum.STANDALONE: (
        "单机(多卡)部署 — 单模型实例, 支持 TP 多卡与多副本."
    ),
    DeploymentArchitectureEnum.PD_DISAGGREGATED: (
        "PD 分离 — 独立 Prefill / Decode 实例组, 各自节点池; "
        "Router 自动路由 (同 owner 模型名即可聚合)."
    ),
    DeploymentArchitectureEnum.MULTI_PD: (
        "多 P 多 D — Prefill / Decode 各多副本, Router 多副本, "
        "支持 KV 传输参数透传 (KV-aware 路由)."
    ),
    DeploymentArchitectureEnum.PIPELINE_PARALLEL: (
        "多机流水线 — N 节点各跑一个 PP stage, 每 stage 可配 TP, "
        "stage 索引通过环境变量注入."
    ),
    DeploymentArchitectureEnum.CUSTOM: (
        "自定义拓扑 — 直接指定角色/节点/副本/环境变量."
    ),
}


def _default_port(architecture: str, role: str) -> int:
    # GPUStack 网关统一入口 80/443; 实例端口由调度器分配 (8080 起步).
    # 这里给预览用的示意端口.
    base = {
        "prefill": 8100,
        "decode": 8200,
        "router": 8080,
        "pipeline": 8300,
        "standalone": 8000,
    }
    return base.get(role, 8000)


def _merge_backend_parameters(payload: Dict[str, Any], extra: List[str]) -> None:
    """追加 backend 参数, 同名 flag 覆盖旧值."""
    from gpustack.utils.command import find_parameter

    existing = list(payload.get("backend_parameters") or [])
    for flag in extra:
        name = flag.split("=")[0].split(" ")[0].lstrip("-")
        existing = [
            p
            for p in existing
            if find_parameter([p], [name.lstrip("-")]) is None
            and p.split("=")[0].split(" ")[0].lstrip("-") != name
        ]
        existing.append(flag)
    payload["backend_parameters"] = existing


def _base_payload(req: PresetDeployRequest, **overrides) -> Dict[str, Any]:
    # ModelCreate 的 source 是枚举 (huggingface/model_scope/local_path),
    # repo id 在 huggingface_repo_id / model_scope_model_id / local_path.
    src_field = {
        "huggingface": "huggingface_repo_id",
        "model_scope": "model_scope_model_id",
    }
    payload: Dict[str, Any] = {
        "name": req.model_name,
        "source": req.model_source_kind or "huggingface",
        "replicas": 1,
        "backend_parameters": list(req.backend_parameters or []),
        "distributed_inference_across_workers": False,
    }
    kind = payload["source"]
    if kind in src_field:
        payload[src_field[kind]] = req.model_source
    else:
        payload["local_path"] = req.model_source
    if req.backend:
        payload["backend"] = req.backend
    if req.env:
        payload["env"] = dict(req.env)
    if req.cluster_id is not None:
        payload["cluster_id"] = req.cluster_id
    payload.update(overrides)
    return payload


def _kv_flags(req: PresetDeployRequest, role: str) -> List[str]:
    """KV 传输参数 — 按角色对称生成 (二开修复: 原版 decode 侧缺失).

    vLLM 官方 PD 语法 (--kv-transfer-config, kv_producer/kv_consumer):
    producer/prefill 侧 kv_rank=0, consumer/decode 侧 kv_rank=1.
    """
    if not req.kv_transfer:
        return []
    kv_role = "kv_producer" if role == "prefill" else "kv_consumer"
    kv_rank = 0 if role == "prefill" else 1
    return [
        "--kv-transfer-config",
        '{"kv_connector":"PyNcclConnector","kv_role":"%s","kv_rank":%d,'
        '"kv_parallel_size":2}' % (kv_role, kv_rank),
    ]


def _engine_role_parameters(
    backend: Optional[str], role: str, kv_transfer: bool
) -> List[str]:
    """按引擎生成 P/D 角色互连参数 (二开修复: 原版 --api-server-type
    在 vLLM/SGLang 都不存在, 引擎启动直接失败).

    kv_transfer=False 时返回空 — PD 两组作为普通独立实例部署, 由
    GPUStack model-route 聚合, 不带引擎互连参数 (互连参数要求
    producer/consumer 成对组网, 单独出现引擎起不来).

    - vLLM (默认): --kv-transfer-config (kv_producer / kv_consumer)
    - SGLang:      --disaggregation-mode prefill|decode (官方 launch_server 语法)
    """
    if not kv_transfer:
        return []
    b = (backend or "vllm").lower()
    if b == "sglang":
        mode = "prefill" if role == "prefill" else "decode"
        return [f"--disaggregation-mode={mode}"]
    # vLLM 形式
    kv_role = "kv_producer" if role == "prefill" else "kv_consumer"
    kv_rank = 0 if role == "prefill" else 1
    return [
        "--kv-transfer-config",
        '{"kv_connector":"PyNcclConnector","kv_role":"%s","kv_rank":%d,'
        '"kv_parallel_size":2}' % (kv_role, kv_rank),
    ]


def _role_plan(
    role: str,
    name: str,
    replicas: int,
    nodes_per_replica: int,
    gpus_per_node: int,
    tp: int,
    env: Dict[str, str],
    backend_parameters: List[str],
    command: str,
    depends_on: Optional[List[str]] = None,
) -> RolePlan:
    return RolePlan(
        role=role,
        name=name,
        replicas=replicas,
        nodes=replicas * nodes_per_replica,
        gpus_per_node=gpus_per_node,
        tensor_parallel_size=tp,
        port=_default_port("", role),
        env=env,
        backend_parameters=backend_parameters,
        command=command,
        depends_on=depends_on or [],
    )


def _finalize(
    architecture: str,
    roles: List[RolePlan],
    payloads: List[Dict[str, Any]],
) -> PresetDeployPlan:
    doc = {
        "architecture": architecture,
        "roles": [r.model_dump() for r in roles],
        "deployments": [
            {k: v for k, v in p.items() if k in (
                "name", "replicas", "backend", "backend_parameters", "env",
                "distributed_inference_across_workers",
            )}
            for p in payloads
        ],
    }
    return PresetDeployPlan(
        architecture=architecture,
        description=PRESET_DESCRIPTIONS[
            DeploymentArchitectureEnum(architecture)
        ],
        roles=roles,
        payloads=payloads,
        plan_yaml=yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
    )


def build_preset_payloads(req: PresetDeployRequest) -> PresetDeployPlan:
    """Expand a preset request into a full deploy plan + payloads."""
    arch = req.architecture

    # ---------------- standalone (单机多卡, 多实例) ----------------
    if arch == DeploymentArchitectureEnum.STANDALONE:
        tp = req.tensor_parallel_size
        payload = _base_payload(req, replicas=req.replicas)
        flags = []
        if tp > 1:
            flags.append(f"--tensor-parallel-size={tp}")
        if flags:
            _merge_backend_parameters(payload, flags)
        role = _role_plan(
            "standalone", req.model_name, req.replicas, 1,
            max(tp, 1), tp, dict(req.env or {}), flags,
            f"vllm serve {req.model_source} --tensor-parallel-size={tp}",
        )
        return _finalize(arch.value, [role], [payload])

    # ---------------- PD 分离 ----------------
    if arch == DeploymentArchitectureEnum.PD_DISAGGREGATED:
        p_flags = (
            [f"--tensor-parallel-size={req.prefill_gpu_count}"]
            + _engine_role_parameters(req.backend, "prefill",
                                      req.kv_transfer)
        )
        d_flags = (
            [f"--tensor-parallel-size={req.decode_gpu_count}"]
            + _engine_role_parameters(req.backend, "decode",
                                      req.kv_transfer)
        )
        p_payload = _base_payload(req, name=f"{req.model_name}-prefill")
        _merge_backend_parameters(p_payload, p_flags)
        d_payload = _base_payload(req, name=f"{req.model_name}-decode")
        _merge_backend_parameters(d_payload, d_flags)
        roles = [
            _role_plan("prefill", f"{req.model_name}-prefill",
                       req.prefill_replicas, 1, req.prefill_gpu_count,
                       req.prefill_gpu_count,
                       {"ROLE": "prefill", **(req.env or {})},
                       p_flags,
                       f"vllm serve {req.model_source} --tensor-parallel-size="
                       f"{req.prefill_gpu_count} (prefill)",
                       depends_on=[]),
            _role_plan("decode", f"{req.model_name}-decode",
                       req.decode_replicas, 1, req.decode_gpu_count,
                       req.decode_gpu_count,
                       {"ROLE": "decode", **(req.env or {})},
                       d_flags,
                       f"vllm serve {req.model_source} --tensor-parallel-size="
                       f"{req.decode_gpu_count} (decode)",
                       depends_on=["prefill"]),
            _role_plan("router", f"{req.model_name}-router",
                       req.router_replicas, 1, 0, 0,
                       {"ROLE": "router", **(req.env or {})},
                       [],
                       "gpustack model-route (同 owner 自动聚合路由)",
                       depends_on=["prefill", "decode"]),
        ]
        return _finalize(arch.value, roles, [p_payload, d_payload])

    # ---------------- 多 P 多 D ----------------
    if arch == DeploymentArchitectureEnum.MULTI_PD:
        p_flags = (
            [f"--tensor-parallel-size={req.prefill_gpu_count}"]
            + _engine_role_parameters(req.backend, "prefill",
                                      req.kv_transfer)
        )
        d_flags = (
            [f"--tensor-parallel-size={req.decode_gpu_count}"]
            + _engine_role_parameters(req.backend, "decode",
                                      req.kv_transfer)
        )
        p_payload = _base_payload(
            req, name=f"{req.model_name}-prefill", replicas=req.prefill_replicas
        )
        _merge_backend_parameters(p_payload, p_flags)
        d_payload = _base_payload(
            req, name=f"{req.model_name}-decode", replicas=req.decode_replicas
        )
        _merge_backend_parameters(d_payload, d_flags)
        roles = [
            _role_plan("prefill", f"{req.model_name}-prefill",
                       req.prefill_replicas, 1, req.prefill_gpu_count,
                       req.prefill_gpu_count,
                       {"ROLE": "prefill", **(req.env or {})},
                       p_flags,
                       f"vllm serve ... --tensor-parallel-size="
                       f"{req.prefill_gpu_count} (x{req.prefill_replicas})"),
            _role_plan("decode", f"{req.model_name}-decode",
                       req.decode_replicas, 1, req.decode_gpu_count,
                       req.decode_gpu_count,
                       {"ROLE": "decode", **(req.env or {})},
                       d_flags,
                       f"vllm serve ... --tensor-parallel-size="
                       f"{req.decode_gpu_count} (x{req.decode_replicas})",
                       depends_on=["prefill"]),
            _role_plan("router", f"{req.model_name}-router",
                       req.router_replicas, 1, 0, 0,
                       {"ROLE": "router", **(req.env or {})},
                       [], "model-route 多副本聚合路由",
                       depends_on=["prefill", "decode"]),
        ]
        return _finalize(arch.value, roles, [p_payload, d_payload])

    # ---------------- 多机流水线 PP ----------------
    if arch == DeploymentArchitectureEnum.PIPELINE_PARALLEL:
        n = req.pipeline_parallel_size
        tp = req.tensor_parallel_size
        # 单一部署: distributed_inference_across_workers=True, 调度器把
        # N*tp 张卡跨 N 个 worker 聚合; 每 stage 索引由 worker 端注入
        payload = _base_payload(
            req, replicas=1,
            distributed_inference_across_workers=True,
        )
        flags = [f"--pipeline-parallel-size={n}"]
        if tp > 1:
            flags.append(f"--tensor-parallel-size={tp}")
        _merge_backend_parameters(payload, flags)
        env = dict(req.env or {})
        env.update({
            "PP_SIZE": str(n),
            "TP_PER_STAGE": str(tp),
        })
        payload["env"] = env
        roles = [
            _role_plan(
                f"pipeline-stage-{i}", f"{req.model_name}-pp{i}",
                1, 1, tp, tp,
                {"ROLE": f"pp-stage-{i}", "STAGE_IDX": str(i),
                 "PP_SIZE": str(n), **(req.env or {})},
                [f"--pipeline-parallel-size={n}"],
                f"vllm serve {req.model_source} "
                f"--pipeline-parallel-size={n} "
                f"--tensor-parallel-size={tp} (stage {i}/{n})",
                depends_on=[f"pipeline-stage-{i-1}"] if i > 0 else [],
            )
            for i in range(n)
        ]
        return _finalize(arch.value, roles, [payload])

    # ---------------- 自定义拓扑 ----------------
    if arch == DeploymentArchitectureEnum.CUSTOM:
        if not req.custom_topology:
            raise ValueError("custom 形态需要 custom_topology.roles")
        payloads, roles = [], []
        for spec in req.custom_topology.roles:
            payload = _base_payload(
                req, name=f"{req.model_name}-{spec.role}",
                replicas=spec.replicas,
                distributed_inference_across_workers=(
                    spec.nodes_per_replica > 1
                ),
            )
            flags = list(spec.extra_backend_parameters)
            if spec.tensor_parallel_size > 1:
                flags.append(
                    f"--tensor-parallel-size={spec.tensor_parallel_size}"
                )
            if spec.nodes_per_replica > 1:
                flags.append(
                    f"--pipeline-parallel-size={spec.nodes_per_replica}"
                )
            _merge_backend_parameters(payload, flags)
            if spec.env:
                payload["env"] = {**(payload.get("env") or {}), **spec.env}
            payloads.append(payload)
            roles.append(_role_plan(
                spec.role, f"{req.model_name}-{spec.role}",
                spec.replicas, spec.nodes_per_replica,
                spec.gpus_per_node, spec.tensor_parallel_size,
                spec.env, flags, "custom", spec.depends_on,
            ))
        return _finalize(arch.value, roles, payloads)

    raise ValueError(f"Unknown architecture: {arch}")


def build_topology_graph(plan: PresetDeployPlan) -> Dict[str, Any]:
    """渲染拓扑图数据 (前端 SVG 用): 节点 + 依赖边.

    二开修复: 原版把角色名按 '-' 切段拼边, 生成不存在的节点 id
    (如 'edge-prefill'), 前端无法连线. 现在 depends_on 存的是角色名,
    边统一解析为『依赖角色 → 本角色』且指向真实存在的节点.
    """
    nodes = [
        {
            "id": r.name,
            "role": r.role,
            "label": f"{r.role}\n{r.name}",
            "replicas": r.replicas,
            "nodes": r.nodes,
            "gpus": r.gpus_per_node,
            "tp": r.tensor_parallel_size,
            "env": r.env,
            "cmd": r.command,
        }
        for r in plan.roles
    ]
    # 角色名 -> 该角色的节点 id (同一角色多节点时连到代表节点)
    role_to_id = {
        r.role: r.name
        for r in plan.roles
    }
    node_ids = {n["id"] for n in nodes}
    edges = []
    for r in plan.roles:
        for dep in r.depends_on:
            source = role_to_id.get(dep, dep if dep in node_ids else None)
            if source and source != r.name:
                edges.append({"from": source, "to": r.name})
    return {"architecture": plan.architecture, "nodes": nodes, "edges": edges}
