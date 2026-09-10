#!/bin/bash
# GPUStack 二开版 — 镜像构建 (脱离 agent 会话, nohup 调用)
# 产物: gpustack-custom:latest / :v2
set -eo pipefail
cd /mnt/gpustack
echo "[build] start $(date)"
docker build --network host \
  -t gpustack-custom:v2 \
  -t gpustack-custom:latest \
  -f deploy/Dockerfile . 
rc=$?
echo "[build] exit=$rc $(date)"
exit $rc
