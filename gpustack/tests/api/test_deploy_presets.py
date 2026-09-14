"""Unit tests for the 二开 deployment preset/topology expansion logic.

Covers the P0/P1 fixes:
  - deploy-presets payloads validate against ModelCreate (no phantom
    ``model_source`` field; ``source`` + repo-id fields land correctly)
  - PD 互连参数按引擎生成且 producer/consumer 对称 (vLLM kv-transfer /
    SGLang disaggregation-mode), 不再产出引擎不识别的 --api-server-type
  - 拓扑图 edges 只指向真实存在的节点 (无悬空边)
"""

import pytest

from gpustack.schemas.deploy_presets import (
    DeploymentArchitectureEnum,
    PresetDeployRequest,
    build_preset_payloads,
    build_topology_graph,
)
from gpustack.schemas.deploy_topologies import (
    DeployTopologyRequest,
)
from gpustack.schemas.deploy_topologies import build_topology_plan
from gpustack.schemas.models import ModelCreate


# ---------------------------------------------------------------- helpers

def _preset_req(**kw):
    base = {
        "architecture": DeploymentArchitectureEnum.PD_DISAGGREGATED,
        "model_name": "testmodel",
        "model_source": "Qwen/Qwen2.5-0.5B-Instruct",
    }
    base.update(kw)
    return PresetDeployRequest(**base)


def _topo_req(**kw):
    base = {
        "shape": "pd_disaggregated",
        "model_name": "t1",
        "model_source": "Qwen/Qwen2.5-0.5B-Instruct",
    }
    base.update(kw)
    return DeployTopologyRequest(**base)


def _flat_params(payload):
    return " ".join(payload.get("backend_parameters") or [])


def _preset_plan(arch, **kw):
    """Build a preset plan for any architecture with valid defaults."""
    if arch == DeploymentArchitectureEnum.CUSTOM:
        kw.setdefault(
            "custom_topology",
            {"roles": [{
                "role": "worker", "replicas": 1,
                "nodes_per_replica": 1, "gpus_per_node": 1,
            }]},
        )
    return build_preset_payloads(_preset_req(architecture=arch, **kw))


# ---------------------------------------------------------------- presets

def test_preset_payloads_validate_against_modelcreate():
    """每个 payload 必须能直接构造 ModelCreate (deploy 端点的硬前提)."""
    for arch in DeploymentArchitectureEnum:
        plan = _preset_plan(arch)
        assert plan.payloads, f"{arch}: no payloads"
        for payload in plan.payloads:
            # raises pydantic ValidationError on phantom fields / bad types
            ModelCreate(**payload)


def test_preset_source_fields_by_kind():
    plan = _preset_plan(
        DeploymentArchitectureEnum.STANDALONE,
        model_source_kind="model_scope",
        model_source="Qwen/Qwen2.5-0.5B-Instruct",
    )
    payload = plan.payloads[0]
    assert payload["source"] == "model_scope"
    assert payload["model_scope_model_id"] == "Qwen/Qwen2.5-0.5B-Instruct"

    plan2 = _preset_plan(
        DeploymentArchitectureEnum.STANDALONE,
        model_source_kind="local_path",
        model_source="/data/models/qwen",
    )
    payload2 = plan2.payloads[0]
    assert payload2["source"] == "local_path"
    assert payload2["local_path"] == "/data/models/qwen"


def test_pd_kv_flags_symmetric():
    plan = _preset_plan(
        DeploymentArchitectureEnum.PD_DISAGGREGATED, kv_transfer=True
    )
    by_name = {p["name"]: p for p in plan.payloads}
    p_params = _flat_params(by_name["testmodel-prefill"])
    d_params = _flat_params(by_name["testmodel-decode"])
    assert "kv_producer" in p_params
    assert "kv_consumer" in d_params
    # producer 与 consumer 都带完整 kv-transfer-config
    assert "--kv-transfer-config" in p_params
    assert "--kv-transfer-config" in d_params
    # 引擎不识别的参数不允许出现
    for params in (p_params, d_params):
        assert "api-server-type" not in params
        assert "server-type" not in params


def test_multi_pd_merged_into_pd_disaggregated():
    """多 P 多 D 已合并进 PD 分离: 组数通过 prefill_groups/decode_groups
    表达, 不再有独立的 multi_pd 架构."""
    assert not hasattr(DeploymentArchitectureEnum, "MULTI_PD")
    plan = _preset_plan(
        DeploymentArchitectureEnum.PD_DISAGGREGATED,
        prefill_groups=2, decode_groups=3, kv_transfer=True,
    )
    by_name = {p["name"]: p for p in plan.payloads}
    assert by_name["testmodel-prefill"]["replicas"] == 2
    assert by_name["testmodel-decode"]["replicas"] == 3
    assert "kv_producer" in _flat_params(by_name["testmodel-prefill"])
    assert "kv_consumer" in _flat_params(by_name["testmodel-decode"])


