# SSH 终端 (节点页「SSH」按钮)

二开功能: 节点列表 → 操作 → **SSH**, 新标签页打开完整交互式终端
(xterm.js + Go 网关 PTY, 支持 vim/top 等交互程序)。

## 架构

```
浏览器 (xterm.js)
   │  POST /v2/ssh-gw/ticket/{worker_id}      ← GPUStack 登录态 + worker 可见性校验
   │  WS   /v2/ssh-gw/ws?ticket=...           ← FastAPI 反代
   ▼
Go ssh-gateway (127.0.0.1:10170, 容器内)
   │  ticket 一次性消费 (10s 有效, 防重放)
   │  SSH 密码认证 (crypto/ssh)
   ▼
节点 PTY (root@worker)
```

- 前端: `gpustack/gpustack/ui_console/ssh_terminal.html` (+ xterm 静态资源)
- 后端反代: `gpustack/gpustack/routes/ssh_gateway.py` (`/v2/ssh-gw/*`)
- 网关: `ssh-gateway/` (Go, 静态编译单二进制 ~11MB, Dockerfile 构建阶段产出)

## 配置 (环境变量, compose 传入)

| 变量 | 默认 | 说明 |
|---|---|---|
| `SSHGW_ROTATE_PASSWORDS` | (空=关) | `true` 开启定期随机密码轮换 |
| `SSHGW_ROTATE_EVERY` | `24h` | 轮换周期 (Go duration 格式) |
| `SSHGW_BOOTSTRAP_PASSWORD` | (必填*) | 首次接入节点的引导密码 (轮换后即作废) |
| `SSHGW_SSH_USER` | `root` | 节点 SSH 用户 |
| `SSHGW_SSH_PORT` | `22` | 节点 SSH 端口 |
| `SSHGW_LISTEN` | `127.0.0.1:10170` | 网关监听地址 (只监听本机) |
| `SSHGW_STATE_FILE` | `/var/lib/gpustack/ssh-gateway-state.json` | 密码状态文件 (0600) |

## 密码轮换

开启 `SSHGW_ROTATE_PASSWORDS=true` 后:

1. **首次接入**: 用 `SSHGW_BOOTSTRAP_PASSWORD` 登录节点 → `chpasswd` 改为
   32 字符 crypto/rand 随机密码 (大小写+数字+符号, 剔除易混淆字符) →
   引导密码即作废
2. **定期轮换**: 每 `SSHGW_ROTATE_EVERY` 用当前密码登录改为新随机密码
3. **持久化**: 当前密码仅存内存 + 0600 状态文件 (容器卷内, 重启恢复);
   密码历史不落盘
4. 轮换日志: `/var/lib/gpustack/ssh-gateway.log`

密码经 stdin 传给 chpasswd, 不出现在命令行 (ps 不可见)。

## 安全设计

- ticket 一次性 + 10 秒 TTL (重放无效)
- 网关只监听 127.0.0.1, 必须经 GPUStack 主服务 (登录态) 才能触达
- SSH 会话用节点随机密码, 无静态凭据
- WS 反代路由不带 GPUStack 会话 (WS 协议限制), 鉴权完全依赖一次性 ticket
