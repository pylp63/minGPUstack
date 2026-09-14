"""Deployment architecture presets v2 (二开增强).

在原有 4 种形态基础上补全为完整的多机服务形态引擎:

* ``standalone``       单机(多卡 TP), 支持多实例 replicas.
* ``pipeline_parallel`` 多机流水线: N 节点各跑一个 PP stage, 每 stage 可配 TP,
                        stage 索引与 PP_SIZE 通过环境变量注入.
* ``pd_disaggregated``  PD 分离: P/D 各选 GPU 数与组数 (组数 >1 即
                        多 P 多 D, 原 ``multi_pd`` 已合并进来).
* ``custom``            自定义拓扑: 调用方直接给出完整 roles 规格.

所有形态统一展开为 ``PresetDeployPlan``:
  - roles: 每个角色 (prefill/decode/router/pipeline-stage) 的
    节点数/GPU/TP/环境变量/启动命令/依赖 — 部署计划可预览 (验收要求).
  - plan_yaml: 完整计划 YAML (可下载).
  - payloads: 按顺序可 POST 的 ModelCreate payload 列表.

引擎互连参数 (PD 分离, 二开修复 — 原版两个引擎都不认):
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
    # PD 分离 (组数 >1 = 多 P 多 D)
    prefill_gpu_count: int = Field(default=1, ge=1, le=1024)
    decode_gpu_count: int = Field(default=1, ge=1, le=1024)
    prefill_groups: int = Field(default=1, ge=1, le=64)
    decode_groups: int = Field(default=1, le=64, ge=1)
    router_replicas: int = Field(default=1, ge=1, le=64)
    # 节点级 rank 分配: {"prefill": [[w1], [w2,w3]], "decode": [[w4]]}
    # 外层索引 = rank, 内层 = 该 rank 的节点列表 (长度 = pd_pipeline_size)
    pd_node_assign: Optional[Dict[str, List[List[str]]]] = None
    # PD 分离形态下的流水线并行度 (单 rank 跨几台节点; 1 = 单机放得下)
    pd_pipeline_size: int = Field(default=1, ge=1, le=16)
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
        "PD 分离 — 独立 Prefill / Decode 实例组; Prefill/Decode 组数 >1 "
        "即多 P 多 D 水平扩展, Router 自动聚合."
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


def _strip_role_params(payload: Dict[str, Any], names: List[str]) -> None:
    """从 payload 的用户预填参数里剔除该角色不该带的 flag."""
    if not names:
        return
    from gpustack.utils.command import find_parameter

    params = list(payload.get("backend_parameters") or [])
    kept = []
    for prm in params:
        bare = prm.split("=")[0].split(" ")[0].lstrip("-")
        if bare in names:
            continue
        kept.append(prm)
    payload["backend_parameters"] = kept


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
    backend: Optional[str], role: str, kv_transfer: bool,
    user_params: Optional[List[str]] = None,
) -> tuple:
    """返回 (flags, strip_names): flags = 该角色的互连参数;
    strip_names = 用户预填参数里该角色必须剔除的 flag 名 (如 decode 侧
    不该带 --disaggregation-bootstrap-server — 它是 P 侧起的 bootstrap
    server, decode 是连接方, 地址由 router/discovery 下发).
    按引擎生成 P/D 角色互连参数 (二开修复: 原版 --api-server-type
    在 vLLM/SGLang 都不存在, 引擎启动直接失败).

    kv_transfer=False 时返回空 — PD 两组作为普通独立实例部署, 由
    GPUStack model-route 聚合, 不带引擎互连参数 (互连参数要求
    producer/consumer 成对组网, 单独出现引擎起不来).

    - vLLM (默认): --kv-transfer-config (kv_producer / kv_consumer)
    - SGLang (官方 PD 语法, 二开补全):
        P: --disaggregation-mode=prefill --disaggregation-bootstrap-server
        D: --disaggregation-mode=decode
      KV bootstrap 端口 (默认 8555) 与传输后端 (默认 mooncake) 尊重
      用户在后端参数框里的预填值 — 前端 PD 预填的通用参数在框里,
      这里只补角色差异项, 同名 flag 不覆盖用户值.
    """
    if not kv_transfer:
        return []
    b = (backend or "vllm").lower()
    if b == "sglang":
        from gpustack.utils.command import find_parameter

        user_params = user_params or []
        mode = "prefill" if role == "prefill" else "decode"
        flags = [f"--disaggregation-mode={mode}"]
        strip = []
        if role == "prefill":
            # P 侧起 KV bootstrap server (D/router 连它); 用户改过端口则用用户的
            has_boot = find_parameter(user_params, ["disaggregation-bootstrap-server"])
            if has_boot is None:
                flags.append("--disaggregation-bootstrap-server=localhost:8555")
        else:
            # decode 侧: 剔除用户预填里的 bootstrap-server (那是 P 的)
            strip.append("disaggregation-bootstrap-server")
        # 传输后端: 用户预填的保留, 没有则默认 mooncake (PD 必需)
        has_tb = find_parameter(user_params, ["disaggregation-transfer-backend"])
        if has_tb is None:
            flags.append("--disaggregation-transfer-backend=mooncake")
        return flags, strip
    # vLLM 形式
    kv_role = "kv_producer" if role == "prefill" else "kv_consumer"
    kv_rank = 0 if role == "prefill" else 1
    return [
        "--kv-transfer-config",
        '{"kv_connector":"PyNcclConnector","kv_role":"%s","kv_rank":%d,'
        '"kv_parallel_size":2}' % (kv_role, kv_rank),
    ], []


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

    # ---------------- PD 分离 (组数>1 = 多 P 多 D) ----------------
    if arch == DeploymentArchitectureEnum.PD_DISAGGREGATED:
        _pf, _p_strip = _engine_role_parameters(
            req.backend, "prefill", req.kv_transfer, req.backend_parameters)
        _df, _d_strip = _engine_role_parameters(
            req.backend, "decode", req.kv_transfer, req.backend_parameters)
        p_flags = [f"--tensor-parallel-size={req.prefill_gpu_count}"] + _pf
        d_flags = [f"--tensor-parallel-size={req.decode_gpu_count}"] + _df
        # ---- 节点级 rank 分配 (pd_node_assign): 每角色每 rank 指定节点列表.
        # 有分配时每 rank 一个独立 Model (固定节点, 不靠调度器漂移);
        # 无分配时保持原行为 (P/D 两个 Model, 组数=replicas 由调度器摆放).
        assign = req.pd_node_assign or {}
        pp_size = req.pd_pipeline_size or 1
        if assign and (assign.get("prefill") or assign.get("decode")):
            # 互斥兜底 (UI 前端已置灰已选节点, 这里双保险):
            # 任一节点不得同时出现在两个不同 rank 的分配里 — P/D 各 rank
            # 的节点集合两两不相交, 否则同一台机被钉给两个实例.
            _owner: dict = {}
            for _role in ("prefill", "decode"):
                for _i, _rank_nodes in enumerate(assign.get(_role) or []):
                    for _n in _rank_nodes or []:
                        if not _n:
                            continue
                        if _n in _owner:
                            raise ValueError(
                                f"节点 {_n} 被重复分配: "
                                f"{_owner[_n]} 与 {_role} rank{_i} "
                                f"(PD 各 rank 节点不能重叠)"
                            )
                        _owner[_n] = f"{_role} rank{_i}"
            payloads, roles = [], []
            for role, cnt, gpu_count, flags in (
                ("prefill", req.prefill_groups,
                 req.prefill_gpu_count, p_flags),
                ("decode", req.decode_groups,
                 req.decode_gpu_count, d_flags),
            ):
                nodes_per_rank = assign.get(role) or []
                for i in range(cnt):
                    nodes = (
                        nodes_per_rank[i]
                        if i < len(nodes_per_rank) else []
                    )
                    nodes = [n for n in (nodes or []) if n][:pp_size]
                    if not nodes:
                        raise ValueError(
                            f"{role} rank{i} 未指定节点 "
                            f"(pd_node_assign.{role}[{i}])"
                        )
                    if len(nodes) != pp_size:
                        raise ValueError(
                            f"{role} rank{i} 需要 {pp_size} 个节点, "
                            f"实际 {len(nodes)}: {nodes}"
                        )
                    # gpu_ids: 每节点取 gpu_count 张卡 (worker_name:cuda:idx)
                    gpu_ids = []
                    for w in nodes:
                        gpu_ids += [
                            f"{w}:cuda:{g}" for g in range(gpu_count)
                        ]
                    p = _base_payload(
                        req,
                        name=f"{req.model_name}-{role}-{i}",
                        replicas=1,
                    )
                    p["gpu_selector"] = {
                        "gpu_ids": gpu_ids,
                        "gpus_per_replica": gpu_count,
                    }
                    r_flags = list(flags)
                    if pp_size > 1:
                        # 跨节点 PP: 分布式推理 + PP 参数
                        p["distributed_inference_across_workers"] = True
                        r_flags = [
                            f"--pipeline-parallel-size={pp_size}",
                            f"--tensor-parallel-size={gpu_count}",
                        ] + [
                            f for f in r_flags
                            if not f.startswith("--pipeline-parallel-size")
                            and not f.startswith("--tensor-parallel-size")
                        ]
                    _strip_role_params(p, _p_strip if role == "prefill" else _d_strip)
                    _merge_backend_parameters(p, r_flags)
                    payloads.append(p)
                    roles.append(_role_plan(
                        f"{role}-{i}", f"{req.model_name}-{role}-{i}",
                        1, len(nodes), gpu_count, gpu_count,
                        {"ROLE": f"{role}-{i}", **(req.env or {})},
                        r_flags,
                        f"sglang launch_server ... "
                        f"(nodes={nodes}, pp={pp_size})",
                        depends_on=(["prefill-0"] if role == "decode"
                                    else []),
                    ))
            return _finalize(arch.value, roles, payloads)
        # ---- 无节点分配: 原自动调度路径 ----
        # 组数即副本数: prefill_groups/decode_groups (合并进来的多 P 多 D)
        p_payload = _base_payload(
            req, name=f"{req.model_name}-prefill",
            replicas=req.prefill_groups,
        )
        _strip_role_params(p_payload, _p_strip)
        _merge_backend_parameters(p_payload, p_flags)
        d_payload = _base_payload(
            req, name=f"{req.model_name}-decode",
            replicas=req.decode_groups,
        )
        _strip_role_params(d_payload, _d_strip)
        _merge_backend_parameters(d_payload, d_flags)
        roles = [
            _role_plan("prefill", f"{req.model_name}-prefill",
                       req.prefill_groups, 1, req.prefill_gpu_count,
                       req.prefill_gpu_count,
                       {"ROLE": "prefill", **(req.env or {})},
                       p_flags,
                       f"vllm serve {req.model_source} --tensor-parallel-size="
                       f"{req.prefill_gpu_count} (prefill, x{req.prefill_groups})",
                       depends_on=[]),
            _role_plan("decode", f"{req.model_name}-decode",
                       req.decode_groups, 1, req.decode_gpu_count,
                       req.decode_gpu_count,
                       {"ROLE": "decode", **(req.env or {})},
                       d_flags,
                       f"vllm serve {req.model_source} --tensor-parallel-size="
                       f"{req.decode_gpu_count} (decode, x{req.decode_groups})",
                       depends_on=["prefill"]),
            _role_plan("router", f"{req.model_name}-router",
                       req.router_replicas, 1, 0, 0,
                       {"ROLE": "router", **(req.env or {})},
                       [],
                       "gpustack model-route (同 owner 自动聚合路由)",
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
