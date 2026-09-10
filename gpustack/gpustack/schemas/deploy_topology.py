"""Resource topology and parallel-strategy planning for model serving.

Three nested levels, strictly ordered (outermost first):

1. Resource topology  — how the deployment spans machines at all:
   ``single_node`` (one machine), ``multi_node`` (machines linked by a
   distributed backend), ``single_node_multi_gpu`` (one machine, several
   GPUs sharded by a single instance).
2. Parallel strategy  — how *one* model instance is split:
   TP × PP × EP (× DP for replicas inside a machine). This level always
   describes a single instance; it never spans above level 3.
3. Service attributes — how the *instance pool* is organised: PD
   disaggregation (N prefill groups + M decode groups), autoscaling,
   multi-tenancy.

PD disaggregation is a service-level (outer) attribute: each P/D group is
an independent model instance, and the parallel strategy (TP/PP/EP/DP)
is configured *inside* each group. PP never sits above PD — a group is
the unit that owns an instance, and an instance is what PP splits.

Group-to-machine conversion (heterogeneous-GPU aware, the hard part)::

    gpus_per_group = TP × PP × EP
    machines_per_group = ceil(gpus_per_group / gpus_per_node)
    groups_formed   = floor(machines_allocated / machines_per_group)
    machines_idle   = machines_allocated mod machines_per_group

The conversion is derived from the physical resources and the parallel
strategy — never from the user's wish. Idle machines that cannot form
another group are surfaced explicitly, never silently absorbed.

DP reuse (optional, off by default) packs multiple same-strategy
instances into one machine::

    instances_per_machine = floor(gpus_per_node / gpus_per_group)
    groups_formed         = machines_allocated × instances_per_machine

Both interaction modes converge on ``plan_groups``:

* declarative  — the user asks for N groups; the planner reports the
  machines required and fails with the shortfall if the pool is too
  small.
* resource-pool — the user allocates machines; the planner reports how
  many groups form and what is left idle.
"""

import math
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ResourceTopologyEnum(str, Enum):
    """Level 1 — how the deployment spans machines."""

    SINGLE_NODE = "single_node"
    SINGLE_NODE_MULTI_GPU = "single_node_multi_gpu"
    MULTI_NODE = "multi_node"


class GroupParallelStrategy(BaseModel):
    """Level 2 — parallel strategy of ONE model instance (a P/D group
    when PD disaggregation is on, the single instance otherwise)."""

    tensor_parallel_size: int = Field(default=1, ge=1, le=1024)
    pipeline_parallel_size: int = Field(default=1, ge=1, le=1024)
    expert_parallel_size: int = Field(default=1, ge=1, le=1024)
    # DP inside the group (replicas sharing the same strategy). Off by
    # default — packing extra instances into a machine is an explicit
    # user decision, never a silent default.
    data_parallel_replicas: int = Field(default=1, ge=1, le=64)

    @property
    def gpus_per_instance(self) -> int:
        return (
            self.tensor_parallel_size
            * self.pipeline_parallel_size
            * self.expert_parallel_size
        )


class NodePool(BaseModel):
    """Physical resources offered to one side (P or D) of a deployment.

    Heterogeneous pools are described by their reference node: the
    planner converts against ``gpus_per_node`` and ``vram_per_gpu_mb``
    and assumes the allocated machines match that profile.
    """

    machines_allocated: int = Field(default=1, ge=0)
    gpus_per_node: int = Field(default=1, ge=1)
    vram_per_gpu_mb: Optional[int] = Field(
        default=None,
        description="Per-GPU VRAM in MB; None skips the VRAM check",
    )


class GroupPlan(BaseModel):
    """Per-group conversion result — everything the UI card renders."""

    parallel: GroupParallelStrategy
    gpus_per_instance: int
    # Single-machine PP vs cross-machine PP, resolved by the planner
    # (never left for the user to guess).
    cross_machine: bool
    machines_per_group: int
    instances_per_machine: int = Field(
        default=1,
        description="DP reuse: >1 only when explicitly requested",
    )


class SidePlan(BaseModel):
    """Conversion result for one side (prefill / decode, or single)."""

    groups_formed: int
    groups_requested: int
    machines_used: int
    machines_idle: int
    gpus_used: int
    gpus_idle: int
    machines_per_group: int
    cross_machine: bool
    # DP reuse: same-strategy process replicas packed per machine.
    # >1 only when the user explicitly enables DP reuse.
    instances_per_machine: int = 1
    # Human-readable surplus/shortfall explanation. Empty when the
    # allocation divides cleanly — never silently absorbed.
    notice: str = ""
    # Declarative mode: machines needed to satisfy groups_requested.
    machines_required: Optional[int] = None
    insufficient: bool = False
    # VRAM feasibility (best effort — None when unverifiable).
    vram_fit: Optional[bool] = None
    vram_notice: str = ""


