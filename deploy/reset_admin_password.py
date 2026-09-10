#!/usr/bin/env python3
"""GPUStack 二开版 — 直接 SQL 重置 admin 密码 (不经 ORM, 避免缓存层依赖).

用法: python3 reset_admin_password.py [DB_URL] [新密码] [--username NAME]
默认: postgresql://postgres@127.0.0.1:5432/gpustack?sslmode=disable / admin / admin
"""
import asyncio
import sys

import asyncpg
from argon2 import PasswordHasher


async def main():
    db_url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "postgresql://postgres@127.0.0.1:5432/gpustack?sslmode=disable"
    )
    new_pass = sys.argv[2] if len(sys.argv) > 2 else "admin"
    username = "admin"

    # asyncpg 不认 sslmode 参数, 剥掉
    if "sslmode=disable" in db_url:
        db_url = db_url.split("?")[0]

    hashed = PasswordHasher().hash(new_pass)

    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow(
            """
            SELECT up.id, up.owner_principal_id
            FROM user_passwords up
            JOIN principals p ON p.id = up.owner_principal_id
            WHERE p.name = $1 AND p.kind = 'USER' AND p.is_admin = TRUE
            ORDER BY up.id DESC LIMIT 1
            """,
            username,
        )
        if row is None:
            print(f"!! 没有找到管理员用户 {username!r}")
            return 1
        await conn.execute(
            """
            UPDATE user_passwords
            SET hashed_secret = $2, algorithm = 'ARGON2',
                require_password_change = FALSE, updated_at = now()
            WHERE id = $1
            """,
            row["id"],
            hashed,
        )
        print(
            f"OK: 用户 {username!r} (principal {row['owner_principal_id']}) "
            f"密码已重置为 {new_pass!r}"
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
