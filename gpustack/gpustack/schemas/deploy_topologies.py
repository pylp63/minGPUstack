"""部署服务形态 (Deploy Topologies) — 二开功能.

五种服务形态, 统一展开为 ModelCreate payloads + 可选 ModelRoute:

* ``single_node_tp``  单机多卡 TP (可选多实例): 1 个 Model,
                       gpu_selector 指定同节点 GPU, replicas 可 >1.
* ``pp_multinode``    多机流水线: 1 个 Model, distributed_inference_
                       across_workers=True, 选 N 个节点 GPU, 注入
                       --pipeline-parallel-size=<节点数> (每 stage 1 节点),
                       每个 stage 内 TP 由 gpus_per_replica 决定.
* ``pd_disaggregated`` PD 分离: 2 个 Model (<name>-prefill / -decode,
                       引擎 server-type/kv-transfer 参数互连) +
                       1 个 ModelRoute 聚合 (对外一个入口);
                       prefill_replicas/decode_replicas >1 即多 P 多 D
                       (原 ``multi_pd`` 形态已合并进来).
* ``custom``          自定义拓扑: 角色列表 (role/replicas/gpu_ids/env/
                       backend_parameters), 每角色一个 Model + 聚合
                       ModelRoute (可选).

设计原则: 复用 GPUStack 原生分布式机制 (调度器 distributed_servers +
vLLM cal_multinode_topology), 不 fork worker 代码; PD 互连参数按引擎
(SGLang server-type / vLLM kv-transfer) 生成, 未指定引擎时给 vLLM 形式.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TopologyRole(BaseModel):
    """自定义拓扑里的一个角色 (custom 形态用)."""

    role: str = Field(min_length=1, max_length=64)  # prefill/decode/worker...
    replicas: int = Field(default=1, ge=1, le=64)
    # 跨节点 GPU id 列表 (每副本消耗 gpus_per_replica 张; 留空=自动调度)
    gpu_ids: Optional[List[str]] = None
    gpus_per_replica: Optional[int] = Field(default=None, ge=1, le=64)
    backend_parameters: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None


class DeployTopologyRequest(BaseModel):
    """POST /v2/deploy-topologies/{plan,deploy} 的请求体."""

    class ShapeEnum(str, Enum):
        SINGLE_NODE_TP = "single_node_tp"
        PP_MULTINODE = "pp_multinode"
        PD_DISAGGREGATED = "pd_disaggregated"
        CUSTOM = "custom"

    shape: ShapeEnum = ShapeEnum.SINGLE_NODE_TP
    # 模型 (与 ModelCreate 一致的字段)
    model_name: str = Field(min_length=1, max_length=255)
    model_source: str = Field(min_length=1)  # HF repo / modelscope id / 本地路径
    source: Optional[str] = None  # huggingface / model_scope / local_path
    backend: Optional[str] = None  # vllm / sglang / ...
    cluster_id: Optional[int] = None
    # 单机多卡
    gpu_ids: Optional[List[str]] = None  # 同一节点
    replicas: int = Field(default=1, ge=1, le=64)  # 实例数
    gpus_per_replica: Optional[int] = None
    # 多机流水线: N 个节点 (每个节点 = 1 个 PP stage), 每节点 TP 卡数
    pp_node_gpu_ids: Optional[List[List[str]]] = None  # [[w1:0,w1:1],[w2:0]]
    pp_tp_per_stage: Optional[int] = Field(default=None, ge=1, le=64)
    # PD 分离 (replicas 即组数, >1 = 多 P 多 D)
    prefill_gpu_ids: Optional[List[str]] = None
    decode_gpu_ids: Optional[List[str]] = None
    prefill_replicas: int = Field(default=1, ge=1, le=64)
    decode_replicas: int = Field(default=1, ge=1, le=64)
    prefill_gpus_per_replica: Optional[int] = None
    decode_gpus_per_replica: Optional[int] = None
    # 通用
    env: Optional[Dict[str, str]] = None
    backend_parameters: Optional[List[str]] = None
    # 自定义拓扑
    roles: Optional[List[TopologyRole]] = None
    # 路由 (PD/custom 自动创建; 也可显式关闭)
    create_route: bool = True
    route_name: Optional[str] = None


class DeployUnit(BaseModel):
    """计划里的一个部署单元 (= 一个 Model)."""

    role: str  # prefill / decode / worker / standalone / stage-N
    name: str  # model name
    replicas: int
    gpu_ids: Optional[List[str]] = None
    gpus_per_replica: Optional[int] = None
    distributed: bool = False
    backend_parameters: List[str] = []
    env: Dict[str, str] = {}
    # 预览信息
    ports: List[int] = []
    start_command_hint: str = ""
    depends_on: List[str] = []  # 依赖的其它单元 (按 name)


class DeployTopologyPlan(BaseModel):
    """plan 响应: 展开结果 + 拓扑图数据 + YAML 预览."""

    shape: str
    description: str
    units: List[DeployUnit]
    route: Optional[Dict[str, Any]] = None  # {"name":..., "targets":[{name,weight}]}
    yaml_preview: str = ""
    warnings: List[str] = []


# ---------------------------------------------------------------- 引擎互连参数

def _engine_role_parameters(
    backend: Optional[str], role: str, counterpart_port_hint: str
) -> List[str]:
    """按引擎生成 P/D 角色互连参数 (二开修复).

    原版问题: vLLM 分支用了 SGLang 的 --server-type (且带 api- 前缀,
    两边都不存在); SGLang 分支的 --disaggregation-tp-size 没带值.

    - SGLang: --disaggregation-mode prefill|decode (官方 launch_server 语法)
    - vLLM:   --kv-transfer-config (kv_producer / kv_consumer, 官方 PD 语法)
    """
    b = (backend or "vllm").lower()
    if b == "sglang":
        mode = "prefill" if role == "prefill" else "decode"
        return [f"--disaggregation-mode={mode}"]
    # vLLM 形式 (kv-transfer)
    if role == "prefill":
        return [
            "--kv-transfer-config",
            '{"kv_connector":"PyNcclConnector","kv_role":"kv_producer",'
            '"kv_rank":0,"kv_parallel_size":2}',
        ]
    return [
        "--kv-transfer-config",
        '{"kv_connector":"PyNcclConnector","kv_role":"kv_consumer",'
        '"kv_rank":1,"kv_parallel_size":2}',
    ]


SHAPE_DESCRIPTIONS = {
    DeployTopologyRequest.ShapeEnum.SINGLE_NODE_TP: (
        "单机多卡 TP — 一个实例独占同节点多张 GPU, 张量并行; 可配多实例."
    ),
    DeployTopologyRequest.ShapeEnum.PP_MULTINODE: (
        "多机流水线 — 指定 N 个节点, 每节点一个 PP stage, "
        "stage 内可配 TP; 放不下单机的模型跨节点流水执行."
    ),
    DeployTopologyRequest.ShapeEnum.PD_DISAGGREGATED: (
        "PD 分离 — Prefill 与 Decode 独立成组, ModelRoute 聚合对外, "
        "KV 由引擎传输层搬运; 组数 >1 即多 P 多 D."
    ),
    DeployTopologyRequest.ShapeEnum.CUSTOM: (
        "自定义拓扑 — 每个角色独立配置节点/副本/GPU/参数/环境变量."
    ),
}


def _base_fields(req: DeployTopologyRequest) -> Dict[str, Any]:
    """ModelCreate 公共字段 (二开修复: 原版塞了不存在的 model_source 字段,
    且 source 枚举值 hugging_face 错误 — 正确值是 huggingface)."""
    source = req.source or "huggingface"
    d: Dict[str, Any] = {
        "source": source,
        "replicas": 1,
        "backend_parameters": list(req.backend_parameters or []),
        "distributed_inference_across_workers": False,
    }
    # repo id 落到与 source 匹配的字段 (ModelCreate 无 model_source 字段)
    if source == "model_scope":
        d["model_scope_model_id"] = req.model_source
    elif source == "local_path":
        d["local_path"] = req.model_source
    else:
        d["huggingface_repo_id"] = req.model_source
    if req.backend:
        d["backend"] = req.backend
    if req.cluster_id is not None:
        d["cluster_id"] = req.cluster_id
    if req.env:
        d["env"] = dict(req.env)
    return d


def _gpu_selector(gpu_ids: Optional[List[str]]) -> Dict[str, Any]:
    if not gpu_ids:
        return {}
    return {"gpu_selector": {"gpu_ids": list(gpu_ids)}}


def _merge_params(payload: Dict[str, Any], extra: List[str]) -> None:
    existing = payload.get("backend_parameters") or []
    existing.extend(extra)
    payload["backend_parameters"] = existing


def _unit_from_payload(
    role: str, payload: Dict[str, Any], gpu_ids: Optional[List[str]],
    distributed: bool, ports: List[int], hint: str, deps: List[str]
) -> DeployUnit:
    return DeployUnit(
        role=role,
        name=payload["name"],
        replicas=payload.get("replicas", 1),
        gpu_ids=gpu_ids,
        gpus_per_replica=(payload.get("gpu_selector") or {}).get("gpus_per_replica"),
        distributed=distributed,
        backend_parameters=payload.get("backend_parameters") or [],
        env=payload.get("env") or {},
        ports=ports,
        start_command_hint=hint,
        depends_on=deps,
    )


def build_topology_plan(req: DeployTopologyRequest) -> DeployTopologyPlan:
    """把形态请求展开为部署计划 (纯函数, plan/deploy 共用)."""
    shape = req.shape
    plan = DeployTopologyPlan(
        shape=shape.value,
        description=SHAPE_DESCRIPTIONS[shape],
        units=[],
        warnings=[],
    )

    if shape == DeployTopologyRequest.ShapeEnum.SINGLE_NODE_TP:
        p = _base_fields(req)
        p["name"] = req.model_name
        p["replicas"] = req.replicas
        p.update(_gpu_selector(req.gpu_ids))
        if req.gpus_per_replica:
            p.setdefault("gpu_selector", {})["gpus_per_replica"] = (
                req.gpus_per_replica
            )
        if req.gpu_ids and len(set(g.split(":")[0] for g in req.gpu_ids)) > 1:
            plan.warnings.append(
                "gpu_ids 跨越多个节点, 将自动转为跨节点分布式部署"
            )
            p["distributed_inference_across_workers"] = True
        plan.units.append(_unit_from_payload(
            "standalone", p, req.gpu_ids,
            p["distributed_inference_across_workers"], [8080],
            "vllm serve <model> --port 8080 --tensor-parallel-size=<GPUs>", [],
        ))
        plan.yaml_preview = _yaml_of(plan)
        return plan

    if shape == DeployTopologyRequest.ShapeEnum.PP_MULTINODE:
        node_gpus = req.pp_node_gpu_ids or []
        n = len(node_gpus)
        if n < 2:
            raise ValueError("多机流水线至少需要 2 个节点 (每节点 1 个 PP stage)")
        flat = [g for node in node_gpus for g in node]
        tp = req.pp_tp_per_stage or (
            max((len(node) for node in node_gpus), default=1)
        )
        p = _base_fields(req)
        p["name"] = req.model_name
        p["distributed_inference_across_workers"] = True
        p.update(_gpu_selector(flat))
        p.setdefault("gpu_selector", {})["gpus_per_replica"] = tp
        _merge_params(p, [
            "--pipeline-parallel-size=" + str(n),
            "--tensor-parallel-size=" + str(tp),
        ])
        plan.units.append(_unit_from_payload(
            "pipeline", p, flat, True,
            [8080] + [9000 + i for i in range(n - 1)],
            f"vllm serve <model> --pipeline-parallel-size={n} "
            f"--tensor-parallel-size={tp} (ray/mp executor 自动组网)",
            [],
        ))
        if n < 2:
            plan.warnings.append("流水线并行度 < 2 无意义, 建议直接单机")
        plan.yaml_preview = _yaml_of(plan)
        return plan

    if shape == DeployTopologyRequest.ShapeEnum.PD_DISAGGREGATED:
        # P 组 (replicas 即组数, >1 = 多 P 多 D)
        pp_ = _base_fields(req)
        pp_["name"] = f"{req.model_name}-prefill"
        pp_["replicas"] = req.prefill_replicas
        pp_.update(_gpu_selector(req.prefill_gpu_ids))
        if req.prefill_gpus_per_replica:
            pp_.setdefault("gpu_selector", {})["gpus_per_replica"] = (
                req.prefill_gpus_per_replica
            )
        _merge_params(
            pp_, _engine_role_parameters(req.backend, "prefill", "kv")
        )
        plan.units.append(_unit_from_payload(
            "prefill", pp_, req.prefill_gpu_ids, False, [8080],
            "引擎以 prefill 角色启动 (KV producer)", [],
        ))
        # D 组
        dd = _base_fields(req)
        dd["name"] = f"{req.model_name}-decode"
        dd["replicas"] = req.decode_replicas
        dd.update(_gpu_selector(req.decode_gpu_ids))
        if req.decode_gpus_per_replica:
            dd.setdefault("gpu_selector", {})["gpus_per_replica"] = (
                req.decode_gpus_per_replica
            )
        _merge_params(
            dd, _engine_role_parameters(req.backend, "decode", "kv")
        )
        plan.units.append(_unit_from_payload(
            "decode", dd, req.decode_gpu_ids, False, [8080],
            "引擎以 decode 角色启动 (KV consumer), 依赖 prefill 组",
            [f"{req.model_name}-prefill"],
        ))
        # 路由聚合: targets 生成 model 名 -> 部署后由路由层解析为
        # ModelRouteTargetUpdateItem (model_id/weight). plan 阶段模型
        # 尚未创建, 没有 id — 存名字, deploy 端点负责换 id.
        if req.create_route:
            plan.route = {
                "name": req.route_name or req.model_name,
                "target_model_names": [
                    f"{req.model_name}-prefill",
                    f"{req.model_name}-decode",
                ],
                "targets": [],  # deploy 时填充 (model_id 形式)
            }
        if req.backend and req.backend.lower() == "vllm":
            plan.warnings.append(
                "vLLM PD 需 >=0.9 (kv-transfer); SGLang 用 disaggregated 模式更成熟"
            )
        plan.yaml_preview = _yaml_of(plan)
        return plan

    if shape == DeployTopologyRequest.ShapeEnum.CUSTOM:
        if not req.roles:
            raise ValueError("自定义拓扑需要提供 roles 列表")
        for r in req.roles:
            p = _base_fields(req)
            p["name"] = f"{req.model_name}-{r.role}"
            p["replicas"] = r.replicas
            p.update(_gpu_selector(r.gpu_ids))
            if r.gpus_per_replica:
                p.setdefault("gpu_selector", {})["gpus_per_replica"] = (
                    r.gpus_per_replica
                )
            if r.backend_parameters:
                _merge_params(p, r.backend_parameters)
            if r.env:
                p["env"] = {**(p.get("env") or {}), **r.env}
            cross = r.gpu_ids and len(
                set(g.split(":")[0] for g in r.gpu_ids)
            ) > 1
            if cross:
                p["distributed_inference_across_workers"] = True
            plan.units.append(_unit_from_payload(
                r.role, p, r.gpu_ids, bool(cross), [8080],
                f"角色 {r.role} 实例", [],
            ))
        if req.create_route and len(plan.units) > 1:
            plan.route = {
                "name": req.route_name or req.model_name,
                "target_model_names": [u.name for u in plan.units],
                "targets": [],  # deploy 时填充 (model_id 形式)
            }
        plan.yaml_preview = _yaml_of(plan)
        return plan

    raise ValueError(f"未知形态: {shape}")


def _yaml_of(plan: DeployTopologyPlan) -> str:
    """生成 YAML 预览 (部署计划的等价描述, 供向导预览用)."""
    lines = [f"# 部署计划 — {plan.shape}", f"# {plan.description}"]
    for u in plan.units:
        lines += [
            f"unit {u.name}:",
            f"  role: {u.role}",
            f"  replicas: {u.replicas}",
        ]
        if u.gpu_ids:
            lines.append(f"  gpus: [{', '.join(u.gpu_ids)}]")
        if u.gpus_per_replica:
            lines.append(f"  gpus_per_replica: {u.gpus_per_replica}")
        if u.distributed:
            lines.append("  distributed_across_workers: true")
        if u.backend_parameters:
            lines.append("  backend_parameters:")
            lines += [f"    - {x}" for x in u.backend_parameters]
        if u.env:
            lines.append("  env:")
            lines += [f"    {k}: {v}" for k, v in u.env.items()]
        if u.depends_on:
            lines.append(f"  depends_on: [{', '.join(u.depends_on)}]")
    if plan.route:
        lines += [
            f"route {plan.route['name']}:",
        ] + [
            f"  - {name} (weight=1)"
            for name in plan.route.get("target_model_names", [])
        ]
    if plan.warnings:
        lines += ["warnings:"] + [f"  - {w}" for w in plan.warnings]
    return "\n".join(lines)