class ServicePlan(BaseModel):
    """Full deployment plan across the three levels."""

    topology: ResourceTopologyEnum
    # Level 3 — PD disaggregation (service level, outermost of the two
    # strategy levels).
    pd_disaggregation: bool = False
    prefill: Optional[SidePlan] = None
    decode: Optional[SidePlan] = None
    single: Optional[SidePlan] = None
    # Errors that make the plan invalid (empty when deployable).
    errors: List[str] = Field(default_factory=list)


class PDServiceSpec(BaseModel):
    """Level 3 configuration — the PD disaggregation switch and the
    group layout. 1P1D is the N=1, M=1 default, not a separate mode."""

    enabled: bool = False
    prefill_groups: int = Field(default=1, ge=1, le=64)
    decode_groups: int = Field(default=1, ge=1, le=64)
    prefill_parallel: GroupParallelStrategy = Field(
        default_factory=GroupParallelStrategy
    )
    decode_parallel: GroupParallelStrategy = Field(
        default_factory=GroupParallelStrategy
    )
    prefill_pool: NodePool = Field(default_factory=NodePool)
    decode_pool: NodePool = Field(default_factory=NodePool)
    # N:M routing among groups (round_robin / least_load / prefix_affin)
    routing_policy: str = Field(default="least_load")
    # KV transfer between P and D groups reuses the cache stack
    # (LMCache/HiCache) already integrated in the worker.
    kv_transfer_backend: str = Field(default="lmcache")


