#!/bin/bash
# GPUStack 二开版 — 镜像构建 (脱离 agent 会话, nohup 调用)
# 产物: gpustack-custom:latest (单 tag; 构建前清理旧版镜像)
set -eo pipefail
cd /mnt/gpustack
echo "[build] start $(date)"

# 清理旧版本镜像, 只保留本次构建产物 (含 dangling 残留层)
docker rmi -f gpustack-custom:latest gpustack-custom:v2 2>/dev/null || true
docker images -q --filter "dangling=true" | grep -v "$(docker ps -q --filter ancestor=gpustack-custom:latest 2>/dev/null)" 2>/dev/null | xargs -r docker rmi -f 2>/dev/null || true

docker build --network host \
  -t gpustack-custom:latest \
  -f deploy/Dockerfile . 
rc=$?
echo "[build] exit=$rc $(date)"
exit $rc
