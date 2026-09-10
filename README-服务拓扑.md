# GPUStack 二次开发 — 服务拓扑与并行策略

在开源 GPUStack 基础上补全的企业级服务编排能力。三层架构严格分层：

```
一级 资源拓扑 (单机部署 / 多机部署 / 单机多卡)
 └─ 三级 PD 分离 (服务级, 外层; 默认关闭, 开启默认 1P1D, N×P + M×D 可自定义)
      └─ 二级 并行策略 (实例级, 内层; TP / PP / EP / DP, 每组独立配置)
```

PD 分离操作对象是「模型实例池」；TP/PP/EP 操作对象是「单个模型实例」。
PP 永远嵌套在 P/D 组内部（组 = 实例容器），不存在 PP 在 PD 之上的配置。

## 审计结论（企业版封锁机制）

后端源码无许可证校验、无 feature flag、无试用期逻辑——企业版功能
（计费导出、企业组织管理等）以**闭源插件**形式存在，代码不在仓库里，
属于「需新增实现」。UI 侧的 "GPUStack 企业版可用" 标记来自插件注册表
探测（`E.get("enterprise")`），开源部署下这些入口本来就不渲染。
本项目按 Apache-2.0 合规地**自行实现**了缺失功能，未触碰任何闭源代码。

## 新增功能

### 1. 服务拓扑规划 API — `/v2/serving-topology`

| 端点 | 说明 |
|------|------|
| `POST /v2/serving-topology/plan` | 三层校验 + 组数换算 + 机器布局（dry-run） |
| `POST /v2/serving-topology/deploy` | plan + 走标准 create_model 链路创建模型（含配额/租户） |

请求体核心字段：

```jsonc
{
  "model_name": "qwen", "model_source": "Qwen/Qwen2.5-7B-Instruct",
  "topology": "multi_node",            // 一级: single_node | single_node_multi_gpu | multi_node
  "mode": "resource_pool",             // resource_pool | declarative (两种交互结果一致)
  "enabled": true,                     // 三级: PD 分离开关 (默认 false; 开启默认 1P1D)
  "prefill_groups": 2, "decode_groups": 2,   // N×P + M×D (≥1)
  "prefill_parallel": {                // 二级: P 组实例策略 (每组独立, 可与 D 组异构)
    "tensor_parallel_size": 8, "pipeline_parallel_size": 2,
    "expert_parallel_size": 1, "data_parallel_replicas": 1
  },
  "decode_parallel":  { "tensor_parallel_size": 1 },
  "prefill_pool":  { "machines_allocated": 4, "gpus_per_node": 8, "vram_per_gpu_mb": 24576 },
  "decode_pool":   { "machines_allocated": 2, "gpus_per_node": 8 }
}
```

响应含每侧换算结果：`groups_formed / machines_used / machines_idle /
gpus_used / gpus_idle / machines_per_group / cross_machine /
instances_per_machine / notice`，校验失败返回 422 + 人类可读原因。

### 2. P/D 组与物理资源换算（核心规则）

```
单组所需 GPU   = TP × PP × EP
单组占用机器数 = ceil(单组所需 GPU / 单节点可用 GPU 数)
可形成组数     = floor(分配机器数 / 单组占用机器数)
剩余机器数     = 分配机器数 mod 单组占用机器数
```

- 换算由物理资源与并行策略**推算**，不采纳用户主观组数；余量必须显式
  提示（"已分配 3 台机, 单组占用 2 台, 只能形成 1 个组, 剩余 1 台
  不足以组成第 2 组"），禁止把闲置机静默计入组数。
- **单机内 PP vs 跨机 PP** 由引擎按 `TP×PP×EP` 与单节点 GPU 数自动判定
  （`cross_machine` 字段），UI 据此明示。
- **DP 复用**默认关闭；显式开启后单机打包
  `floor(单节点GPU数 / (TP×PP×EP))` 个同策略副本（replicas 承载），
  UI 显示「已榨干 / 剩余 N 卡空闲」。
- **声明式**（要 N 组 → 反推机器数，不足报错并给出所需台数）与
  **资源池式**（分配 X 台 → 算组数与余量）结果一致。
- 每侧（P/D）独立换算；`vram_per_gpu_mb` 提供时做显存校验，
  不满足给出「需 N 台/组或调整并行策略」提示。

### 3. PD 分离部署语义

- 每组 = 一个独立 Model 行（`<name>-prefill-N` / `<name>-decode-N`），
  组内并行策略写入 `backend_parameters`，`distributed_inference_across_workers`
  按是否跨机自动设置——完全复用官方调度器与分布式推理链路。
