"""Deployment architecture presets (feature two): one-click deploy.

Endpoints::

    GET  /v1/deploy-presets                 list available architectures
    POST /v1/deploy-presets/plan            expand to ModelCreate payloads
    POST /v1/deploy-presets/deploy          plan + create the models

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
)
from gpustack.server.deps import SessionDep, TenantContextDep

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=List[dict])
async def list_presets():
    """Enumerate the supported one-click deployment architectures."""
    return [
        {
            "architecture": arch.value,
            "description": desc,
            "parameters": (
                ["prefill_gpu_count", "decode_gpu_count"]
                if arch in (DeploymentArchitectureEnum.PD_DISAGGREGATED,)
                else (
                    [
                        "prefill_gpu_count",
                        "decode_gpu_count",
                        "prefill_replicas",
                        "decode_replicas",
                    ]
                    if arch == DeploymentArchitectureEnum.MULTI_PD
                    else (
                        ["pipeline_parallel_size"]
                        if arch == DeploymentArchitectureEnum.PIPELINE_PARALLEL
                        else []
                    )
                )
            ),
        }
        for arch, desc in PRESET_DESCRIPTIONS.items()
    ]


@router.post("/plan", response_model=PresetDeployPlan)
async def plan_preset(req: PresetDeployRequest):
    """Expand an architecture request into ModelCreate payloads (dry-run)."""
    try:
        return build_preset_payloads(req)
    except ValueError as e:
        raise InvalidException(message=str(e))


@router.post("/deploy", response_model=PresetDeployPlan)
async def deploy_preset(session: SessionDep, ctx: TenantContextDep, req: PresetDeployRequest):
    """One-click deploy: expand the preset and create the models."""
    from gpustack.routes.models import create_model
    from gpustack.schemas.models import ModelCreate

    plan = build_preset_payloads(req)

    created = []
    try:
        for payload in plan.payloads:
            model_in = ModelCreate(**payload)
            # Reuse the full model-create path — validation, quota
            # enforcement (feature one), tenant stamping, route wiring.
            model = await create_model(session=session, ctx=ctx, model_in=model_in)
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

    logger.info(
        f"Preset '{req.architecture.value}' deployed: "
        f"{[m.name for m in created]}"
    )
    return plan
