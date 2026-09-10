# GPUStack 二开版 — slim 镜像 (全国内源)
#
# 与官方 gpustack/gpustack 镜像的区别:
#   - 不含 Higress 网关 (GPUSTACK_GATEWAY_MODE=disabled)
#   - 不含 Prometheus / Grafana / skopeo / ROCm 库
#   - 体积 ~2GB (官方 ~10GB+), 适合快速体验与二开
#   - 内置 PostgreSQL (官方同款单容器数据库方案)
#   - 构建全程国内源: 基础镜像 daocloud / PyPI 清华 / UI 腾讯云 COS
#
# 构建: docker build --network host -t gpustack-custom:latest -f deploy/Dockerfile .
#   (在仓库根 /root/gpustack 目录执行; 构建上下文为仓库根)

ARG BASE_REGISTRY=docker.m.daocloud.io/library
FROM ${BASE_REGISTRY}/python:3.11-slim AS base

# UI 静态资源版本钉扎 (二开修复): 原版拉 latest.tar.gz, 官方 UI 一更新
# patch 链的字面锚点就漂移 (构建不可复现). 钉到当前验证过的版本;
# 升级 UI 时先本地验证 patch 链再更新此 SHA256.
ARG UI_TARBALL_URL="https://gpustack-ui-1303613262.cos.accelerate.myqcloud.com/releases/latest.tar.gz"
ARG UI_TARBALL_SHA256="df12eeccf16e1cbc859e4343e4e80cd16e8c9eefa293af3f468f3480ed1884da"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_NO_CACHE=1 \
    UV_HTTP_TIMEOUT=500 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"

# 运行期需要的最小系统依赖
# - postgresql: 官方迁移链依赖 PG 语法 (ALTER constraints 等);
#   slim 镜像内置 postgres, 单容器即可跑, 与官方镜像行为一致
# - tini: 优雅信号处理
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \
    ; apt-get update -y \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git procps tini \
        postgresql postgresql-client \
        gosu \
    && rm -rf /var/cache/apt /var/lib/apt/lists/*

# 安装 uv (快速依赖解析; 清华 PyPI, 不依赖境外镜像仓库)
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv

# ---- 构建阶段: 安装 gpustack 源码 + 依赖 ----
FROM base AS builder

# 需要编译的部分依赖
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
        build-essential pkg-config libssl-dev libffi-dev \
    && rm -rf /var/cache/apt /var/lib/apt/lists/*

WORKDIR /workspace
COPY gpustack/pyproject.toml gpustack/uv.lock gpustack/README.md ./
COPY gpustack/gpustack ./gpustack
COPY gpustack/static ./static

# 安装 gpustack 及其依赖 (国内 PyPI 镜像; gpustack-runtime/runner 等)
# hatchling 先装入 venv: --no-build-isolation 要求构建后端已在环境中
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install hatchling \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-build-isolation .

# 下载 UI 静态资源 (腾讯云 COS, 国内直连; tarball 结构为 dist/...)
# 二开修复: 校验 SHA256 (钉版本), 校验失败构建即失败 — 避免 latest
# 漂移导致 patch 锚点失效产出坏镜像.
RUN mkdir -p /workspace/gpustack/ui \
    && curl --retry 3 --retry-connrefused --retry-delay 3 -sSfL \
        "$UI_TARBALL_URL" -o /tmp/ui.tar.gz \
    && echo "$UI_TARBALL_SHA256  /tmp/ui.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/ui.tar.gz --directory /workspace/gpustack/ui --strip-components=1 \
    && rm -f /tmp/ui.tar.gz
# static/ 下的目录图标等随包附带, 合并进 ui 目录
RUN if [ -d /workspace/gpustack/ui ]; then cp -a /workspace/static/. /workspace/gpustack/ui/ 2>/dev/null || true; fi

# 把 UI 放进已安装的 gpustack 包目录 (ui.register 从包内 gpustack/ui 提供静态文件)
# NOTE: 必须 cd / 后再解析包路径 — WORKDIR 是 /workspace, 本地 gpustack/
# 源码目录会被 python 误导入, 导致路径指向源码树而非 venv.
RUN cd / && PKG_UI="$(/opt/venv/bin/python -c 'import gpustack,os;print(os.path.join(os.path.dirname(gpustack.__file__),"ui"))')" \
    && mkdir -p "$PKG_UI" \
    && cp -a /workspace/gpustack/ui/. "$PKG_UI/"

# 二开: 修补编译后的 UI — 删除计费菜单/页面/计量 tab (含 .gz 重生成),
# 组织页替换为控制台跳转, 全部固化进镜像
COPY deploy/patch_ui_dist.py deploy/purge_billing_locale.py deploy/inject_deploy_arch.py deploy/rehash_umi_entry.py /tmp/
RUN cd / && PKG_UI="$(/opt/venv/bin/python -c 'import gpustack,os;print(os.path.join(os.path.dirname(gpustack.__file__),"ui"))')" \
    && UI_DIR="$PKG_UI" python3 /tmp/patch_ui_dist.py \
    && UI_DIR="$PKG_UI" python3 /tmp/purge_billing_locale.py \
    && UI_DIR="$PKG_UI" python3 /tmp/inject_deploy_arch.py \
    && UI_DIR="$PKG_UI" python3 /tmp/rehash_umi_entry.py

# 下载后端工具 (gguf-parser / fastfetch). 官方源走 GitHub, 国内网络
# 不可达时用腾讯云 COS 镜像 (GPUSTACK_TOOLS_DOWNLOAD_BASE_URL).
RUN VIRTUAL_ENV=/opt/venv GPUSTACK_TOOLS_DOWNLOAD_BASE_URL="https://gpustack-1303613262.cos.ap-guangzhou.myqcloud.com" \
    /opt/venv/bin/gpustack download-tools || true

# ---- 运行阶段 ----
FROM base AS runtime

# 二开修复: PGDATA 放进数据卷 (compose 设 GPUSTACK_PGDATA=/var/lib/gpustack/
# postgresql/data); 兼容旧路径由 entrypoint 迁移逻辑处理.
ENV PATH="/opt/venv/bin:$PATH" \
    GPUSTACK_DATA_DIR=/var/lib/gpustack \
    GPUSTACK_GATEWAY_MODE=disabled \
    GPUSTACK_DISABLE_BUILTIN_OBSERVABILITY=true \
    PGDATA=/var/lib/gpustack/postgresql/data \
    GPUSTACK_TOOLS_DOWNLOAD_BASE_URL="https://gpustack-1303613262.cos.ap-guangzhou.myqcloud.com"

# 从构建阶段带走 venv 与 gpustack 包 (含修补后的 UI 和工具)
COPY --from=builder /opt/venv /opt/venv

# 启动脚本: 初始化并拉起内置 PostgreSQL, 再启动 gpustack
COPY deploy/entrypoint-slim.sh /usr/local/bin/entrypoint-slim.sh
RUN chmod +x /usr/local/bin/entrypoint-slim.sh \
    && mkdir -p /var/lib/gpustack /var/lib/postgresql /var/lib/gpustack/postgresql \
    && chown -R postgres:postgres /var/lib/postgresql /var/lib/gpustack/postgresql

VOLUME /var/lib/gpustack
WORKDIR /
EXPOSE 80 443 30080

# tini 作为 init; entrypoint 先起 postgres 再 exec gpustack
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint-slim.sh"]
CMD ["gpustack", "start"]