def test_pd_node_assign_one_rank_one_node():
    """1P1D 单机: 指定哪个节点是 P 哪个是 D — 每 rank 一个 Model,
    gpu_ids 钉死节点."""
    plan = _preset_plan(
        DeploymentArchitectureEnum.PD_DISAGGREGATED,
        prefill_groups=1, decode_groups=1,
        prefill_gpu_count=2, decode_gpu_count=2,
        pd_node_assign={"prefill": [["node-a"]],
                        "decode": [["node-b"]]},
    )
    assert len(plan.payloads) == 2
    by_name = {p["name"]: p for p in plan.payloads}
    p = by_name["testmodel-prefill-0"]
    d = by_name["testmodel-decode-0"]
    assert p["gpu_selector"]["gpu_ids"] == [
        "node-a:cuda:0", "node-a:cuda:1"]
    assert d["gpu_selector"]["gpu_ids"] == [
        "node-b:cuda:0", "node-b:cuda:1"]
    # 1P1D 单机: 非分布式
    assert not p["distributed_inference_across_workers"]


def test_pd_node_assign_multi_rank_multi_node_pp():
    """多P多D + PP=2: 每 rank 两台节点, 分布式 + PP 参数."""
    plan = _preset_plan(
        DeploymentArchitectureEnum.PD_DISAGGREGATED,
        prefill_groups=2, decode_groups=2,
        prefill_gpu_count=1, decode_gpu_count=1,
        pd_pipeline_size=2,
        pd_node_assign={
            "prefill": [["n1", "n2"], ["n3", "n4"]],
            "decode": [["n5", "n6"], ["n7", "n8"]],
        },
    )
    assert len(plan.payloads) == 4
    by_name = {p["name"]: p for p in plan.payloads}
    p0 = by_name["testmodel-prefill-0"]
    assert p0["gpu_selector"]["gpu_ids"] == ["n1:cuda:0", "n2:cuda:0"]
    assert p0["distributed_inference_across_workers"] is True
    assert "--pipeline-parallel-size=2" in _flat_params(p0)
    d1 = by_name["testmodel-decode-1"]
    assert d1["gpu_selector"]["gpu_ids"] == ["n7:cuda:0", "n8:cuda:0"]


def test_pd_node_assign_rejects_incomplete():
    """防浪费: rank 节点数 != PP 或缺节点 -> 直接 ValueError (UI 同步红字)."""
    with pytest.raises(ValueError, match="未指定节点"):
        _preset_plan(
            DeploymentArchitectureEnum.PD_DISAGGREGATED,
            pd_node_assign={"prefill": [[]], "decode": [["n-b"]]},
        )
    with pytest.raises(ValueError, match="需要 2 个节点"):
        _preset_plan(
            DeploymentArchitectureEnum.PD_DISAGGREGATED,
            pd_pipeline_size=2,
            pd_node_assign={"prefill": [["n-a"]], "decode": [["n-b"]]},
        )


def test_pd_node_assign_rejects_duplicate_nodes():
    """互斥: 同一节点不得被两个 rank (P/D 任意组合) 重复认领 —
    UI 端已置灰已选节点, 后端双保险拒绝."""
    # P rank0 与 D rank0 撞同一台节点
    with pytest.raises(ValueError, match="不能重叠"):
        _preset_plan(
            DeploymentArchitectureEnum.PD_DISAGGREGATED,
            pd_node_assign={"prefill": [["node-a"]],
                            "decode": [["node-a"]]},
        )
    # 多 P: prefill rank0 与 rank1 撞同一台节点
    with pytest.raises(ValueError, match="node-x"):
        _preset_plan(
            DeploymentArchitectureEnum.PD_DISAGGREGATED,
            prefill_groups=2,
            pd_node_assign={"prefill": [["node-x"], ["node-x"]],
                            "decode": [["node-y"]]},
        )
    # PP>1 时同 rank 内节点也不重复 (重复节点同样是撞车)
    with pytest.raises(ValueError, match="不能重叠"):
        _preset_plan(
            DeploymentArchitectureEnum.PD_DISAGGREGATED,
            pd_pipeline_size=2,
            pd_node_assign={"prefill": [["n-a", "n-a"]],
                            "decode": [["n-b", "n-c"]]},
        )


