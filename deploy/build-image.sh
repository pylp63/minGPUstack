#!/bin/bash
# GPUStack 二开版 — 镜像构建 (脱离 agent 会话, nohup 调用)
# 产物: gpustack-custom:latest (单 tag; 构建前清理旧版镜像)
set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"
echo "[build] start $(date)"
echo "[build] repo root: $REPO_ROOT"

# 清理旧版本镜像, 只保留本次构建产物 (含 dangling 残留层)
docker rmi -f gpustack-custom:latest gpustack-custom:v2 2>/dev/null || true
docker images -q --filter "dangling=true" | grep -v "$(docker ps -q --filter ancestor=gpustack-custom:latest 2>/dev/null)" 2>/dev/null | xargs -r docker rmi -f 2>/dev/null || true

# 构建前预检: 注入片段在官方上下文中的语法 (宿主 node; 容器内无 node,
# 构建期守卫只有括号计数 — 历史上漏过数组元素缺逗号的语法错, 浏览器才炸)
node "$SCRIPT_DIR/check_inject.js" || exit 1

docker build --network host \
  -t gpustack-custom:latest \
  -f deploy/Dockerfile .
rc=$?
echo "[build] exit=$rc $(date)"
exit $rc