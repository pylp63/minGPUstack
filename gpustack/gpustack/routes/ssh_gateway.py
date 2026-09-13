# SSH 网关反代 (二开): /v2/ssh-gw/* -> Go ssh-gateway (127.0.0.1:10170)
#
# 功能: 节点页「SSH」按钮的完整交互终端通道。
#   POST /v2/ssh-gw/ticket/{id}      — 签发一次性 WS ticket (GPUStack 登录态 + worker 可见性校验)
#   POST /v2/ssh-gw/credentials/{id} — 弹窗录入的节点 SSH 凭据 (先由网关实际登录验证, 通过才保存)
#   WS   /v2/ssh-gw/ws               — WebSocket 终端 (xterm.js <-> Go 网关 <-> 节点 PTY)
#
# 安全: ticket 由本路由校验权限后转发签发; WS 升级请求带 ticket,
# 网关侧一次性消费 (防重放)。网关只监听 127.0.0.1, 不对外。

import logging
from typing import Optional

import aiohttp
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from sqlmodel.ext.asyncio.session import AsyncSession

from gpustack.api.exceptions import InvalidException, NotFoundException
from gpustack.api.tenant import assert_resource_visible
from gpustack.server.deps import SessionDep, TenantContextDep
from gpustack.schemas.workers import Worker

logger = logging.getLogger(__name__)

router = APIRouter()

SSHGW_BASE = "http://127.0.0.1:10170"


async def _worker_and_ip(worker_id: int, session, ctx):
    """取 worker + ticket 用 IP (fake-IP 回落 127.0.0.1, 与 ssh-exec 同款)。"""
    worker = await Worker.one_by_id(session, worker_id)
    if worker is not None and worker.deleted_at is not None:
        worker = None
    if worker is None:
        raise NotFoundException(message="worker not found")
    assert_resource_visible(ctx, worker, not_found_message="worker not found")
    return worker, (worker.ip or "")


def _fallback_ip(ip: str) -> str:
    import ipaddress

    try:
        if ipaddress.ip_address(ip) in ipaddress.ip_network("198.18.0.0/15"):
            return "127.0.0.1"
    except ValueError:
        pass
    return ip


@router.post("/ticket/{worker_id}")
async def ssh_gw_ticket(
    worker_id: int,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """签发 SSH 终端一次性 ticket。权限与 ssh-exec 同级。"""
    worker, ip = await _worker_and_ip(worker_id, session, ctx)
    ip = _fallback_ip(ip)

    connector = aiohttp.TCPConnector(limit=2, force_close=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.post(
                f"{SSHGW_BASE}/api/ssh/ticket",
                json={"worker_id": worker_id, "ip": ip},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    raise InvalidException(
                        message=f"ssh-gateway 不可用 (HTTP {resp.status})"
                    )
                return await resp.json()
    except aiohttp.ClientError as e:
        raise InvalidException(message=f"ssh-gateway 不可达: {e}")
    finally:
        await connector.close()


@router.post("/credentials/{worker_id}")
async def ssh_gw_credentials(
    worker_id: int,
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """保存节点 SSH 凭据 (弹窗录入)。网关先实际登录验证, 通过才保存;
    失败按原因分类返回 (unreachable / auth_failed), 前端弹窗提示。"""
    worker, ip = await _worker_and_ip(worker_id, session, ctx)
    ip = _fallback_ip(ip)

    body = await request.json()
    if not body.get("username") or not body.get("password"):
        raise InvalidException(message="username and password are required")

    connector = aiohttp.TCPConnector(limit=2, force_close=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.post(
                f"{SSHGW_BASE}/api/ssh/credentials",
                json={
                    "worker_id": worker_id,
                    "ip": ip,
                    "username": body["username"],
                    "password": body["password"],
                    "port": int(body.get("port") or 22),
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    raise InvalidException(
                        message=f"ssh-gateway 不可用 (HTTP {resp.status})"
                    )
                return await resp.json()
    except aiohttp.ClientError as e:
        raise InvalidException(message=f"ssh-gateway 不可达: {e}")
    finally:
        await connector.close()


@router.websocket("/ws")
async def ssh_gw_ws(ws: WebSocket):
    """WebSocket 反代: 浏览器 <-> Go 网关 (二进制双向转发)。

    鉴权: ?ticket= 一次性票据由网关消费; 本路由只做透明转发
    (ticket 签发时已校验 worker 可见性)。
    """
    await ws.accept()
    ticket = ws.query_params.get("ticket", "")

    connector = aiohttp.TCPConnector(limit=2, force_close=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.ws_connect(
                f"{SSHGW_BASE}/ws/ssh?ticket={ticket}",
                timeout=aiohttp.ClientWSTimeout(ws_receive=3600, ws_close=10),
                autoping=True,
                max_msg_size=0,  # 终端输出不设限
            ) as gw:
                async def browser_to_gw():
                    try:
                        while True:
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                return
                            if msg.get("text") is not None:
                                await gw.send_str(msg["text"])
                            elif msg.get("bytes") is not None:
                                await gw.send_bytes(msg["bytes"])
                    except WebSocketDisconnect:
                        return

                async def gw_to_browser():
                    try:
                        async for msg in gw:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await ws.send_text(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await ws.send_bytes(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED,
                            ):
                                return
                    except Exception:
                        return

                # 双向并发, 任一方向结束即收工
                import asyncio

                done, pending = await asyncio.wait(
                    [asyncio.create_task(browser_to_gw()),
                     asyncio.create_task(gw_to_browser())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
    except aiohttp.ClientError as e:
        try:
            await ws.close(code=1011, reason=str(e)[:100])
        except Exception:
            pass
    finally:
        await connector.close()
        try:
            await ws.close()
        except Exception:
            pass
