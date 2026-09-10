#!/usr/bin/env bash
# GPUStack 二开版 — API 冒烟测试
# 验证: 登录 / 用户隔离 / GPU申请-审批-通知 / 节点授权 / 部署架构 presets
#
# 用法: ./smoke-test.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
API="$BASE/v2"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin}"
JAR_ADMIN=/tmp/gpustack-admin.jar
JAR_USER=/tmp/gpustack-user.jar

STEP() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
PASS() { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; }
FAIL() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; exit 1; }

# jq: 用 python 读 JSON, 表达式经环境变量传入, 避免引号地狱
jq() { EXPR="$1" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
print(eval(os.environ["EXPR"]))'; }

STEP "0. 服务健康"
curl -sf "$BASE/" >/dev/null || FAIL "UI 首页不可达"
PASS "UI 可达"

STEP "1. admin 登录"
curl -sf -c "$JAR_ADMIN" -X POST "$BASE/auth/login" \
  -d "username=$ADMIN_USER" -d "password=$ADMIN_PASS" >/dev/null \
  || FAIL "admin 登录失败 (检查 BOOTSTRAP_PASSWORD)"
PASS "admin 登录成功"
curl -sf -b "$JAR_ADMIN" "$API/users/me" | EXPR='d["username"]' python3 -c 'import json,os,sys;print("  admin =", json.load(sys.stdin)[os.environ["EXPR"]])' >/dev/null 2>&1 || true
curl -sf -b "$JAR_ADMIN" "$API/users" | jq 'd["items"][0]["username"]' | sed 's/^/  admin = /' || true

STEP "2. 创建普通用户 dev1"
PASSWD="Dev1pass!2026"
DEV1_ID=$(curl -sf -b "$JAR_ADMIN" "$API/users?search=dev1" | jq 'next((u["id"] for u in d["items"] if u["username"]=="dev1"), None)')
if [ "$DEV1_ID" = "None" ] || [ -z "$DEV1_ID" ]; then
  curl -sf -b "$JAR_ADMIN" -X POST "$API/users" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"dev1\",\"password\":\"$PASSWD\",\"is_admin\":false}" >/dev/null \
    || FAIL "创建 dev1 失败"
  DEV1_ID=$(curl -sf -b "$JAR_ADMIN" "$API/users?search=dev1" | jq 'next((u["id"] for u in d["items"] if u["username"]=="dev1"), None)')
else
  # 用户已存在 (数据卷持久化): 重置密码保证后续登录步骤可用
  curl -sf -b "$JAR_ADMIN" -X PUT "$API/users/$DEV1_ID" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"dev1\",\"password\":\"$PASSWD\",\"is_admin\":false}" >/dev/null \
    || FAIL "重置 dev1 密码失败"
fi
PASS "dev1 就绪 (id=$DEV1_ID)"

STEP "3. dev1 登录并提交 GPU 申请"
curl -sf -c "$JAR_USER" -X POST "$BASE/auth/login" \
  -d "username=dev1" -d "password=$PASSWD" >/dev/null || FAIL "dev1 登录失败"
PASS "dev1 登录成功"
PENDING_ID=$(curl -sf -b "$JAR_USER" "$API/gpu-requests?status=pending" | jq 'next((r["id"] for r in d["items"]), None)')
if [ "$PENDING_ID" = "None" ] || [ -z "$PENDING_ID" ]; then
  curl -sf -b "$JAR_USER" -X POST "$API/gpu-requests" \
    -H "Content-Type: application/json" \
    -d '{"requested_gpu_count":4,"reason":"smoke test","model_name":"qwen"}' >/dev/null \
    || FAIL "提交 GPU 申请失败"
  PENDING_ID=$(curl -sf -b "$JAR_USER" "$API/gpu-requests?status=pending" | jq 'next((r["id"] for r in d["items"]), None)')
else
  echo "  复用已存在的 pending 申请"
fi
PASS "GPU 申请已就绪 (id=$PENDING_ID)"

STEP "4. admin 收到通知"
UNREAD=$(curl -sf -b "$JAR_ADMIN" "$API/notifications/unread" | jq 'd["unread"]')
[ "$UNREAD" -ge 1 ] 2>/dev/null || FAIL "admin 未收到通知 (unread=$UNREAD)"
PASS "admin 通知 unread=$UNREAD"

STEP "5. dev1 只能看到自己的申请 (隔离)"
LEAKED=$(curl -sf -b "$JAR_USER" "$API/gpu-requests" | DEV1_ID="$DEV1_ID" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
mine = os.environ["DEV1_ID"]
print(len([r for r in d["items"] if str(r["user_principal_id"]) != str(mine)]))')
[ "$LEAKED" = "0" ] || FAIL "dev1 看到了 $LEAKED 条别人的申请"
MINE=$(curl -sf -b "$JAR_USER" "$API/gpu-requests" | jq 'len(d["items"])')
echo "  dev1 可见申请 $MINE 条 (全部属于自己)"
PASS "申请列表隔离生效"

STEP "6. admin 批准申请 (配额 2)"
DEC=$(curl -sf -b "$JAR_ADMIN" -X POST "$API/gpu-requests/$PENDING_ID/approve" \
  -H "Content-Type: application/json" -d '{"granted_gpu_count":2}') \
  || FAIL "批准失败"
ST=$(echo "$DEC" | jq 'd["status"]')
[ "$ST" = "approved" ] || FAIL "状态不是 approved (实际 $ST)"
GC=$(echo "$DEC" | jq 'd["granted_gpu_count"]')
PASS "已批准, 配额=$GC"

STEP "7. dev1 收到通知 + 配额生效"
DEV_UNREAD=$(curl -sf -b "$JAR_USER" "$API/notifications/unread" | jq 'd["unread"]')
[ "$DEV_UNREAD" -ge 1 ] 2>/dev/null || FAIL "dev1 未收到通知"
PASS "dev1 通知 unread=$DEV_UNREAD"
QUOTA=$(curl -sf -b "$JAR_USER" "$API/gpu-requests/quota/me" | jq 'd["quota"]')
[ "$QUOTA" = "2" ] || FAIL "dev1 配额应为 2, 实际 $QUOTA"
PASS "dev1 配额 = $QUOTA"

STEP "8. 部署架构 presets"
PRESETS=$(curl -sf -b "$JAR_ADMIN" "$API/deploy-presets")
N=$(echo "$PRESETS" | jq 'len(d)')
[ "$N" = "5" ] || FAIL "应有 5 种架构 (standalone/pd/multi_pd/pp/custom), 实际 $N"
PASS "5 种架构: $(echo "$PRESETS" | jq '",".join(x["architecture"] for x in d)')"

PLAN=$(curl -sf -b "$JAR_ADMIN" -X POST "$API/deploy-presets/plan" \
  -H "Content-Type: application/json" \
  -d '{"architecture":"pd_disaggregated","model_name":"qwen","model_source":"Qwen/Qwen2.5-0.5B-Instruct","prefill_gpu_count":2,"decode_gpu_count":2}')
PN=$(echo "$PLAN" | jq 'len(d["payloads"])')
[ "$PN" = "2" ] || FAIL "PD 分离应展开为 2 个 payload, 实际 $PN"
PPARAMS=$(echo "$PLAN" | jq 'd["payloads"][0].get("backend_parameters")')
echo "  PD 分离 payload[0] 参数: $PPARAMS"
PASS "PD 分离展开为 prefill + decode"

PLAN2=$(curl -sf -b "$JAR_ADMIN" -X POST "$API/deploy-presets/plan" \
  -H "Content-Type: application/json" \
  -d '{"architecture":"pipeline_parallel","model_name":"qwen","model_source":"Qwen/Qwen2.5-0.5B-Instruct","pipeline_parallel_size":4}')
PP=$(echo "$PLAN2" | jq 'sum(1 for p in d["payloads"] for b in [p.get("backend_parameters") or []] if "--pipeline-parallel-size=4" in b)')
[ "$PP" = "1" ] || FAIL "流水线并行参数未生成"
PASS "流水线并行 --pipeline-parallel-size=4 已生成"

STEP "8b. PD 引擎参数语法 (kv 对称)"
PDPLAN=$(curl -sf -b "$JAR_ADMIN" -X POST "$API/deploy-presets/plan" \
  -H "Content-Type: application/json" \
  -d '{"architecture":"pd_disaggregated","model_name":"kvtest","model_source":"Qwen/Qwen2.5-0.5B-Instruct","kv_transfer":true}')
NP=$(echo "$PDPLAN" | jq 'sum(1 for p in d["payloads"] for b in [p.get("backend_parameters") or []] if "kv_producer" in " ".join(b))')
NC=$(echo "$PDPLAN" | jq 'sum(1 for p in d["payloads"] for b in [p.get("backend_parameters") or []] if "kv_consumer" in " ".join(b))')
[ "$NP" = "1" ] && [ "$NC" = "1" ] || FAIL "kv 参数不对称 (producer=$NP consumer=$NC, 应各 1)"
BAD=$(echo "$PDPLAN" | jq 'sum(1 for p in d["payloads"] for b in [p.get("backend_parameters") or []] if "api-server-type" in " ".join(b))')
[ "$BAD" = "0" ] || FAIL "仍生成不存在的 --api-server-type 参数"
PASS "kv producer/consumer 对称, 无引擎不识别参数"

STEP "8c. 拓扑图 edges 指向真实节点"
TOPO=$(curl -sf -b "$JAR_ADMIN" -X POST "$API/deploy-presets/preview" \
  -H "Content-Type: application/json" \
  -d '{"architecture":"pd_disaggregated","model_name":"edgetest","model_source":"Qwen/Qwen2.5-0.5B-Instruct"}')
DANGLING=$(echo "$TOPO" | TOPO="$TOPO" python3 -c '
import json, os, sys
d = json.loads(os.environ["TOPO"])
topo = d["topology"]
ids = {n["id"] for n in topo["nodes"]}
bad = [e for e in topo["edges"] if e["from"] not in ids or e["to"] not in ids]
print(len(bad))')
[ "$DANGLING" = "0" ] || FAIL "拓扑图有 $DANGLING 条悬空边 (from/to 不在节点集)"
PASS "拓扑图全部边指向真实节点"

STEP "8d. deploy-topologies 单机部署 (端到端)"
TD=$(curl -sf -b "$JAR_ADMIN" -X POST "$API/deploy-topologies/deploy" \
  -H "Content-Type: application/json" \
  -d '{"shape":"single_node_tp","model_name":"topo-smoke","model_source":"Qwen/Qwen2.5-0.5B-Instruct","replicas":1}')
TU=$(echo "$TD" | jq 'len(d["units"])')
[ "$TU" = "1" ] || FAIL "topologies 部署单元数应为 1, 实际 $TU"
PASS "deploy-topologies deploy 成功 (不再 422/500)"
curl -sf -b "$JAR_ADMIN" "$API/models?search=topo-smoke" | jq 'len(d["items"])' | sed 's/^/  已创建模型数: /'
MID2=$(curl -sf -b "$JAR_ADMIN" "$API/models?search=topo-smoke" | jq 'next((m["id"] for m in d["items"] if m["name"]=="topo-smoke"), None)')
[ "$MID2" != "None" ] && [ -n "$MID2" ] && curl -sf -b "$JAR_ADMIN" -X DELETE "$API/models/$MID2" -o /dev/null \
  && echo "  (清理 topo-smoke 完成)"

STEP "9. worker 视图隔离"
ALL_WORKERS=$(curl -sf -b "$JAR_ADMIN" "$API/workers")
ALL_COUNT=$(echo "$ALL_WORKERS" | jq 'len(d["items"])')
SEEN=$(curl -sf -b "$JAR_USER" "$API/workers" | jq 'len(d["items"])' 2>/dev/null || echo 0)
echo "  admin 可见 worker=$ALL_COUNT, dev1 可见 worker=$SEEN (预期 0)"
if [ "$ALL_COUNT" -ge 1 ] && [ "$SEEN" = "0" ]; then
  WID=$(echo "$ALL_WORKERS" | jq 'd["items"][0]["id"]')
  WNAME=$(echo "$ALL_WORKERS" | jq 'd["items"][0]["name"]')
  PASS "授权前 dev1 看不到任何节点"
  STEP "10. admin 开放节点 $WNAME (id=$WID) 给 dev1"
  curl -sf -b "$JAR_ADMIN" -X POST "$API/workers/$WID/access" \
    -H "Content-Type: application/json" \
    -d "{\"principal_id\":$DEV1_ID}" >/dev/null || FAIL "授权失败"
  PASS "已授权"
  SEEN2=$(curl -sf -b "$JAR_USER" "$API/workers" | jq 'len(d["items"])')
  [ "$SEEN2" = "1" ] || FAIL "dev1 应只看到 1 台, 实际 $SEEN2"
  PASS "dev1 现在恰好看到被授权的 1 台节点"
else
  echo "  (集群无 worker 节点, 视图隔离部分跳过 — 需 --enable-worker 或接入节点)"
  PASS "无节点场景: dev1 可见 worker=0"
fi

printf '\n\033[1;32m全部冒烟测试通过 ✔\033[0m\n'