def test_pd_no_kv_transfer_by_default():
    plan = _preset_plan(DeploymentArchitectureEnum.PD_DISAGGREGATED)
    for p in plan.payloads:
        assert "kv-transfer-config" not in _flat_params(p)


def test_pd_sglang_disaggregation_mode():
    plan = _preset_plan(
        DeploymentArchitectureEnum.PD_DISAGGREGATED,
        backend="sglang", kv_transfer=True,
    )
    by_name = {p["name"]: p for p in plan.payloads}
    assert "--disaggregation-mode=prefill" in _flat_params(
        by_name["testmodel-prefill"]
    )
    assert "--disaggregation-mode=decode" in _flat_params(
        by_name["testmodel-decode"]
    )


def test_topology_graph_edges_resolve_to_nodes():
    """edges 的 from/to 必须都在节点集里 (前端 SVG 才能连线)."""
    for arch in DeploymentArchitectureEnum:
        if arch == DeploymentArchitectureEnum.CUSTOM:
            plan = _preset_plan(
                arch,
                custom_topology={"roles": [
                    {"role": "prefill", "replicas": 1},
                    {"role": "decode", "replicas": 1,
                     "depends_on": ["prefill"]},
                ]},
            )
        else:
            plan = _preset_plan(arch)
        graph = build_topology_graph(plan)
        ids = {n["id"] for n in graph["nodes"]}
        assert ids, f"{arch}: no nodes"
        for edge in graph["edges"]:
            assert edge["from"] in ids, f"{arch}: dangling from {edge}"
            assert edge["to"] in ids, f"{arch}: dangling to {edge}"


def test_pipeline_parallel_single_distributed_payload():
    plan = _preset_plan(
        DeploymentArchitectureEnum.PIPELINE_PARALLEL,
        pipeline_parallel_size=4,
        tensor_parallel_size=2,
    )
    assert len(plan.payloads) == 1
    payload = plan.payloads[0]
    assert payload["distributed_inference_across_workers"] is True
    assert "--pipeline-parallel-size=4" in _flat_params(payload)
    assert "--tensor-parallel-size=2" in _flat_params(payload)
    assert len(plan.roles) == 4  # N 个 stage 角色


def test_pipeline_parallel_manual_gpu_selection():
    """PP 手动选卡: gpus_per_replica = 跨节点总卡数 (PP×TP) —
    调度器按总预算跨节点分配, 引擎侧按每节点卡数=TP 拆分."""
    plan = _preset_plan(
        DeploymentArchitectureEnum.PIPELINE_PARALLEL,
        pipeline_parallel_size=2,
        tensor_parallel_size=8,
        gpu_ids=[
            "n1:cuda:0", "n1:cuda:1", "n1:cuda:2", "n1:cuda:3",
            "n1:cuda:4", "n1:cuda:5", "n1:cuda:6", "n1:cuda:7",
            "n2:cuda:0", "n2:cuda:1", "n2:cuda:2", "n2:cuda:3",
            "n2:cuda:4", "n2:cuda:5", "n2:cuda:6", "n2:cuda:7",
        ],
    )
    payload = plan.payloads[0]
    assert payload["gpu_selector"]["gpus_per_replica"] == 16  # 总量, 非 8
    assert len(payload["gpu_selector"]["gpu_ids"]) == 16
    assert "--pipeline-parallel-size=2" in _flat_params(payload)
    assert "--tensor-parallel-size=8" in _flat_params(payload)


def test_pipeline_parallel_manual_gpu_mismatch_rejected():
    """选卡数/节点数与 PP×TP 不匹配 -> 显式报错, 而不是静默产出
    world_size 校验必炸的 payload."""
    # 卡数不够 PP×TP
    with pytest.raises(ValueError, match="不匹配"):
        _preset_plan(
            DeploymentArchitectureEnum.PIPELINE_PARALLEL,
            pipeline_parallel_size=2,
            tensor_parallel_size=8,
            gpu_ids=["n1:cuda:0"] * 8 + ["n2:cuda:0"] * 4,
        )
    # 节点数不等于 PP
    with pytest.raises(ValueError, match="流水线并行度"):
        _preset_plan(
            DeploymentArchitectureEnum.PIPELINE_PARALLEL,
            pipeline_parallel_size=2,
            tensor_parallel_size=4,
            gpu_ids=[
                "n1:cuda:0", "n1:cuda:1", "n1:cuda:2", "n1:cuda:3",
                "n1:cuda:4", "n1:cuda:5", "n1:cuda:6", "n1:cuda:7",
            ],
        )
    # 每节点卡数不等 (TP 拆不均) — 总量正确 (8=2×4) 但 n1/n2 分配 3/5
    with pytest.raises(ValueError, match="选卡数不等"):
        _preset_plan(
            DeploymentArchitectureEnum.PIPELINE_PARALLEL,
            pipeline_parallel_size=2,
            tensor_parallel_size=4,
            gpu_ids=[
                "n1:cuda:0", "n1:cuda:1", "n1:cuda:2",
                "n2:cuda:0", "n2:cuda:1", "n2:cuda:2", "n2:cuda:3",
                "n2:cuda:4",
            ],
        )


