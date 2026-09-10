"""Unit tests for the serving-topology planner (three-level hierarchy).

Covers the conversion red lines from the spec:
  - 单组占机数 = ceil(TP×PP×EP / 单节点GPU数)
  - 可形成组数 = floor(分配机数 / 单组占机数)
  - 余量必须显式提示, 禁止静默忽略或把闲置机计入组数
  - 声明式与资源池式两种交互结果一致
  - DP 复用默认关闭, 显式开启后单机多实例
  - PP 嵌套在 P/D 组内部 (PD 分离是外层服务属性)
"""

import pytest

from gpustack.schemas.deploy_topology import (
    GroupParallelStrategy,
    NodePool,
    PDServiceSpec,
    ResourceTopologyEnum,
    build_service_plan,
    plan_groups,
    suggest_topology_upgrade,
)
from gpustack.routes.serving_topology import (
    TopologyPlanRequest,
    _build_full_plan,
)
from gpustack.schemas.models import ModelCreate


def strat(tp=1, pp=1, ep=1, dp=1):
    return GroupParallelStrategy(
        tensor_parallel_size=tp,
        pipeline_parallel_size=pp,
        expert_parallel_size=ep,
        data_parallel_replicas=dp,
    )


def pool(machines, gpus_per_node=8, vram_per_gpu_mb=None):
    return NodePool(
        machines_allocated=machines,
        gpus_per_node=gpus_per_node,
        vram_per_gpu_mb=vram_per_gpu_mb,
    )


# ------------------------------------------------- spec table (8 GPUs/node)

SPEC_TABLE = [
    # (machines, tp, pp, ep, machines_per_group, groups, idle)
    (3, 1, 2, 1, 1, 3, 0),    # 2 GPUs/group -> 3 groups
    (3, 8, 2, 1, 2, 1, 1),    # 16 GPUs/group -> 1 group + 1 idle
    (4, 8, 2, 1, 2, 2, 0),    # 4 machines -> exactly 2 groups
    (5, 8, 2, 1, 2, 2, 1),    # 5 machines -> 2 groups + 1 idle
    (2, 8, 2, 1, 2, 1, 0),    # 2 machines -> exactly 1 group
    (3, 8, 3, 1, 3, 1, 0),    # 24 GPUs/group spans 3 machines
]


@pytest.mark.parametrize(
    "machines,tp,pp,ep,mpg,groups,idle",
    SPEC_TABLE,
)
def test_spec_conversion_table(machines, tp, pp, ep, mpg, groups, idle):
    """The mandatory examples from the spec must hold exactly."""
    s = plan_groups(strat(tp, pp, ep), pool(machines), "resource_pool")
    assert s.machines_per_group == mpg
    assert s.groups_formed == groups
    assert s.machines_idle == idle
    assert s.cross_machine == (tp * pp * ep > 8)
    # 闲置机绝不计入组数
    assert s.machines_used + s.machines_idle == machines


def test_single_machine_vs_cross_machine_pp():
    """Same-machine PP (fits one node) vs cross-machine PP must be
    distinguished by the planner, not left ambiguous."""
    local = plan_groups(strat(pp=2), pool(1), "resource_pool")
    assert local.cross_machine is False
    assert local.machines_per_group == 1

    cross = plan_groups(strat(tp=8, pp=2), pool(2), "resource_pool")
    assert cross.cross_machine is True
    assert cross.machines_per_group == 2


def test_heterogeneous_gpu_profile():
    """The conversion adapts to the node profile — 4 GPUs per node
    with a 16-GPU group spans 4 machines, not 2."""
    s = plan_groups(strat(tp=8, pp=2), pool(4, gpus_per_node=4),
                    "resource_pool")
    assert s.machines_per_group == 4
    assert s.groups_formed == 1
    assert s.machines_idle == 0


# ---------------------------------------------------- interaction parity

def test_declarative_matches_resource_pool():
    """Both interaction modes converge on the same physical outcome."""
    a = plan_groups(strat(8, 2), pool(4), "resource_pool")
    b = plan_groups(strat(8, 2), pool(4), "declarative",
                    groups_requested=2)
    assert a.groups_formed == b.groups_formed == 2
    assert a.machines_used == b.machines_used == 4
    assert a.machines_idle == b.machines_idle == 0
    assert a.machines_per_group == b.machines_per_group == 2


