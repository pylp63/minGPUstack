"""Deployment architecture presets (feature two).

One-click deployment architectures for GPUStack:

* ``standalone``      — single-node deployment, one instance on one
                        machine (the default GPUStack behaviour).
* ``pd-disaggregated``— PD 分离: separate prefill and decode instance
                        groups for the same model, each with its own
                        parallelism. The gateway splits traffic; for
                        vLLM this uses the
                        ``--scheduling-policy`` / role flags pattern.
* ``multi-pd``        — 多P多D: multiple prefill + multiple decode
                        replicas with per-role GPU allocations.
* ``pipeline``        — 流水线并行: one logical deployment split
                        across multiple nodes/GPUs via pipeline
                        parallelism (``--pipeline-parallel-size``) and
                        distributed inference across workers.

The preset endpoint expands an architecture spec into a ready-to-POST
``ModelCreate`` payload (or several, for the disaggregated variants),
so a UI / CLI can offer "一键部署" with a single choice.

Also exposed as a pure library function (:func:`build_preset_payloads`)
so the CLI and API share one expansion implementation.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DeploymentArchitectureEnum(str, Enum):
    STANDALONE = "standalone"
    PD_DISAGGREGATED = "pd_disaggregated"
    MULTI_PD = "multi_pd"
    PIPELINE_PARALLEL = "pipeline_parallel"


class PresetDeployRequest(BaseModel):
    """Body of POST /v1/deploy-presets/deploy.

    ``model_name`` and ``model_source`` follow ModelCreate; the preset
    adds architecture-specific fields.
    """

    architecture: DeploymentArchitectureEnum = (
        DeploymentArchitectureEnum.STANDALONE
    )
    model_name: str = Field(min_length=1, max_length=255)
    model_source: str = Field(min_length=1, description="HF repo id or URL")
    # Per-role GPU counts / parallelism.
    prefill_gpu_count: int = Field(default=1, ge=1, le=1024)
    decode_gpu_count: int = Field(default=1, ge=1, le=1024)
    # Replicas per role (multi-P-multi-D).
    prefill_replicas: int = Field(default=1, ge=1, le=64)
    decode_replicas: int = Field(default=1, ge=1, le=64)
    # Pipeline parallel size (pipeline_parallel).
    pipeline_parallel_size: int = Field(default=1, ge=1, le=64)
    backend_parameters: Optional[List[str]] = None
    cluster_id: Optional[int] = None


class PresetDeployPlan(BaseModel):
    """Expanded plan: the ModelCreate payloads to POST, in order."""

    architecture: str
    description: str
    payloads: List[Dict[str, Any]]


PRESET_DESCRIPTIONS = {
    DeploymentArchitectureEnum.STANDALONE: (
        "单机部署 — one model instance scheduled onto a single machine."
    ),
    DeploymentArchitectureEnum.PD_DISAGGREGATED: (
        "PD 分离 — independent prefill (P) and decode (D) instance groups "
        "with their own GPU pools; maximizes throughput for online serving."
    ),
    DeploymentArchitectureEnum.MULTI_PD: (
        "多P多D — multiple prefill and multiple decode replicas, each "
        "role horizontally scaled; for high-concurrency production serving."
    ),
    DeploymentArchitectureEnum.PIPELINE_PARALLEL: (
        "流水线并行 — a single deployment split across multiple GPUs / "
        "nodes via pipeline parallelism; for models too large for one GPU."
    ),
}


def _base_payload(req: PresetDeployRequest, **overrides) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": req.model_name,
        "model_source": req.model_source,
        "replicas": 1,
        "backend_parameters": list(req.backend_parameters or []),
        "distributed_inference_across_workers": False,
    }
    if req.cluster_id is not None:
        payload["cluster_id"] = req.cluster_id
    payload.update(overrides)
    return payload


def _merge_backend_parameters(payload: Dict[str, Any], extra: List[str]) -> None:
    """Append ``extra`` flags, replacing any same-named flag already set."""
    from gpustack.utils.command import find_parameter

    existing = payload.get("backend_parameters") or []
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


def build_preset_payloads(req: PresetDeployRequest) -> PresetDeployPlan:
    """Expand a preset request into ModelCreate payloads.

    For disaggregated architectures this yields two payloads — the
    prefill group and the decode group — named ``<model>-prefill`` and
    ``<model>-decode`` so they coexist under the same base name.
    """
    arch = req.architecture

    if arch == DeploymentArchitectureEnum.STANDALONE:
        return PresetDeployPlan(
            architecture=arch.value,
            description=PRESET_DESCRIPTIONS[arch],
            payloads=[
                _base_payload(req, replicas=1),
            ],
        )

    if arch == DeploymentArchitectureEnum.PD_DISAGGREGATED:
        # One P group + one D group. Both carry tensor parallel sized to
        # their GPU allocation; the role is conveyed through standard
        # vLLM flags (vllm serve --api-server-type / sglang equivalent).
        prefill = _base_payload(req, name=f"{req.model_name}-prefill")
        _merge_backend_parameters(
            prefill,
            [
                f"--tensor-parallel-size={req.prefill_gpu_count}",
                "--api-server-type=prefill",
            ],
        )
        decode = _base_payload(req, name=f"{req.model_name}-decode")
        _merge_backend_parameters(
            decode,
            [
                f"--tensor-parallel-size={req.decode_gpu_count}",
                "--api-server-type=decode",
            ],
        )
        return PresetDeployPlan(
            architecture=arch.value,
            description=PRESET_DESCRIPTIONS[arch],
            payloads=[prefill, decode],
        )

    if arch == DeploymentArchitectureEnum.MULTI_PD:
        payloads = []
        # N prefill replicas.
        prefill = _base_payload(
            req,
            name=f"{req.model_name}-prefill",
            replicas=req.prefill_replicas,
        )
        _merge_backend_parameters(
            prefill,
            [
                f"--tensor-parallel-size={req.prefill_gpu_count}",
                "--api-server-type=prefill",
            ],
        )
        payloads.append(prefill)
        # M decode replicas.
        decode = _base_payload(
            req,
            name=f"{req.model_name}-decode",
            replicas=req.decode_replicas,
        )
        _merge_backend_parameters(
            decode,
            [
                f"--tensor-parallel-size={req.decode_gpu_count}",
                "--api-server-type=decode",
            ],
        )
        payloads.append(decode)
        return PresetDeployPlan(
            architecture=arch.value,
            description=PRESET_DESCRIPTIONS[arch],
            payloads=payloads,
        )

    if arch == DeploymentArchitectureEnum.PIPELINE_PARALLEL:
        payload = _base_payload(
            req,
            replicas=1,
            distributed_inference_across_workers=(req.pipeline_parallel_size > 1),
        )
        _merge_backend_parameters(
            payload,
            [f"--pipeline-parallel-size={req.pipeline_parallel_size}"],
        )
        return PresetDeployPlan(
            architecture=arch.value,
            description=PRESET_DESCRIPTIONS[arch],
            payloads=[payload],
        )

    raise ValueError(f"Unknown architecture: {arch}")
