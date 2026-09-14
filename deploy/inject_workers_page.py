"""构建期 patch: 节点页面 (workers chunk) 增强注入 (二开).

1. SSH 终端: 操作下拉加「SSH 终端」项 -> 新窗口打开 /console/ssh_terminal.html?id=<wid>
2. 部署形态徽标: 名称列 + 独立「部署形态」列, 按 worker 注册时打的
   gpustack.io/node-kind 标签显示 K8s Pod / 容器 / BMS 裸金属 (三色区分),
   并保留 GPU/CPU 服务器信息 (GPU 服务器 N 卡 / CPU 服务器)

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
    print("!! name render anchor not found (node-kind badge skip)")
else:
    # 名称列徽标 (v2): 部署形态 (k8s-pod/container/bms) + GPU/CPU 卡数。
    # 与「部署形态」列同一标签来源; 老节点无标签时不显示形态徽标
    # (只显示 GPU/CPU), 避免误标。
    BADGE = (
        'children:(0,ae.jsx)("span",{className:"name-text",children:e})}),h(n),'
        '(function(){var g=((n&&n.status)||{}).gpu_devices||[];'
        'var kind=((n||{}).labels||{})["gpustack.io/node-kind"];'
        'var kc={'
        '"k8s-pod":["K8s Pod","#722ed1","rgba(114,46,209,.10)"],'
        '"container":["容器","#08979c","rgba(8,151,156,.10)"],'
        '"bms":["BMS","#d46b08","rgba(212,107,8,.10)"]}[kind];'
        'return (0,ae.jsx)("span",{style:{marginLeft:8,fontSize:11,'
        'padding:"1px 6px",borderRadius:4,whiteSpace:"nowrap",'
        'color:kc?kc[1]:(g.length?"#4f8cff":"#9aa0a6"),'
        'background:kc?kc[2]:(g.length?"rgba(79,140,255,.12)":"rgba(154,160,166,.12)")},'
        'children:kc?kc[0]:(g.length?("GPU 服务器 · "+g.length+" 卡"):"CPU 服务器")})})()]})}'
    )
    t = t[:na] + BADGE + t[na + len(NAME_ANCHOR):]
    print("node-kind badge injected into name column")

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
# 二开: 部署形态列 — agent 承载方式 (worker 注册时自动打
# gpustack.io/node-kind 标签: k8s-pod / container / bms):
#   k8s-pod  K8S Pod        (紫)  — agent 跑在 K8S 集群 Pod 里
#   container 宿主容器      (青)  — docker/compose 起的容器
#   bms      BMS 裸金属     (橙)  — agent 直接跑物理机/虚机 OS
# 老节点 (无标签) fallback 到 container 判定 (GPUDocker 部署的存量节点),
# 再 fallback 未知 — 不误标 BMS。
# 徽标第二行保留 GPU/CPU 信息 (GPU 服务器·N 卡 / CPU 服务器)。
TYPE_COL = (
    '{title:"部署形态",dataIndex:"__type_col__",minWidth:150,'
    'render:function(e,n){'
    'var g=((n&&n.status)||{}).gpu_devices||[];'
    'var lb=(n&&n.labels)||{};'
    'var kind=lb["gpustack.io/node-kind"];'
    'if(!kind){'
    # 容器证据 (与 worker 侧判定同源, 老节点兜底): .dockerenv 在 UI 里
    # 不可探 — 用 filesystem.mount_from=overlay 推断 (GPUDocker 部署特征)
    'var fs=((n&&n.status)||{}).filesystem||[];'
    'var mnt=(fs[0]||{}).mount_from||"";'
    'kind=mnt==="overlay"?"container":"unknown";}'
    'var cfg={'
    '"k8s-pod":{t:"K8s Pod",c:"#722ed1",bg:"rgba(114,46,209,.10)"},'
    '"container":{t:"容器",c:"#08979c",bg:"rgba(8,151,156,.10)"},'
    '"bms":{t:"BMS 裸金属",c:"#d46b08",bg:"rgba(212,107,8,.10)"}}[kind]'
    '||{t:"未知",c:"#9aa0a6",bg:"rgba(154,160,166,.12)"};'
    'return (0,ae.jsx)("div",{style:{display:"flex",flexDirection:"column",gap:2},children:['
    '(0,ae.jsx)("span",{style:{display:"inline-flex",alignItems:"center",'
    'padding:"1px 8px",borderRadius:4,fontSize:12,whiteSpace:"nowrap",'
    'width:"fit-content",'
    'color:cfg.c,background:cfg.bg,'
    'children:cfg.t}}),'
    '(0,ae.jsx)("span",{style:{fontSize:11,color:g.length?"#4f8cff":"#9aa0a6",'
    'whiteSpace:"nowrap"},'
    'children:g.length?("GPU 服务器 · "+g.length+" 卡"):"CPU 服务器"})]})}},'
)
t = t[:ip] + TYPE_COL + t[ip:]
print("node-kind column injected before IP column")

# ============================================================
# 5. IP 手动覆盖: 编辑弹窗加「IP 地址」输入框。
#    背景: worker 自动探测 IP 在多网卡/代理网卡环境会选错 (如 fake-IP
#    DNS 代理的 Meta 网卡 198.18.x.x)。管理员手动指定后写入
#    labels["gpustack.io/ip-override"], 后端 update_worker 拦截:
#    覆盖 ip/advertise_address 且心跳/重注册不再冲掉 (三处保护)。
#    四个锚点 (构建期 fail-fast):
#      a. 编辑弹窗 initialValues 加 ip (回显现有 IP)
#      b. labels 字段后插 IP 输入框
#      c. ve (onOk) 提交时把 ip 值塞进 labels 覆写键
#      d. Ae 的 data prop 加 ip
# ============================================================
INIT_VALUES_OLD = 'initialValues:{name:a.name,labels:a.labels},children:['
INIT_VALUES_NEW = (
    'initialValues:{name:a.name,labels:a.labels,'
    # 回显: 优先覆写标签值 (手动指定过的), 否则当前探测 ip
    'ip:((a.labels||{})["gpustack.io/ip-override"])||a.ip},children:['
)
LABELS_ITEM_END_OLD = (
    'children:(0,ae.jsx)(be.Z,{label:i.formatMessage({id:"resources.table.labels"}),'
    'btnText:i.formatMessage({id:"common.button.addLabel"})})})]})})}),'
)
IP_FIELD = (
    # 原 labels item 的 children 段原样保留 (被替换原文的开头),
    # 其后插入 IP 输入框 item; allowClear 清空 = 解除覆盖 (心跳探测值恢复写回)
    'children:(0,ae.jsx)(be.Z,{label:i.formatMessage({id:"resources.table.labels"}),'
    'btnText:i.formatMessage({id:"common.button.addLabel"})})}),'
    '(0,ae.jsx)(ye.Z.Item,{name:"ip",children:(0,ae.jsx)(xe.Z.Input,{'
    'label:"IP 地址",placeholder:"自动探测 (留空恢复)",allowClear:!0,'
    'description:"手动指定后以该 IP 为准, 心跳不再用探测值覆盖; 清空恢复自动探测"})})'
    ']})})}),'
)
VE_SUBMIT_OLD = (
    'e.next=3,(0,S.Vq)(W.data.id,i()(i()({},W.data),{},{labels:n.labels}));'
)
VE_SUBMIT_NEW = (
    # 表单 ip 值 -> labels 覆写键 (后端 update_worker 拦截生效);
    # 清空时移除键 (解除覆盖)。其余字段原样透传。
    'e.next=3,(function(){var lv=i()(i()({},n.labels||{}),{},'
    '{name:W.data.name});'
    'var ipov=(n.ip||"").trim();'
    'if(ipov){lv["gpustack.io/ip-override"]=ipov}'
    'else{delete lv["gpustack.io/ip-override"]}'
    'return (0,S.Vq)(W.data.id,i()(i()({},W.data),{},{labels:lv}))})();'
)
AE_DATA_OLD = 'data:{name:W.data.name,labels:W.data.labels}})'
AE_DATA_NEW = (
    'data:{name:W.data.name,labels:W.data.labels,'
    'ip:((W.data.labels||{})["gpustack.io/ip-override"])||W.data.ip}})'
)
_ok = True
for _n, _old, _new in (
    ("edit initialValues", INIT_VALUES_OLD, INIT_VALUES_NEW),
    ("edit ip field", LABELS_ITEM_END_OLD, IP_FIELD),
    ("ve submit", VE_SUBMIT_OLD, VE_SUBMIT_NEW),
    ("Ae data prop", AE_DATA_OLD, AE_DATA_NEW),
):
    if _old not in t:
        print("!! anchor missing: " + _n)
        _ok = False
    else:
        t = t.replace(_old, _new, 1)
        print("edit-modal " + _n + " patched")
if not _ok:
    sys.exit(1)

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