def test_declarative_shortfall_reports_required_machines():
    s = plan_groups(strat(8, 2), pool(3), "declarative",
                    groups_requested=2)
    assert s.insufficient is True
    assert s.machines_required == 4
    assert "4 台" in s.notice


# ------------------------------------------------------------- DP reuse

def test_dp_off_by_default():
    """DP reuse is opt-in: without it one machine runs one instance and
    the leftover GPUs are reported idle, never silently packed."""
    s = plan_groups(strat(pp=2), pool(1), "resource_pool")
    assert s.groups_formed == 1
    assert s.instances_per_machine == 1
    assert s.gpus_idle == 6


def test_dp_reuse_packs_instances_per_machine():
    """With DP on, the machine holds multiple same-strategy replicas;
    the deployment-unit (Model row) count stays 1 and carries the
    replica count."""
    s = plan_groups(strat(pp=2, dp=4), pool(1), "resource_pool")
    assert s.groups_formed == 1
    assert s.instances_per_machine == 4
    assert s.gpus_used == 8
    assert s.gpus_idle == 0


# ------------------------------------------------- PD nesting (the rule)

def test_pp_nested_inside_pd_groups():
    """2P2D with TP=8,PP=2 per group (cross-machine inside the group):
    each side needs 4 machines — 8 total. PP never sits above PD."""
    pd = PDServiceSpec(
        enabled=True,
        prefill_groups=2,
        decode_groups=2,
        prefill_parallel=strat(8, 2),
        decode_parallel=strat(8, 2),
        prefill_pool=pool(4),
        decode_pool=pool(4),
    )
    plan = build_service_plan(ResourceTopologyEnum.MULTI_NODE, pd)
    assert plan.prefill.groups_formed == 2
    assert plan.decode.groups_formed == 2
    assert plan.prefill.machines_used == 4
    assert plan.decode.machines_used == 4
    assert plan.prefill.cross_machine and plan.decode.cross_machine
    assert plan.errors == []


def test_pd_heterogeneous_pp_per_side():
    """P and D sides may use different PP degrees — prefill PP=2 while
    decode PP=4 is a valid heterogeneous layout."""
    pd = PDServiceSpec(
        enabled=True,
        prefill_groups=1,
        decode_groups=1,
        prefill_parallel=strat(8, 2),   # 16 GPUs -> 2 machines
        decode_parallel=strat(8, 4),    # 32 GPUs -> 4 machines
        prefill_pool=pool(2),
        decode_pool=pool(4),
    )
    plan = build_service_plan(ResourceTopologyEnum.MULTI_NODE, pd)
    assert plan.prefill.machines_per_group == 2
    assert plan.decode.machines_per_group == 4
    assert plan.errors == []


def test_pd_default_1p1d():
    """PD disaggregation defaults to 1P1D — N=1, M=1 is just the
    default, not a separate mode."""
    pd = PDServiceSpec(enabled=True)
    assert pd.prefill_groups == 1
    assert pd.decode_groups == 1


def test_pd_idle_machines_surfaced():
    """3 machines for P with 16-GPU groups -> 1 group + 1 idle, and
    the notice says why the second group cannot form."""
    pd = PDServiceSpec(
        enabled=True,
        prefill_groups=1,
        decode_groups=1,
        prefill_parallel=strat(8, 2),
        decode_parallel=strat(1),
        prefill_pool=pool(3),
        decode_pool=pool(1),
    )
    plan = build_service_plan(ResourceTopologyEnum.MULTI_NODE, pd)
    assert plan.prefill.groups_formed == 1
    assert plan.prefill.machines_idle == 1
    assert "不足以组成第 2 组" in plan.prefill.notice


def test_pd_three_machines_small_groups():
    """3 machines for P with 2-GPU groups -> exactly 3 P groups."""
    pd = PDServiceSpec(
        enabled=True,
        prefill_groups=3,
        decode_groups=1,
        prefill_parallel=strat(pp=2),
        decode_parallel=strat(1),
        prefill_pool=pool(3),
        decode_pool=pool(1),
    )
    plan = build_service_plan(ResourceTopologyEnum.MULTI_NODE, pd)
    assert plan.prefill.groups_formed == 3
    assert plan.prefill.machines_idle == 0
    assert plan.errors == []


# ---------------------------------------------------- level-1 + VRAM

def test_single_node_upgrade_hint_preserves_strategy():
    """When the instance outgrows one node the hint suggests the
    multi-node topology and the strategy itself is untouched."""
    hint = suggest_topology_upgrade(strat(8, 2), pool(1, 8))
    assert hint is not None
    assert "多机部署" in hint
    assert "跨 2 台" in hint
    assert "保持不变" in hint