def test_pd_node_assign_multinode_gpus_per_replica_includes_pp():
    """PD 跨节点 (pd_pipeline_size>1): gpus_per_replica 必须是总卡数
    (gpu_count×pp), 写单节点数会让调度器 world_size 校验失败."""
    plan = _preset_plan(
        DeploymentArchitectureEnum.PD_DISAGGREGATED,
        prefill_gpu_count=4, decode_gpu_count=4,
        pd_pipeline_size=2,
        pd_node_assign={
            "prefill": [["n1", "n2"]],
            "decode": [["n3", "n4"]],
        },
    )
    by_name = {p["name"]: p for p in plan.payloads}
    p = by_name["testmodel-prefill-0"]
    assert p["gpu_selector"]["gpus_per_replica"] == 8  # 4/节点 × 2 节点
    assert len(p["gpu_selector"]["gpu_ids"]) == 8
    assert "--tensor-parallel-size=4" in _flat_params(p)  # TP=每节点数
    assert "--pipeline-parallel-size=2" in _flat_params(p)


def test_standalone_tp_flag_only_when_needed():
    plan = _preset_plan(
        DeploymentArchitectureEnum.STANDALONE,
        tensor_parallel_size=1,
        replicas=2,
    )
    payload = plan.payloads[0]
    assert payload["replicas"] == 2
    assert "--tensor-parallel-size" not in _flat_params(payload)


def test_standalone_single_payload_no_dup_ternary():
    """PD 分离 = 恰好 2 个 payload (prefill + decode), 修掉的同义三元
    表达式曾暗示多余分支."""
    plan = _preset_plan(DeploymentArchitectureEnum.PD_DISAGGREGATED)
    assert len(plan.payloads) == 2
    assert {p["name"] for p in plan.payloads} == {
        "testmodel-prefill", "testmodel-decode",
    }


# ------------------------------------------------------------ topologies

def test_topology_units_validate_against_modelcreate():
    """部署单元的字段必须能构造 ModelCreate (deploy 端点 P0 修复)."""
    for shape in ("single_node_tp", "pd_disaggregated"):
        req = _topo_req(shape=shape)
        plan = build_topology_plan(req)
        assert plan.units, f"{shape}: no units"
        for unit in plan.units:
            # 与 deploy 端点相同的组装逻辑
            source = req.source or "huggingface"
            payload = {
                "name": unit.name,
                "replicas": unit.replicas,
                "backend_parameters": unit.backend_parameters,
                "env": unit.env or None,
                "source": source,
            }
            if source == "model_scope":
                payload["model_scope_model_id"] = req.model_source
            elif source == "local_path":
                payload["local_path"] = req.model_source
            else:
                payload["huggingface_repo_id"] = req.model_source
            ModelCreate(**payload)


def test_topology_pd_engine_parameters():
    plan = build_topology_plan(_topo_req())
    by_role = {u.role: u for u in plan.units}
    p_flat = " ".join(by_role["prefill"].backend_parameters)
    d_flat = " ".join(by_role["decode"].backend_parameters)
    assert "kv_producer" in p_flat
    assert "kv_consumer" in d_flat
    assert "server-type" not in p_flat + d_flat


def test_topology_sglang_engine_parameters():
    plan = build_topology_plan(_topo_req(backend="sglang"))
    by_role = {u.role: u for u in plan.units}
    assert "--disaggregation-mode=prefill" in " ".join(
        by_role["prefill"].backend_parameters
    )
    assert "--disaggregation-mode=decode" in " ".join(
        by_role["decode"].backend_parameters
    )


def test_topology_route_uses_target_model_names():
    """route 计划不再存 {name, weight} (ModelRouteCreate 不接受)."""
    plan = build_topology_plan(_topo_req(create_route=True))
    assert plan.route is not None
    assert plan.route["targets"] == []
    assert set(plan.route["target_model_names"]) == {
        "t1-prefill", "t1-decode",
    }


def test_topology_yaml_preview_renders():
    plan = build_topology_plan(_topo_req())
    assert "unit t1-prefill" in plan.yaml_preview
    assert "route t1" in plan.yaml_preview
