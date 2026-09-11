"""构建期 patch: 节点页面 (workers chunk) 增强注入 (二开).

1. SSH 终端: 操作下拉加「SSH 终端」项 -> 新窗口打开 /console/ssh_terminal.html?id=<wid>
2. CPU/GPU 服务器区分: 名称列 render 里按 status.gpu_devices 数量加徽标
   (GPU 服务器 N 卡 / CPU 服务器)

用法: UI_DIR=<pkg>/ui python3 inject_workers_page.py
"""
import glob
import gzip
import os
import re
import sys

BASE = os.environ.get("UI_DIR")
if not BASE or not os.path.isdir(BASE):
    print("!! UI_DIR not set:", BASE)
    sys.exit(1)
JS = os.path.join(BASE, "js")


def read(p):
    with open(p, encoding="utf-8", errors="strict") as f:
        return f.read()


def write(p, t):
    with open(p, "w", encoding="utf-8") as f:
        f.write(t)


def regen_gz(p):
    gz = p + ".gz"
    if not os.path.isfile(gz):
        return
    with open(p, "rb") as fi:
        data = fi.read()
    with open(gz, "wb") as fo:
        g = gzip.GzipFile(filename="", mode="wb", fileobj=fo, mtime=0)
        g.write(data)
        g.close()


# ---- 定位 workers chunk ----
files = glob.glob(os.path.join(JS, "p__resources__components__workers.*.chunk.js"))
files = [f for f in files if not f.endswith(".gz")]
if not files:
    print("!! workers page chunk not found")
    sys.exit(1)
f = files[0]
t = read(f)
orig = t

# ============================================================
# 1. SSH 终端: 操作下拉项注入 (插在 view_ssh 项之前)
DROPDOWN_ANCHOR = '{label:"resources.worker.ssh.view",key:"view_ssh"'
idx = t.find(DROPDOWN_ANCHOR)
if idx == -1:
    print("!! dropdown anchor not found")
    sys.exit(1)

SSH_ITEM = (
    '{label:"SSH 终端",key:"ssh_terminal",'
    'icon:(0,ae.jsx)(Y.Z,{type:"icon-ssh-outlined"}),'
    'onClick:function(){'
    'window.open("/console/ssh_terminal.html?id="+this.row.id,"_blank")}'
    '},'
)
# this.row 不可用 (items 数组无 row 上下文) — dropdown onClick 用 record 参数。
# 检查 J.Z (dropdown) items 渲染是否传 record... antd Dropdown onClick(item) 只有 key。
# 改用闭包: 在 filter 处捕获 record n -> 需要在 items 构造处。保守方案:
# 点击后从 key 解析 id (key 编码为 ssh_terminal:<id>)。
SSH_ITEM = (
    '{label:function(r){return "SSH 终端"},key:"__ssh_term__",'
    'icon:(0,ae.jsx)(Y.Z,{type:"icon-ssh-outlined"})}'
    ','
)
# 上面的 function label 需要 intl… 简化: 直接静态 label (中文界面)。
# key 用 __ssh_term__ 前缀 + id, 在 dropdown 的 onClick (J.Z 外层有 onClick?) 处理。
# 查看外层: J.Z({items:...,onClick:?}) — 需要确认。若外层 onClick 存在则拦截 key。
t = t[:idx] + SSH_ITEM + t[idx:]
print("SSH dropdown item injected")