def test_no_upgrade_hint_when_fits():
    assert suggest_topology_upgrade(strat(2), pool(1, 8)) is None


def test_vram_shortfall_flagged():
    pd = PDServiceSpec(
        enabled=False,
        prefill_parallel=strat(8),
        prefill_pool=pool(1, 8, vram_per_gpu_mb=24576),
    )
    plan = build_service_plan(
        ResourceTopologyEnum.SINGLE_NODE, pd, per_instance_vram_mb=204800
    )
    assert plan.single.vram_fit is False
    assert plan.single.vram_notice


# ------------------------------------------------------- payload expansion

def _req(**kw):
    base = dict(
        model_name="svc",
        model_source="Qwen/Qwen2.5-0.5B-Instruct",
        topology="multi_node",
        mode="resource_pool",
        enabled=False,
        prefill_parallel={"tensor_parallel_size": 2},
        prefill_pool={"machines_allocated": 1, "gpus_per_node": 8},
    )
    base.update(kw)
    return TopologyPlanRequest.model_validate(base)


def test_plan_payloads_validate_against_modelcreate():
    """Every expanded payload must construct a ModelCreate — the
    deploy endpoint's hard precondition."""
    req = _req(
        enabled=True,
        prefill_groups=2,
        decode_groups=2,
        prefill_parallel={"tensor_parallel_size": 8,
                          "pipeline_parallel_size": 2},
        decode_parallel={"tensor_parallel_size": 8,
                         "pipeline_parallel_size": 2},
        prefill_pool={"machines_allocated": 4, "gpus_per_node": 8},
        decode_pool={"machines_allocated": 4, "gpus_per_node": 8},
    )
    result = _build_full_plan(req)
    assert len(result["payloads"]) == 4
    for payload in result["payloads"]:
        ModelCreate(**payload)


def test_plan_pd_engine_role_flags():
    """PD interconnect flags land per side (vLLM kv-transfer by
    default, SGLang disaggregation-mode when requested)."""
    req = _req(
        enabled=True,
        prefill_parallel={"tensor_parallel_size": 1},
        decode_parallel={"tensor_parallel_size": 1},
        prefill_pool={"machines_allocated": 1, "gpus_per_node": 8},
        decode_pool={"machines_allocated": 1, "gpus_per_node": 8},
    )
    result = _build_full_plan(req)
    by_name = {p["name"]: p for p in result["payloads"]}
    p_params = " ".join(by_name["svc-prefill-0"]["backend_parameters"])
    d_params = " ".join(by_name["svc-decode-0"]["backend_parameters"])
    assert "kv_producer" in p_params
    assert "kv_consumer" in d_params

    sg = _req(
        backend="sglang",
        enabled=True,
        prefill_parallel={"tensor_parallel_size": 1},
        decode_parallel={"tensor_parallel_size": 1},
        prefill_pool={"machines_allocated": 1, "gpus_per_node": 8},
        decode_pool={"machines_allocated": 1, "gpus_per_node": 8},
    )
    by_name = {
        p["name"]: p for p in _build_full_plan(sg)["payloads"]
    }
    assert "--disaggregation-mode=prefill" in " ".join(
        by_name["svc-prefill-0"]["backend_parameters"]
    )
    assert "--disaggregation-mode=decode" in " ".join(
        by_name["svc-decode-0"]["backend_parameters"]
    )


def test_plan_single_side_has_no_interconnect_flags():
    req = _req()
    result = _build_full_plan(req)
    assert len(result["payloads"]) == 1
    params = " ".join(result["payloads"][0]["backend_parameters"])
    assert "kv-transfer" not in params
    assert "disaggregation" not in params


def test_plan_declarative_insufficient_rejected():
    req = _req(
        mode="declarative",
        enabled=True,
        prefill_groups=2,
        decode_groups=1,
        prefill_parallel={"tensor_parallel_size": 8,
                          "pipeline_parallel_size": 2},
        decode_parallel={"tensor_parallel_size": 1},
        prefill_pool={"machines_allocated": 3, "gpus_per_node": 8},
        decode_pool={"machines_allocated": 1, "gpus_per_node": 8},
    )
    from gpustack.api.exceptions import InvalidException
    with pytest.raises(InvalidException):
        _build_full_plan(req)
