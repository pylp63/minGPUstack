# GPUStack 二次开发版

基于 [gpustack.ai](https://gpustack.ai) 开源代码的二次开发,新增两大功能。

## 功能一: 用户隔离 (GPU 申请审批 + 节点级授权)

普通用户与管理员 (admin) 之间的完整 GPU 资源隔离流程:

```
普通用户                     admin
   │  ① 提交 GPU 申请           │
   │  POST /v1/gpu-requests    │
   ├──────────────────────────▶│ ② 收到通知 (POST 时自动 fan-out
   │                           │    给所有平台管理员)
   │                           │ ③ 审批 — 批准并指定配额,
   │                           │    同时可挑选指定节点开放给该用户
   │ ④ 收到审批结果通知          │    POST /v1/workers/{id}/access
   ◀───────────────────────────┤
   │ ⑤ 只能看到被开放的节点       │
   │    GET /v1/workers (过滤)  │
   │ ⑥ 部署模型受配额限制,       │
   │    且只能调度到被授权节点    │
```

### 核心机制

| 能力 | 实现 |
|------|------|
| GPU 申请 | `POST /v1/gpu-requests` (数量/用途/目标模型) |
| admin 通知 | 申请提交时自动给所有 admin 生成站内通知 `GET /v1/notifications` |
| 审批/驳回 | `POST /v1/gpu-requests/{id}/approve` / `reject`,批准时设定 `granted_gpu_count` 配额 |
| 节点开放 | `POST /v1/workers/{worker_id}/access` 把**指定机器**开放给某用户 |
| 视图隔离 | 普通用户 `GET /v1/workers` 只返回被开放的节点(SQL 过滤 + 行级过滤双保险) |
| 调度隔离 | 调度器新增 `WorkerAccessFilter`: 普通用户的模型实例**只会**落在被授权节点 |
| 配额强制 | 创建/更新模型时校验: 已部署 GPU + 新需求 ≤ 批准配额,超限拒绝 |
| 通知闭环 | 审批/驳回/取消/节点开放均产生通知给相关方 |

### API 摘要

```bash
# 普通用户提交申请
curl -X POST $API/v1/gpu-requests -H "Authorization: Bearer $USER_TOKEN" \
  -d '{"requested_gpu_count": 4, "reason": "跑 Qwen 微调", "model_name": "qwen2.5-7b"}'

# admin 查看通知 / 申请
curl $API/v1/notifications/unread -H "Authorization: Bearer $ADMIN_TOKEN"
curl "$API/v1/gpu-requests?status=pending" -H "Authorization: Bearer $ADMIN_TOKEN"

# admin 批准,配额 4 GPU
curl -X POST $API/v1/gpu-requests/1/approve -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"granted_gpu_count": 4}'

# admin 把 2 号节点开放给该用户 (principal_id)
curl -X POST $API/v1/workers/2/access -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"principal_id": 3}'
```

数据库表: `gpu_allocation_requests`, `worker_access`, `notifications`
(Alembic 迁移 `f1a2b3c4d5e6`)。

## 功能二: 一键启动部署架构

支持四种部署架构一键生成:

| 架构 | 说明 |
|------|------|
| `standalone` | 单机部署 — 单实例调度到单节点 |
| `pd_disaggregated` | PD 分离 — 独立 prefill / decode 实例组,各自 GPU 池 |
| `multi_pd` | 多P多D — prefill 与 decode 均多副本水平扩展 |
| `pipeline_parallel` | 流水线并行 — 跨节点/GPU 的 pipeline 并行 |

```bash
# 查看架构列表
curl $API/v1/deploy-presets

# 预览展开结果 (dry-run)
curl -X POST $API/v1/deploy-presets/plan -d '{
  "architecture": "pd_disaggregated",
  "model_name": "qwen", "model_source": "Qwen/Qwen2.5-7B-Instruct",
  "prefill_gpu_count": 2, "decode_gpu_count": 2
}'

# 一键部署 (走完整 model-create 链路,含配额校验)
curl -X POST $API/v1/deploy-presets/deploy -d '{...}'
```

展开逻辑在 `gpustack/schemas/deploy_presets.py` (库函数 `build_preset_payloads`,
CLI 与 API 共用)。

## Docker Compose 部署 (slim 镜像)

```bash
cd deploy
docker compose up -d          # 自动构建 gpustack-custom:latest 并启动
# 访问 http://localhost:8080  admin / ${BOOTSTRAP_PASSWORD:-Gpustack123!}
```

slim 镜像与官方镜像的差异: 无内置 PostgreSQL (默认 SQLite, 可设
`GPUSTACK_DATABASE_URL` 换 PG)、无 Higress 网关 (`GPUSTACK_GATEWAY_MODE=disabled`)、
无 Prometheus/Grafana。生产环境建议沿用官方镜像 + 外部 PG。

### 追加 worker 节点

先在 UI 集群页拿到 worker 注册 token,然后:

```bash
./deploy.sh --add-worker
# 按输出的片段加进 docker-compose.yml,在 worker 机器上 docker compose up -d
```
