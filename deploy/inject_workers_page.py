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
# label 必须是静态字符串 — antd Menu item 的 label 若传函数,
# 菜单直接把函数 toString 渲染出来 (显示成乱码文本, 实测)。
# key 用 __ssh_term__:<id> 编码行 id, 由外层 Dropdown onClick 拦截。
DROPDOWN_ANCHOR = '{label:"resources.worker.ssh.view",key:"view_ssh"'
idx = t.find(DROPDOWN_ANCHOR)
if idx == -1:
    print("!! dropdown anchor not found")
    sys.exit(1)

SSH_ITEM = (
    '{label:"SSH",key:"__ssh_term__",'
    'icon:(0,ae.jsx)(Y.Z,{type:"icon-ssh-outlined"})}'
    ','
)
# label 静态 "SSH" (label 函数会被 antd toString 渲染成乱码文本 — 实测)。
t = t[:idx] + SSH_ITEM + t[idx:]
print("SSH dropdown item injected")

# ============================================================
# 2. SSH 菜单点击 -> 新标签页终端。SplitButton (81034) 的菜单点击走
#    onSelect(key, item); workers 页 onSelect 闭包捕获 render 的 record n,
#    handleSelect (xe) 的 (e,n) — e=key, n=record (与 view_ssh 分支同构)。
#    在 xe 的 key 分支链里注入 __ssh_term__ 分支, n.id 即行 id。
XE_ANCHOR = '"stop_maintenance"===e&&N(n)'
xi = t.find(XE_ANCHOR)
if xi == -1:
    print("!! handleSelect anchor not found")
    sys.exit(1)
# 在 xe 函数体的分支链里加: __ssh_term__ -> window.open
XE_PATCH = (
    ',"__ssh_term__"===e&&window.open("/console/ssh_terminal.html?id="+(n?n.id:""),"_blank")'
)
t = t[:xi + len(XE_ANCHOR)] + XE_PATCH + t[xi + len(XE_ANCHOR):]
print("SSH open-terminal branch injected into handleSelect")

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

# ============================================================
# 4. CPU/GPU 服务器分类列: 插在 IP 列之前 (名称/标签/集群之后)。
#    列内容与名称列徽标同一视觉语言: GPU 服务器·N 卡 (蓝) / CPU 服务器 (灰)。
#    幂等: 先移除旧注入 (以 __type_col__ 标记识别), 再插入。
TYPE_COL_MARK = '__type_col__'
tc = t.find(TYPE_COL_MARK)
if tc != -1:
    # 移除上一次注入的整列 (从列数组元素开头到该元素结尾)
    # 注入形态: {title:"类型",...,dataIndex:"__type_col__",...}] 括号自平衡
    start = t.rfind('{title:"类型"', 0, tc)
    if start != -1:
        depth = 0
        i = start
        while i < len(t):
            if t[i] == '{':
                depth += 1
            elif t[i] == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        t = t[:start] + t[i + 1:]
        # 清理元素已删但其前导逗号可能残留 (,,) — 吃掉一个
        if start > 0 and t[start - 1] == ',' and start < len(t) and t[start] == ',':
            t = t[:start - 1] + t[start:]
        print("old type column removed (idempotent cleanup)")

IP_ANCHOR = '{title:"IP",dataIndex:"ip"'
if t.count(IP_ANCHOR) != 1:
    print("!! IP anchor not unique:", t.count(IP_ANCHOR))
    sys.exit(1)
ip = t.find(IP_ANCHOR)
TYPE_COL = (
    '{title:"类型",dataIndex:"__type_col__",minWidth:110,'
    'render:function(e,n){var g=((n&&n.status)||{}).gpu_devices||[];'
    'return (0,ae.jsx)("span",{style:{display:"inline-flex",alignItems:"center",'
    'padding:"1px 8px",borderRadius:4,fontSize:12,'
    'color:g.length?"#4f8cff":"#9aa0a6",'
    'background:g.length?"rgba(79,140,255,.12)":"rgba(154,160,166,.12)",'
    'whiteSpace:"nowrap"},'
    'children:g.length?("GPU 服务器 · "+g.length+" 卡"):"CPU 服务器"})}},'
)
t = t[:ip] + TYPE_COL + t[ip:]
print("CPU/GPU type column injected before IP column")

if t == orig:
    print("!! no-op")
    sys.exit(1)

# ---- 语法守卫 (同 inject_deploy_arch 模式) ----
try:
    import subprocess
    import tempfile
    # node --check 不支持 /dev/stdin (v24 报 ENOENT), 落临时文件检查
    with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as tf:
        tf.write(t.encode())
        tmp_name = tf.name
    try:
        subprocess.run(["node", "--check", tmp_name], timeout=30, check=True)
    finally:
        os.unlink(tmp_name)
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
