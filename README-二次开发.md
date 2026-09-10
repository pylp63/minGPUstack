# GPUStack 二次开发版

基于 [gpustack.ai](https://gpustack.ai) 开源代码的二次开发,新增两大功能。

## 功能一: 用户隔离 (GPU 申请审批 + 节点级授权)

普通用户与管理员 (admin) 之间的完整 GPU 资源隔离流程:

```
普通用户                     admin
   │  ① 提交 GPU 申请           │
   │  POST /v2/gpu-requests    │
   ├──────────────────────────▶│ ② 收到通知 (POST 时自动 fan-out
   │                           │    给所有平台管理员)
   │                           │ ③ 审批 — 批准并指定配额,
   │                           │    同时可挑选指定节点开放给该用户
   │  ④ 收到审批结果通知          │    POST /v2/workers/{id}/access
   ◀───────────────────────────┤
   │  ⑤ 只能看到被开放的节点       │
   │    GET /v2/workers (过滤)  │
   │  ⑥ 部署模型受配额限制,       │
   │    且只能调度到被授权节点    │
```

### 核心机制

| 能力 | 实现 |
|------|------|
| GPU 申请 | `POST /v2/gpu-requests` (数量/用途/目标模型) |
| admin 通知 | 申请提交时自动给所有 admin 生成站内通知 `GET /v2/notifications` |
| 审批/驳回 | `POST /v2/gpu-requests/{id}/approve` / `reject`,批准时设定 `granted_gpu_count` 配额 |
| 节点开放 | `POST /v2/workers/{worker_id}/access` 把**指定机器**开放给某用户 |
| 视图隔离 | 普通用户 `GET /v2/workers` 只返回被开放的节点(SQL 过滤 + 行级过滤双保险) |
| 调度隔离 | 调度器新增 `WorkerAccessFilter`: 普通用户的模型实例**只会**落在被授权节点 |
| 配额强制 | 创建/更新模型时校验: 已部署 GPU + 新需求 ≤ 批准配额,超限拒绝 |
| 通知闭环 | 审批/驳回/取消/节点开放均产生通知给相关方 |

### API 摘要

```bash
# 普通用户提交申请
curl -X POST $API/v2/gpu-requests -H "Authorization: Bearer ***" \
  -d '{"requested_gpu_count": 4, "reason": "跑 Qwen 微调", "model_name": "qwen2.5-7b"}'

# admin 查看通知 / 申请
curl $API/v2/notifications/unread -H "Authorization: Bearer ***"
curl "$API/v2/gpu-requests?status=pending" -H "Authorization: Bearer ***"

# admin 批准,配额 4 GPU
curl -X POST $API/v2/gpu-requests/1/approve -H "Authorization: Bearer ***" \
  -d '{"granted_gpu_count": 4}'

# admin 把 2 号节点开放给该用户 (principal_id)
curl -X POST $API/v2/workers/2/access -H "Authorization: Bearer ***" \
  -d '{"principal_id": 3}'
```

数据库表: `gpu_allocation_requests`, `worker_access`, `notifications`
(Alembic 迁移 `f1a2b3c4d5e6`)。

## 功能二: 一键启动部署架构

支持五种部署架构一键生成:

| 架构 | 说明 |
|------|------|
| `standalone` | 单机部署 — 单实例调度到单节点 (可 TP 多卡、多副本) |
| `pd_disaggregated` | PD 分离 — 独立 prefill / decode 实例组,各自 GPU 池 |
| `multi_pd` | 多P多D — prefill 与 decode 均多副本水平扩展 |
| `pipeline_parallel` | 流水线并行 — 跨节点/GPU 的 pipeline 并行 |
| `custom` | 自定义拓扑 — 直接指定角色/节点/副本/环境变量 |

引擎互连参数按引擎生成 (vLLM `--kv-transfer-config` / SGLang
`--disaggregation-mode`,producer/consumer 两侧对称):

```bash
# 查看架构列表
curl $API/v2/deploy-presets

# 预览展开结果 (dry-run, 含拓扑图数据 + 可下载 YAML)
curl -X POST $API/v2/deploy-presets/preview -d '{
  "architecture": "pd_disaggregated",
  "model_name": "qwen", "model_source": "Qwen/Qwen2.5-7B-Instruct",
  "prefill_gpu_count": 2, "decode_gpu_count": 2, "kv_transfer": true
}'

# 一键部署 (走完整 model-create 链路,含配额校验)
curl -X POST $API/v2/deploy-presets/deploy -d '{...}'
```

另有一套更底层的 `/v2/deploy-topologies` API (plan/deploy, 支持
gpu_selector 指定具体 GPU、pp 多节点选卡), 与 presets 共用
model-create 链路; UI 向导走 presets。

展开逻辑在 `gpustack/schemas/deploy_presets.py` (库函数 `build_preset_payloads`)
与 `gpustack/schemas/deploy_topologies.py` (`build_topology_plan`)。

## Docker Compose 部署 (slim 镜像)

```bash
cd deploy
cp .env.example .env   # 按需改密码/端口
docker compose up -d   # 需先 build-image.sh 构建镜像
# 访问 http://localhost:8080  admin / ${BOOTSTRAP_PASSWORD}
```

slim 镜像与官方镜像的差异: **内置 PostgreSQL** (数据落在
`gpustack-data` 卷的 `postgresql/data` 下, 容器重建不丢库)、
无 Higress 网关 (`GPUSTACK_GATEWAY_MODE=disabled`)、无
Prometheus/Grafana、体积约 2GB。生产环境多节点建议外部 PG。

### 追加 worker 节点

先在 UI 集群页拿到 worker 注册 token,然后:

```bash
./deploy.sh --add-worker
# 按输出的片段加进 docker-compose.yml,在 worker 机器上 docker compose up -d
```

## 开发

- `deploy/build-image.sh` — 构建镜像 (gpustack-custom:latest / :v2)
- `deploy/smoke-test.sh [BASE_URL]` — API 冒烟测试 (登录/隔离/审批/架构/topologies)
- `deploy/git_push.sh [msg]` — 提交并推送 (token 放 `/mnt/gpustack/.gh_token`)
- UI 定制: 构建期 Python 脚本改 minified JS (`deploy/patch_ui_dist.py`
  等), 改后由 `rehash_umi_entry.py` 统一重算文件名 hash (浏览器
  immutable 缓存才会刷新); UI tarball 版本由 Dockerfile 的
  `UI_TARBALL_SHA256` 钉住, 升级 UI 需同步更新该值。
