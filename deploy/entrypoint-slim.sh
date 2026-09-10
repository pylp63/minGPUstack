#!/bin/bash
# GPUStack slim 镜像 entrypoint: 拉起内置 PostgreSQL, 再 exec gpustack
# 参考 official pack/entrypoint.sh 的 postgres 初始化逻辑 (简化版)
set -e

DATA_DIR="${GPUSTACK_DATA_DIR:-/var/lib/gpustack}"
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PG_DB="gpustack"
# 内置 PG 端口: host 网络模式下 5432 常被占用, 默认 5433
PG_PORT="${GPUSTACK_EMBEDDED_PG_PORT:-5433}"

# Debian 的 postgresql 包把版本化二进制放 /usr/lib/postgresql/<N>/bin,
# 不在默认 PATH — 加入之 (initdb / pg_ctl / postgres 等)
for pgbin in /usr/lib/postgresql/*/bin; do
    [ -d "$pgbin" ] && export PATH="$pgbin:$PATH"
done

# 已配置外部数据库则跳过内置 postgres
if [ -n "${GPUSTACK_DATABASE_URL:-}" ]; then
    echo "[entrypoint] GPUSTACK_DATABASE_URL set, skipping embedded postgres"
    exec "$@"
fi

mkdir -p "$DATA_DIR" "$PGDATA"
chown -R postgres:postgres "$(dirname "$PGDATA")"

# 首次启动: initdb + 创建库
if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "[entrypoint] initializing postgres cluster at $PGDATA"
    gosu postgres initdb -D "$PGDATA" -E UTF8 --locale=C.UTF-8 >/dev/null
    # 只监听本机 — 数据库不出容器
    cat >> "$PGDATA/postgresql.conf" <<EOF
listen_addresses = '127.0.0.1'
port = $PG_PORT
EOF
    cat > "$PGDATA/pg_hba.conf" <<EOF
local all all trust
host  all all 127.0.0.1/32 trust
EOF
    chown postgres:postgres "$PGDATA/postgresql.conf" "$PGDATA/pg_hba.conf"
fi

# 起 postgres (前台托管: 存 pid, 退出时停库). 日志写 PGDATA (postgres 属主可写)
gosu postgres pg_ctl -D "$PGDATA" -w -t 60 -l "$PGDATA/postgres.log" start
trap 'gosu postgres pg_ctl -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true' EXIT INT TERM

# 建库 (幂等)
gosu postgres psql -h 127.0.0.1 -p "$PG_PORT" -tc "SELECT 1 FROM pg_database WHERE datname='$PG_DB'" \
    | grep -q 1 || gosu postgres createdb -h 127.0.0.1 -p "$PG_PORT" "$PG_DB"

# gpustack 连接串指向内置 PG (仅当外部连接串未配置时)
export GPUSTACK_DATABASE_URL="${GPUSTACK_DATABASE_URL:-postgresql://postgres@127.0.0.1:$PG_PORT/$PG_DB?sslmode=disable}"

echo "[entrypoint] postgres ready, starting: $*"
exec "$@"