def plan_groups(
    parallel: GroupParallelStrategy,
    pool: NodePool,
    mode: str,
    groups_requested: Optional[int] = None,
) -> SidePlan:
    """Convert a pool of machines into P/D groups under a strategy.

    ``mode`` is either ``resource_pool`` (machines given, groups derived)
    or ``declarative`` (groups given, machines derived). Both produce
    the same SidePlan for the same physical outcome.
    """
    gpus_per_instance = parallel.gpus_per_instance
    cross_machine = gpus_per_instance > pool.gpus_per_node
    machines_per_group = math.ceil(gpus_per_instance / pool.gpus_per_node)

    dp = parallel.data_parallel_replicas
    if dp > 1 and not cross_machine:
        # DP reuse only packs within a machine; cross-machine DP is
        # expressed as more groups, not as replicas inside a group.
        instances_per_machine = max(1, pool.gpus_per_node // gpus_per_instance)
    else:
        instances_per_machine = 1

    notice = ""
    machines_required = None
    insufficient = False

    # DP reuse packs process replicas inside each machine; the
    # deployment unit count (groups_formed) counts Model rows — each
    # row carries data_parallel_replicas as its replica count. The
    # replica packing is reported via instances_per_machine.
    if mode == "declarative":
        wanted = groups_requested or 1
        machines_required = wanted * machines_per_group
        if machines_required > pool.machines_allocated:
            insufficient = True
            notice = (
                f"需要 {machines_required} 台机才能组成 {wanted} 个组, "
                f"当前只分配了 {pool.machines_allocated} 台."
            )
        groups_formed = min(
            wanted, math.floor(pool.machines_allocated / machines_per_group)
        )
        machines_used = min(pool.machines_allocated, machines_required)
        machines_idle = pool.machines_allocated - machines_used
    else:
        # resource_pool mode
        groups_formed = math.floor(
            pool.machines_allocated / machines_per_group
        )
        machines_used = groups_formed * machines_per_group
        machines_idle = pool.machines_allocated - machines_used
        if machines_idle > 0 and groups_formed > 0:
            notice = (
                f"已分配 {pool.machines_allocated} 台机, 单组占用 "
                f"{machines_per_group} 台, 只能形成 {groups_formed} 个组, "
                f"剩余 {machines_idle} 台不足以组成第 "
                f"{groups_formed + 1} 组."
            )
        elif machines_idle > 0 and groups_formed == 0:
            notice = (
                f"已分配 {pool.machines_allocated} 台机, 单组需 "
                f"{machines_per_group} 台, 无法组成任何一个组."
            )
        groups_requested = groups_requested or groups_formed
        machines_required = machines_used

    # Physical GPU footprint counts every process replica (DP reuse
    # included); groups_formed stays the Model-row count.
    gpus_used = (
        groups_formed
        * gpus_per_instance
        * parallel.data_parallel_replicas
        if instances_per_machine > 1
        else groups_formed * gpus_per_instance
    )
    total_gpus = pool.machines_allocated * pool.gpus_per_node
    gpus_idle = max(0, total_gpus - gpus_used)

    return SidePlan(
        groups_formed=groups_formed,
        groups_requested=groups_requested or 0,
        machines_used=machines_used,
        machines_idle=machines_idle,
        gpus_used=gpus_used,
        gpus_idle=gpus_idle,
        machines_per_group=machines_per_group,
        cross_machine=cross_machine,
        instances_per_machine=instances_per_machine,
        notice=notice,
        machines_required=machines_required,
        insufficient=insufficient,
    )


def check_vram(
    side: SidePlan,
    parallel: GroupParallelStrategy,
    pool: NodePool,
    per_instance_vram_mb: Optional[int],
) -> None:
    """Best-effort VRAM feasibility check.

    ``per_instance_vram_mb`` is the whole-instance footprint (weights +
    KV cache + activations) when known; when it is unknown the check is
    skipped rather than guessed.
    """
    if (
        per_instance_vram_mb is None
        or pool.vram_per_gpu_mb is None
        or side.groups_formed == 0
    ):
        side.vram_fit = None
        return

    # An instance spanning TP×PP×EP GPUs on its machines: each GPU
    # carries 1/(TP×PP×EP) of the instance footprint (roughly — PP
    # shards layers, TP/EP shard tensors/experts).
    per_gpu_needed = per_instance_vram_mb / parallel.gpus_per_instance
    if per_gpu_needed <= pool.vram_per_gpu_mb:
        side.vram_fit = True
        side.vram_notice = ""
        return
    side.vram_fit = False
    per_machine = pool.gpus_per_node * pool.vram_per_gpu_mb
    needed_machines = math.ceil(per_instance_vram_mb / per_machine)
    side.vram_notice = (
        f"单组显存需求 {per_instance_vram_mb} MB 超过单组可用 "
        f"{per_machine} MB; 需 {needed_machines} 台/组 或调整并行策略."
    )


def build_service_plan(
    topology: ResourceTopologyEnum,
    pd: PDServiceSpec,
    mode: str = "resource_pool",
    per_instance_vram_mb: Optional[int] = None,
) -> ServicePlan:
    """Validate the three levels and produce the full service plan."""
    plan = ServicePlan(topology=topology, pd_disaggregation=pd.enabled)

    if not pd.enabled:
        # Levels 1+2 only: one instance, topology decides placement.
        pool = pd.prefill_pool
        side = plan_groups(pd.prefill_parallel, pool, mode,
                           groups_requested=1)
        check_vram(side, pd.prefill_parallel, pool, per_instance_vram_mb)
        if side.groups_formed < 1:
            side.insufficient = True
            side.notice = side.notice or (
                f"资源不足以部署 1 个实例 (单组需 "
                f"{side.machines_per_group} 台机, 分配 {pool.machines_allocated} 台)."
            )
        plan.single = side
        return plan

    p = plan_groups(pd.prefill_parallel, pd.prefill_pool, mode,
                    groups_requested=pd.prefill_groups)
    d = plan_groups(pd.decode_parallel, pd.decode_pool, mode,
                    groups_requested=pd.decode_groups)
    check_vram(p, pd.prefill_parallel, pd.prefill_pool,
               per_instance_vram_mb)
    check_vram(d, pd.decode_parallel, pd.decode_pool,
               per_instance_vram_mb)
    plan.prefill = p
    plan.decode = d

    if p.insufficient or p.groups_formed < 1:
        plan.errors.append(f"Prefill 组不足: {p.notice}")
    if d.insufficient or d.groups_formed < 1:
        plan.errors.append(f"Decode 组不足: {d.notice}")
    if (not p.insufficient) and p.groups_formed < p.groups_requested:
        p.insufficient = True
        plan.errors.append(
            f"Prefill 只能形成 {p.groups_formed}/{p.groups_requested} 个组: {p.notice}"
        )
    if (not d.insufficient) and d.groups_formed < d.groups_requested:
        d.insufficient = True
        plan.errors.append(
            f"Decode 只能形成 {d.groups_formed}/{d.groups_requested} 个组: {d.notice}"
        )
    if (p.vram_fit is False) or (d.vram_fit is False):
        for s, name in ((p, "Prefill"), (d, "Decode")):
            if s.vram_fit is False:
                plan.errors.append(f"{name} 组显存不足: {s.vram_notice}")
    return plan


def suggest_topology_upgrade(
    parallel: GroupParallelStrategy,
    pool: NodePool,
) -> Optional[str]:
    """Single-machine → multi-machine hint when the instance no longer
    fits one node. The parallel strategy is preserved as-is; only the
    topology level changes."""
    if parallel.gpus_per_instance <= pool.gpus_per_node:
        return None
    machines = math.ceil(parallel.gpus_per_instance / pool.gpus_per_node)
    return (
        f"单机 {pool.gpus_per_node} 卡放不下该实例 "
        f"(需 {parallel.gpus_per_instance} 卡), 建议切换为「多机部署」"
        f"跨 {machines} 台机; 已配置的并行策略保持不变."
    )