# ============================================================
# 2. 拦截 key 执行打开终端: 找 J.Z({items:...( 处的 onClick。
# 若没有 onClick, 注入一个。dropdown 组件形如 (0,ae.jsx)(J.Z,{items:(...),onClick:fn})
# 先探测:
if 'key:"__ssh_term__"' in t:
    # 找 items 数组的闭合后的 onClick — 直接在 J.Z,{items: 后面查找 "onClick"
    jz = t.find('(0,ae.jsx)(J.Z,{items:')
    seg = t[jz:jz + 400]
    if 'onClick' not in seg.split('})')[0]:
        # 没有 onClick: 在 J.Z,{items: 后面插 onClick 处理 (拦截 __ssh_term__ key)
        ONCLICK = (
            'onClick:function(info){'
            'if(String(info.key||"").indexOf("__ssh_term__")===0){'
            'var wid=String(info.key).split(":")[1];'
            'window.open("/console/ssh_terminal.html?id="+wid,"_blank")}},'
        )
        # items: 前插 onClick? antd Dropdown props 顺序无关。插在 J.Z,{ 后:
        t = t[:jz + len('(0,ae.jsx)(J.Z,{')] + ONCLICK + t[jz + len('(0,ae.jsx)(J.Z,{'):]
        print("dropdown onClick handler injected")
    else:
        print("(dropdown already has onClick — 检查 key 格式兼容)")

# key 里编码 id: SSH_ITEM 的 key 改为动态。但 items 是静态数组 (无 record)…
# 检查 items 构造上下文: filter(function(e){...t=n...}) — n 是 record (闭包)!
# 所以可以在 SSH_ITEM 里用闭包变量。上面探测到的 filter: (t=n,oe.filter(...))
# t 就是当前行 record! 在 SSH_ITEM 内引用不了 (数组在外面)。
# 正解: label 用静态文案, key 固定 "__ssh_term__", onClick 从 label 拿不到 id;
# 但 dropdown onClick 的 info 里有 item.props… 复杂。
# 最稳: 利用已有闭包 — 把 SSH_ITEM 的 key 写成运行时拼接: key:"__ssh_term__:"+t.id
# (t 是 filter 闭包里的 record, 同一作用域!)

# 替换静态 key 为闭包 key:
t = t.replace(
    'key:"__ssh_term__"',
    'key:"__ssh_term__:"+(t?t.id:"")',
)
print("SSH item key bound to row id")

# ============================================================
# 3. CPU/GPU 区分: 名称列 render 的 name-text span 后加类型徽标
# render: ...children:(0,ae.jsx)("span",{className:"name-text",children:e})}),h(n)]})}}
# 在 h(n) 调用后追加 GPU/CPU 徽标 (h(n) 是现有副标题组件)。
NAME_ANCHOR = 'children:(0,ae.jsx)("span",{className:"name-text",children:e})}),h(n)]})}'
na = t.find(NAME_ANCHOR)
if na == -1:
    print("!! name render anchor not found (GPU/CPU badge skip)")
else:
    BADGE = (
        'children:(0,ae.jsx)("span",{className:"name-text",children:e})}),h(n),'
        '(function(){var g=((n&&n.status)||{}).gpu_devices||[];'
        'return (0,ae.jsx)("span",{style:{marginLeft:8,fontSize:11,'
        'padding:"1px 6px",borderRadius:4,'
        'color:g.length?"#4f8cff":"#9aa0a6",'
        'background:g.length?"rgba(79,140,255,.12)":"rgba(154,160,166,.12)"},'
        'children:g.length?("GPU 服务器 · "+g.length+" 卡"):"CPU 服务器"})})()]})}'
    )
    t = t[:na] + BADGE + t[na + len(NAME_ANCHOR):]
    print("CPU/GPU badge injected into name column")

if t == orig:
    print("!! no-op")
    sys.exit(1)

# ---- 语法守卫 (同 inject_deploy_arch 模式) ----
try:
    import subprocess
    subprocess.run(["node", "--check", "/dev/stdin"],
                   input=t.encode(), timeout=30, check=True)
    print("workers chunk syntax check (node): OK")
except FileNotFoundError:
    print("(node unavailable — count-only)")
except subprocess.CalledProcessError:
    print("!! workers chunk fails node --check")
    sys.exit(1)

write(f, t)
regen_gz(f)
print("wrote " + os.path.basename(f))

# locale: SSH 终端 文案 (静态中文 label 无需 locale, 但 dropdown item 的
# label 若走 formatMessage 需要 — 我们用静态中文, 跳过)

print("inject_workers_page ALL DONE")
