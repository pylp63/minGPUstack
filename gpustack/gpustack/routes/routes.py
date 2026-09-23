import os

from fastapi import APIRouter, Depends

from gpustack.routes import (
    api_keys,
    auth,
    cache_providers,
    cache_service_instances,
    cache_services,
    cluster_access,
    config,
    dashboard,
    debug,
    draft_models,
    gpu_devices,
    gpu_instance_ssh_public_keys,
    gpu_instance_templates,
    inference_backend,
    me_orgs,
    metrics,
    model_evaluations,
    model_files,
    model_instances,
    model_sets,
    organization_members,
    organizations,
    ota_sources,
    probes,
    proxy,
    source_probe,
    update,
    user_groups,
    users,
    models,
    openai,
    workers,
    usage,
    usage_resources,
    cloud_credentials,
    worker_pools,
    clusters,
    token,
    benchmarks,
    benchmark_profiles,
    model_provider,
    rerank,
    model_routes,
    grafana,
    prometheus,
    gpu_instance_persistent_volume_types,
    gpu_instance_persistent_volumes,
    gpu_instance_types,
    gpu_instance_type_flavors,
    gpu_instances,
    gpu_requests,
    notifications,
    worker_access,
    deploy_presets,
    deploy_topologies,
    serving_topology,
)

from gpustack.api.exceptions import error_responses, openai_api_error_responses
from gpustack.api.auth import (
    get_admin_user,
    get_current_user,
    get_cluster_principal,
    get_worker_principal,
    management_scope,
    inference_scope,
)
from gpustack.api.tenant import (
    TenantContext,
    bypass_tenant_filter,
    get_tenant_context,
    require_org_role,
)
from gpustack.schemas.principals import OrgRole
from gpustack.websocket_proxy.message_server import router as message_server_router
from gpustack.routes.ssh_gateway import router as ssh_gateway_router
from gpustack.routes.gateway_metrics import router as gateway_metrics_router

from gpustack_higress_plugins.server import router as higress_plugins_router

versioned_prefix = "/v2"

# Org-owner gate for management surfaces (Models / Model Routes /
# Model Providers / Benchmarks / Model Files). Platform admin and
# SYSTEM principals (worker / cluster callbacks reaching shared
# routers) bypass via ``require_org_role`` itself.
_org_owner_only = [Depends(require_org_role(OrgRole.OWNER))]