- 引擎互连参数按引擎生成：
  vLLM `--kv-transfer-config`（producer/consumer 对称，kv_rank 0/1）、
  SGLang `--disaggregation-mode=prefill|decode`。
- 异构支持：P/D 组策略独立（P 组 PP=2、D 组 PP=4 合法）。
- 典型链路：权重单机放不下 → 组内 TP=8,PP=2 跨 2 机 → 叠加 2P2D
  → P 侧 4 台 + D 侧 4 台，共 8 台（`test_pp_nested_inside_pd_groups`）。

### 4. 单机 → 多机自动升级提示

`plan` 响应携带 `upgrade_hint`：单机 GPU 数不足以容纳实例时提示切换
「多机部署」并说明跨机台数，**已配置的并行策略保持不变**
（`suggest_topology_upgrade`）。

### 5. 前端

主 UI「模型服务」菜单新增 **服务拓扑** 入口（`/models/serving-topology`
→ `/console/serving_topology.html`），单文件页面与官方风格一致：

- 拓扑选择（一级）→ 并行策略 TP/PP/EP/DP（二级）→ PD 分组卡片（三级）；
- PD 开启后并行策略下沉到每张 P/D 组卡片内，`+`/`−` 增减组数，
  卡片实时显示换算结果（单组卡数 / 跨机台数 / 可形成组数 / 余量）；
- 单机放不下时顶部提示切换多机（策略保留）；
- 「仅预览计划 / 生成部署计划并部署」直连 `/v2/serving-topology` API。

## 使用示例

```bash
# 2P2D, 每组 TP=8 PP=2 (16卡/组, 跨2机), P/D 各分配 4 台
curl -X POST $API/v2/serving-topology/plan -H "Authorization: Bearer ***" \
  -H 'Content-Type: application/json' -d '{
    "model_name": "qwen", "model_source": "Qwen/Qwen2.5-72B-Instruct",
    "topology": "multi_node", "mode": "resource_pool",
    "enabled": true, "prefill_groups": 2, "decode_groups": 2,
    "prefill_parallel": {"tensor_parallel_size": 8, "pipeline_parallel_size": 2},
    "decode_parallel":  {"tensor_parallel_size": 8, "pipeline_parallel_size": 2},
    "prefill_pool": {"machines_allocated": 4, "gpus_per_node": 8},
    "decode_pool":  {"machines_allocated": 4, "gpus_per_node": 8}}'
# -> prefill.groups_formed=2, decode.groups_formed=2, 8 台机全部用满

# 声明式要 2 个 P 组但只有 3 台 (单组占 2 台) -> 422:
# "Prefill 组不足: 需要 4 台机才能组成 2 个组, 当前只分配了 3 台."
```

## 代码位置

| 文件 | 内容 |
|------|------|
| `gpustack/schemas/deploy_topology.py` | 三层模型 + 换算引擎（纯函数） |
| `gpustack/routes/serving_topology.py` | plan/deploy API + payload 展开 |
| `gpustack/ui_console/serving_topology.html` | 前端配置页 |
| `deploy/patch_ui_dist.py` | 主 UI 菜单注入（id 51 服务拓扑） |
| `tests/api/test_serving_topology.py` | 24 个单测（规格表/嵌套/余量/DP/引擎参数） |

## 测试

- 单元测试 `tests/api/test_serving_topology.py`：24/24 通过
  （规格换算表 6 例、模式一致性、DP 开关、PP 嵌套 PD、异构 PP、
  payload 可构造 ModelCreate、引擎参数对称、声明式不足拒绝）。
- 仓库全量 `tests/api/`：286/286 通过，零回归。
- 端到端（新镜像 + 重建容器）：worker 注册 0 失败；验收场景 7
  （3 台 P 机 + 16 卡/组 → 1 组 + 1 台闲置 + 提示）、场景 8
  （3 台 P 机 + 2 卡/组 → 3 组）实测正确；冒烟测试全过。

## 路由策略与 KV 传递的现状说明

N:M 请求路由与 KV Cache 传递复用现有基础设施：实例间路由由
GPUStack 既有 model-route / 负载均衡承载（P/D 组作为独立模型接入），
KV 传输参数按引擎注入（vLLM PyNcclConnector / SGLang disaggregation），
同机 KV 共享可复用已集成的 LMCache/HiCache。P/D 组增减 = 增删 Model
行，官方控制器对实例故障自动重试/摘流，在线请求不受影响。
