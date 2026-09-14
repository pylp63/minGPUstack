#!/usr/bin/env bash
# GPUStack 二开版 — 一键部署脚本
#
# 用法:
#   ./deploy.sh                        # 构建镜像并启动 (单机)
#   ./deploy.sh --skip-build           # 跳过构建直接启动
#   ./deploy.sh --add-worker NAME TOKEN # 输出 worker 节点 compose 片段
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SCRIPT_DIR}/../gpustack"   # 上游源码 (含二开改动)

SKIP_BUILD=false
ACTION="up"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=true; shift ;;
    --add-worker) ACTION="add-worker"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$ACTION" == "add-worker" ]]; then
  cat <<'EOF'
# ========== 方式 A: 容器方式接入 (与主节点同 docker 网络) ==========
# 把以下片段加入 docker-compose.yml 的 services: 下,替换 <NAME> 与 <TOKEN>
  gpustack-worker-<NAME>:
    image: gpustack-custom:latest
    restart: unless-stopped
    command: >
      gpustack start --worker-only
      --server-url http://<SERVER_IP>:8080
      --token <TOKEN>
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - worker-<NAME>-data:/var/lib/gpustack
EOF
  cat <<'EOF'

# ========== 方式 B: BMS 裸金属直装 (节点不在 K8S/不方便跑容器) ==========
# 在裸机节点上直接运行 (pip 方式, 或把镜像里 /opt/venv 拷过去):
#   1) 安装: pip install gpustack   (或 uv pip install gpustack)
#   2) 启动并注册 (自动打 gpustack.io/node-kind=bms 标签):
#        gpustack start --worker-only \
#          --server-url http://<SERVER_IP>:8080 \
#          --token <TOKEN> \
#          --worker-name <NAME>
#   3) (可选) systemd 常驻:
#        systemd-run --unit=gpustack-worker --stay \
#          gpustack start --worker-only --server-url http://<SERVER_IP>:8080 --token <TOKEN>
# 接入后节点页「部署形态」列显示 BMS 裸金属 (橙色徽标)。
EOF
  exit 0
fi

cd "$SCRIPT_DIR"

if [[ "$SKIP_BUILD" != "true" ]]; then
  echo "==> 构建 gpustack-custom 镜像 (首次构建较慢)..."
  # 二开修复: Dockerfile 在 deploy/ 下, 构建上下文是仓库根
  docker build --network host -t gpustack-custom:latest \
    -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR/.."
fi

echo "==> 启动 GPUStack..."
docker compose up -d

echo ""
echo "==> 完成. 访问 http://localhost:${GPUSTACK_PORT:-8080}"
echo "    初始管理员: admin / admin"