# 二开: 模型服务管理面 (模型库/部署/路由) 开放给普通用户的「个人空间」。
# _org_owner_only 要求 org OWNER 角色 — 普通用户 (不在任何 org) 一律 403,
# 前端菜单也被 canSeeOrgAdmin (=is_admin) 隐藏。开放策略:
#   平台 admin / SYSTEM        → 全量 (bypass)
#   org context + OWNER 角色   → 该 org (原有行为)
#   个人空间 (principal=自己)  → 自己的资源 (owner_principal_id 过滤已存在)
# 提供商/基准测试/推理后端/缓存加速仍 _org_owner_only: 平台级运维配置,
# 普通用户无对应资源视图。
async def _org_owner_or_personal_dep(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> TenantContext:
    if bypass_tenant_filter(ctx) or ctx.is_platform_admin:
        return ctx
    # org context: 维持 OWNER 门槛
    if ctx.current_principal_id is not None and not ctx.current_is_personal_scope:
        ctx.assert_org_role(OrgRole.OWNER)
        return ctx
    # 个人空间 / 无 context: 放行 (行级 owner_principal_id 过滤兜底,
    # 无 context 时非 admin 只能看到自己 id 的行或被 404)
    return ctx


_org_owner_or_personal = [Depends(_org_owner_or_personal_dep)]

# Toggle for surfacing extended API endpoints in the OpenAPI schema
# and ``/docs``. Endpoints stay mounted regardless — only the public
# docs surface is gated. Off by default; set the env var to a truthy
# value to expose the full surface.
_EXTENDED_API_IN_SCHEMA = os.getenv("GPUSTACK_EXTENDED_API_DOCS", "").lower() in (
    "1",
    "true",
    "yes",
)

api_router = APIRouter(responses=error_responses)
management_router = APIRouter(dependencies=[Depends(management_scope)])
management_router.include_router(
    grafana.router,
    prefix="/grafana",
    dependencies=[Depends(get_admin_user)],
    include_in_schema=False,
)
management_router.include_router(
    prometheus.router,
    prefix="/prometheus",
    dependencies=[Depends(get_admin_user)],
    include_in_schema=False,
)

# authed routes

v1_base_router = APIRouter(dependencies=[Depends(get_current_user)])
v1_base_router.include_router(users.me_router, prefix="/users", tags=["Users"])
v1_base_router.include_router(users.directory_router, tags=["Users"])
v1_base_router.include_router(api_keys.router, prefix="/api-keys", tags=["API Keys"])
v1_base_router.include_router(usage.router, prefix="/usage", tags=["Usage"])
# 二开补充: 官方新版 UI 使用量页的资源计量端点 (summary/resource-*/storage),
# 消除前端 404。详见 usage_resources.py 模块头注释。
v1_base_router.include_router(
    usage_resources.router, prefix="/usage", tags=["Usage"]
)
v1_base_router.include_router(
    me_orgs.router,
    prefix="/users/me",
    tags=["My Organizations"],
    include_in_schema=_EXTENDED_API_IN_SCHEMA,
)
v1_base_router.include_router(
    organization_members.router,
    tags=["Organization Members"],
    include_in_schema=_EXTENDED_API_IN_SCHEMA,
)
v1_base_router.include_router(
    user_groups.router,
    tags=["User Groups"],
    include_in_schema=_EXTENDED_API_IN_SCHEMA,
)
v1_base_router.include_router(
    metrics.router, prefix="/metrics", include_in_schema=False
)
v1_base_router.include_router(
    model_routes.my_models_router,
    dependencies=[Depends(get_current_user)],
    prefix="/my-models",
    tags=["My Models"],
)
# BYO cluster: clusters / cloud-credentials / worker-pools live on the
# user-level router so Org owner / admin can CRUD their own infra. The
# routes themselves enforce per-row ownership via assert_cluster_writable
# and friends, so platform-only operations (e.g. set-default) still
# require is_admin inside the handler.
v1_base_router.include_router(clusters.router, prefix="/clusters", tags=["Clusters"])
v1_base_router.include_router(
    cloud_credentials.router,
    prefix="/cloud-credentials",
    tags=["Cloud Credentials"],
)
v1_base_router.include_router(
    worker_pools.router, prefix="/worker-pools", tags=["Worker Pools"]
)
# Workers are visible to anyone who can see their cluster; mutations gated
# by an explicit is_admin check inside each handler.
v1_base_router.include_router(workers.router, prefix="/workers", tags=["Workers"])

# User isolation (feature one): GPU allocation requests, notifications,
# and worker-level access grants. Worker-access endpoints carry their own
# /workers/... paths (registered without an extra prefix).
v1_base_router.include_router(
    gpu_requests.router, prefix="/gpu-requests", tags=["GPU Requests"]
)
v1_base_router.include_router(
    notifications.router, prefix="/notifications", tags=["Notifications"]
)
v1_base_router.include_router(
    worker_access.router, tags=["Worker Access"]
)

# Feature two: one-click deployment architecture presets.
v1_base_router.include_router(
    deploy_presets.router, prefix="/deploy-presets", tags=["Deploy Presets"]
)
v1_base_router.include_router(
    deploy_topologies.router,
    prefix="/deploy-topologies",
    tags=["Deploy Topologies"],
)

v1_base_router.include_router(
    serving_topology.router,
    prefix="/serving-topology",
    tags=["Serving Topology"],
)

cluster_client_router = APIRouter()
cluster_client_router.add_api_route(
    path="/clusters/{id}/manifests",
    endpoint=clusters.get_cluster_manifests,
    methods=["GET"],
)
cluster_client_router.add_api_route(
    path="/workers",
    endpoint=workers.create_worker,
    methods=["POST"],
)

model_routers = [
    {
        "router": models.router,
        "prefix": "/models",
        "tags": ["Models"],
        "dependencies": _org_owner_or_personal,
    },
    {
        "router": model_instances.router,
        "prefix": "/model-instances",
        "tags": ["Model Instances"],
        "dependencies": _org_owner_or_personal,
    },
    {
        "router": model_files.router,
        "prefix": "/model-files",
        "tags": ["Model Files"],
        "dependencies": _org_owner_or_personal,
    },
    {
        "router": cache_services.router,
        "prefix": "/cache-services",
        "tags": ["Cache Services"],
        "dependencies": _org_owner_only,
    },
    {
        "router": cache_service_instances.router,
        "prefix": "/cache-service-instances",
        "tags": ["Cache Service Instances"],
        "dependencies": _org_owner_only,
    },
    {
        "router": benchmarks.router,
        "prefix": "/benchmarks",
        "tags": ["Benchmarks"],
        "dependencies": _org_owner_only,
    },
    {
        "router": benchmark_profiles.router,
        "prefix": "/benchmark-profiles",
        "tags": ["Benchmark Profiles"],
        "dependencies": _org_owner_only,
    },
    {
        "router": model_routes.target_router,
        "prefix": "/model-route-targets",
        "tags": ["Model Route Targets"],
        "dependencies": _org_owner_only,
    },
    {
        "router": gpu_instance_persistent_volume_types.router,
        "prefix": "/gpu-instance-persistent-volume-types",
        "tags": ["GPU Instance Persistent Volume Types"],
    },
    {
        "router": gpu_instance_persistent_volumes.router,
        "prefix": "/gpu-instance-persistent-volumes",
        "tags": ["GPU Instance Persistent Volumes"],
    },
    {
        "router": gpu_instance_ssh_public_keys.router,
        "prefix": "/gpu-instance-ssh-public-keys",
        "tags": ["GPU Instance SSH Public Keys"],
    },
    {
        "router": gpu_instance_templates.router,
        "prefix": "/gpu-instance-templates",
        "tags": ["GPU Instance Templates"],
    },
    {
        "router": gpu_instance_types.router,
        "prefix": "/gpu-instance-types",
        "tags": ["GPU Instance Types"],
    },
    {
        "router": gpu_instance_type_flavors.router,
        "prefix": "/gpu-instance-type-flavors",
        "tags": ["GPU Instance Type Flavors"],
    },
    {
        "router": gpu_instances.router,
        "prefix": "/gpu-instances",
        "tags": ["GPU Instances"],
    },
]
# worker client have full access to model and model instances
worker_client_router = APIRouter()
for model_router in model_routers:
    worker_client_router.include_router(**model_router)
# ready only access to workers
worker_client_router.add_api_route(
    path="/workers",
    endpoint=workers.get_workers,
    methods=["GET"],
    response_model=workers.WorkersPublic,
)
worker_client_router.add_api_route(
    path="/workers/{id}",
    endpoint=workers.get_worker,
    methods=["GET"],
    response_model=workers.WorkerPublic,
)
worker_client_router.add_api_route(
    path="/worker-status",
    endpoint=workers.create_worker_status,
    methods=["POST"],
    include_in_schema=False,
)
worker_client_router.add_api_route(
    path="/worker-heartbeat",
    endpoint=workers.heartbeat,
    methods=["POST"],
    include_in_schema=False,
)
worker_client_router.add_api_route(
    path="/runner-override-entries",
    endpoint=inference_backend.get_runner_override_entries,
    methods=["GET"],
    response_model=inference_backend.RunnerOverrideEntriesPublic,
    tags=["Inference Backend"],
)
worker_client_router.include_router(
    inference_backend.router, prefix="/inference-backends", tags=["Inference Backend"]
)

# Tenant-aware routers: any logged-in user can hit them; the handlers
# filter by TenantContext (owner_principal_id / cluster visibility).
tenant_routers = model_routers + [
    {"router": gpu_devices.router, "prefix": "/gpu-devices", "tags": ["GPU Devices"]},
    {
        "router": model_provider.router,
        "prefix": "/model-providers",
        "tags": ["Model Providers"],
        "dependencies": _org_owner_only,
    },
    {
        "router": model_routes.router,
        "prefix": "/model-routes",
        "tags": ["Model Routes"],
        "dependencies": _org_owner_or_personal,
    },
    {
        "router": model_evaluations.router,
        "prefix": "/model-evaluations",
        "tags": ["Model Evaluations"],
        "dependencies": _org_owner_only,
    },
    # Read-only platform catalogs (no tenant data) — every logged-in user
    # needs them to deploy models, including Org owners/managers.
    {"router": model_sets.router, "prefix": "/model-sets", "tags": ["Model Sets"]},
    {
        "router": draft_models.router,
        "prefix": "/draft-models",
        "tags": ["Draft Models"],
    },
    {
        "router": cache_providers.router,
        "prefix": "/cache-providers",
        "tags": ["Cache Providers"],
    },
    # Inference backends are platform-wide (admin curates) but every Org
    # owner/manager needs to read them to pick a backend at deploy time.
    # Worker / cluster system principals also reach this through
    # v1_base_router since `get_current_user` accepts ``kind=SYSTEM``
    # callers.
    {
        "router": inference_backend.router,
        "prefix": "/inference-backends",
        "tags": ["Inference Backend"],
    },
    # Dashboard sub-routes gate themselves inside the handler. The
    # per-cluster ``GET /dashboard?cluster_id=X`` accepts anyone who
    # can see the cluster (cluster-detail audience); the aggregate
    # ``GET /dashboard`` and the ``/usage`` endpoints stay platform
    # admin only.
    {"router": dashboard.router, "prefix": "/dashboard", "tags": ["Dashboard"]},
    # cluster_access GET is open to anyone who can see the cluster
    # (cluster-detail audience); POST / DELETE gate inside the
    # handler to platform admin OR cluster owner Org owner
    # (`assert_cluster_writable`) — share-out is part of running
    # the cluster, non-owner grantees can't re-grant.
    {
        "router": cluster_access.router,
        "tags": ["Cluster Access"],
        "include_in_schema": _EXTENDED_API_IN_SCHEMA,
    },
    # Slim org-list endpoint for picker UIs (cluster-access "Grant
    # access" form, etc.). Gated inside the handler to platform admin
    # OR owner-role in any Org context; the full /organizations CRUD
    # stays admin-only under admin_routers below.
    {
        "router": organizations.directory_router,
        "tags": ["Organizations"],
        "include_in_schema": _EXTENDED_API_IN_SCHEMA,
    },
]

# Platform-only routers — admin can manage globally; non-admin gets 403.
admin_routers = [
    {"router": users.router, "prefix": "/users", "tags": ["Users"]},
    {
        "router": organizations.router,
        "prefix": "/organizations",
        "tags": ["Organizations"],
        "include_in_schema": _EXTENDED_API_IN_SCHEMA,
    },
    # The OTA content family: per-kind source configuration, and the probe that
    # reports on it. Top-level rather than hanging off each consumer — see
    # ``routes/ota_sources.py``. Versioned, because both responses mirror the
    # source rows rather than the server itself, unlike the unversioned /debug
    # and /update.
    {
        "router": ota_sources.router,
        "prefix": "/ota-sources",
        "tags": ["OTA Sources"],
    },
    {
        "router": source_probe.router,
        "prefix": "/source-probe",
        "tags": ["Source Probe"],
    },
]

for tr in tenant_routers:
    v1_base_router.include_router(**tr)

v1_admin_router = APIRouter()
for admin_router in admin_routers:
    v1_admin_router.include_router(**admin_router)

# Order matters: FastAPI dispatches the FIRST router whose path matches.
# v1_base_router and worker_client_router register overlapping endpoints
# (e.g. /v2/models, /v2/workers) — putting v1_base_router first means
# regular user requests resolve through ``get_current_user`` (which also
# accepts worker / cluster system principals), and only routes that
# are unique to the worker / cluster client paths fall through to
# those routers.
management_router.include_router(
    v1_base_router, dependencies=[Depends(get_current_user)], prefix=versioned_prefix
)
management_router.include_router(
    worker_client_router,
    dependencies=[Depends(get_worker_principal)],
    prefix=versioned_prefix,
)
management_router.include_router(
    cluster_client_router,
    dependencies=[Depends(get_cluster_principal)],
    prefix=versioned_prefix,
)
management_router.include_router(
    v1_admin_router, dependencies=[Depends(get_admin_user)], prefix=versioned_prefix
)
management_router.include_router(
    config.router,
    dependencies=[Depends(get_admin_user)],
    prefix=versioned_prefix,
    include_in_schema=False,
)
management_router.include_router(
    debug.router,
    dependencies=[Depends(get_admin_user)],
    prefix="/debug",
    include_in_schema=False,
)
management_router.include_router(
    update.router,
    dependencies=[Depends(get_admin_user)],
    prefix="/update",
    include_in_schema=False,
)
management_router.include_router(
    proxy.router,
    dependencies=[Depends(get_current_user)],
    prefix="/proxy",
    tags=["Server-Side Proxy"],
    include_in_schema=False,
)

inference_router = APIRouter(
    dependencies=[Depends(get_current_user), Depends(inference_scope)]
)

inference_router.include_router(
    openai.get_legacy_api_router(),
    prefix="/v1-openai",
    responses=openai_api_error_responses,
    tags=["OpenAI-Compatible APIs (Legacy alias)"],
)
inference_router.include_router(
    openai.get_api_router(),
    prefix="/v1",
    responses=openai_api_error_responses,
    tags=["OpenAI-Compatible APIs"],
)
inference_router.include_router(
    rerank.router,
    prefix="/v1",
    tags=["Rerank"],
)

# Following routes should not check api scope as it is publicly accessible and used for authentication by external services.
api_router.include_router(probes.router, tags=["Probes"])
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(
    router=token.router,
    prefix="/token-auth",
    include_in_schema=False,
)

api_router.include_router(management_router)
api_router.include_router(inference_router)
api_router.include_router(higress_plugins_router, include_in_schema=False)
api_router.include_router(
    gateway_metrics_router,
    prefix=f"{versioned_prefix}/usage",
    include_in_schema=False,
)
api_router.include_router(
    message_server_router,
    tags=["WebSocket Proxy"],
    include_in_schema=True,
)
# SSH 网关反代 (二开): /v2/ssh-gw/ticket/{id} + /v2/ssh-gw/ws
# 挂 api_router (v2 前缀) 但不带 get_current_user 依赖 —
# ticket 路由自带 SessionDep/TenantContextDep 校验, WS 走一次性 ticket。
api_router.include_router(
    ssh_gateway_router,
    prefix=f"{versioned_prefix}/ssh-gw",
    tags=["SSH Gateway"],
    include_in_schema=False,
)
