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

from gpustack.api.exceptions import (
    ForbiddenException,
    InvalidException,
    NotFoundException,
)
from gpustack.api.tenant import assert_resource_visible
from gpustack.server.deps import SessionDep, TenantContextDep
from gpustack.schemas.workers import Worker

logger = logging.getLogger(__name__)

router = APIRouter()

SSHGW_BASE = "http://127.0.0.1:10170"

# 直通 (无 body 变换) 的网关端点: method -> gateway path。
# 鉴权: 本函数统一做 worker 可见性校验 (针对带 {worker_id} 的调用)。
_DIRECT_ROUTES = {
    ("GET", "list"): ("GET", "/api/ssh/cred/list"),
    ("POST", "rotate-config"): ("POST", "/api/ssh/rotate-config"),
    ("POST", "rotate-now"): ("POST", "/api/ssh/rotate-now"),
    ("POST", "upload"): ("POST", "/api/ssh/upload"),
    ("GET", "download"): ("GET", "/api/ssh/download"),
    ("DELETE", "delete"): ("DELETE", "/api/ssh/cred/delete"),
}


async def _proxy_direct(
    gw_method: str,
    gw_path: str,
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
    require_worker: bool = True,
):
    """透明转发到 Go 网关 (登录态 + 可选 worker 可见性校验)。"""
    if require_worker:
        wid = request.query_params.get("worker_id")
        if not wid and request.method == "POST" and "application/json" in (
            request.headers.get("content-type") or ""
        ):
            try:
                wid = (await request.json()).get("worker_id")
            except Exception:
                wid = None
        if wid:
            try:
                await _worker_and_ip(int(wid), session, ctx)
            except (ValueError, NotFoundException):
                raise NotFoundException(message="worker not found")

    url = f"{SSHGW_BASE}{gw_path}"
    headers = {}
    body = None
    if request.method == "POST" and "multipart/form-data" not in (
        request.headers.get("content-type") or ""
    ):
        body = await request.body()
        headers["Content-Type"] = request.headers.get("content-type", "")
    elif request.method == "POST":
        # multipart (上传): aiohttp 整包透传
        body = await request.body()
        headers["Content-Type"] = request.headers.get("content-type", "")

    connector = aiohttp.TCPConnector(limit=4, force_close=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.request(
                gw_method,
                url + ("?" + request.url.query if request.url.query else ""),
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=None if body else 60,
                                              sock_connect=5),
            ) as resp:
                payload = await resp.read()
                ct = resp.headers.get("content-type", "application/json")
                from fastapi.responses import Response

                return Response(
                    content=payload,
                    status_code=resp.status,
                    media_type=ct.split(";")[0],
                    headers={
                        k: v
                        for k, v in resp.headers.items()
                        if k.lower()
                        in ("content-disposition", "content-length")
                    },
                )
    except aiohttp.ClientError as e:
        raise InvalidException(message=f"ssh-gateway 不可达: {e}")
    finally:
        await connector.close()


@router.get("/credentials")
async def ssh_gw_cred_list(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """节点凭证列表 (密码不回传)。"""
    return await _proxy_direct("GET", "/api/ssh/cred/list", request, session, ctx,
                               require_worker=False)


@router.delete("/credentials/{worker_id}")
async def ssh_gw_cred_delete(
    worker_id: int,
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """删除节点凭据。"""
    await _worker_and_ip(worker_id, session, ctx)
    request.scope["query_string"] = f"id={worker_id}".encode()
    return await _proxy_direct("DELETE", "/api/ssh/cred/delete", request, session,
                               ctx, require_worker=False)


@router.get("/rotate-config")
@router.post("/rotate-config")
async def ssh_gw_rotate_config(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """随机密码轮换设置 (页面顶部: 开关 + 天数)。仅平台管理员。"""
    if not ctx.is_platform_admin:
        raise ForbiddenException(message="Platform admin permission required")
    return await _proxy_direct(
        request.method, "/api/ssh/rotate-config", request, session, ctx,
        require_worker=False,
    )


@router.post("/rotate-now")
async def ssh_gw_rotate_now(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """立即轮换 (?id=N 指定节点, 缺省全部)。仅平台管理员。"""
    if not ctx.is_platform_admin:
        raise ForbiddenException(message="Platform admin permission required")
    return await _proxy_direct("POST", "/api/ssh/rotate-now", request, session, ctx,
                               require_worker=False)


@router.post("/upload")
async def ssh_gw_upload(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """SFTP 上传 (multipart: file + worker_id + remote_path)。"""
    return await _proxy_direct("POST", "/api/ssh/upload", request, session, ctx)


@router.get("/ls")
async def ssh_gw_ls(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """SFTP 目录浏览 (?worker_id=&path=, 含隐藏文件)。"""
    return await _proxy_direct("GET", "/api/ssh/ls", request, session, ctx)


@router.get("/cwd")
async def ssh_gw_cwd(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """终端交互 shell 的当前目录 (?worker_id=; 文件面板跟随 cd 用)。"""
    return await _proxy_direct("GET", "/api/ssh/cwd", request, session, ctx)


@router.post("/complete")
async def ssh_gw_complete(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """Tab 补全候选 (body: worker_id/line/cursor; 失败静默返回空列表)。"""
    return await _proxy_direct("POST", "/api/ssh/complete", request, session, ctx)


@router.api_route("/global-cred", methods=["GET", "POST", "DELETE"])
async def ssh_gw_global_cred(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """通用凭据 (一批服务器的统一账号): GET 查看 / POST 验证保存 / DELETE 删除。
    仅平台管理员。"""
    if not ctx.is_platform_admin:
        raise ForbiddenException(message="Platform admin permission required")
    return await _proxy_direct(request.method, "/api/ssh/global-cred", request,
                               session, ctx, require_worker=False)


@router.get("/download")
async def ssh_gw_download(
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """SFTP 下载 (?worker_id=&path=), 流式回传。"""
    return await _proxy_direct("GET", "/api/ssh/download", request, session, ctx)


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
    request: Request,
    session: SessionDep,
    ctx: TenantContextDep,
):
    """签发 SSH 终端一次性 ticket。权限与 ssh-exec 同级。"""
    worker, ip = await _worker_and_ip(worker_id, session, ctx)
    ip = _fallback_ip(ip)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    connector = aiohttp.TCPConnector(limit=2, force_close=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.post(
                f"{SSHGW_BASE}/api/ssh/ticket",
                json={
                    "worker_id": worker_id,
                    "ip": ip,
                    # 多凭据: 前端选择窗口指定的用户名 (空 = 默认第一套)
                    "username": body.get("username") or "",
                },
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
