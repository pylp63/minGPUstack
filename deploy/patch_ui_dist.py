"""编辑编译后的 UI dist (构建期执行) — 二开定制 v3:

A. 彻底移除计费 (billing) — 同前版
B. 组织页面 (id 42, chunk 9675) 替换为真实功能页: iframe 嵌 /console/
   (后端 /v2/organizations 真实 API 的组织管理 UI)
C. 模型服务菜单组 (id 9) 下新增两个菜单项:
   - 49: GPU 申请 (所有用户)  → /models/gpu-requests
   - 50: GPU 审批 (仅 admin)  → /models/gpu-approvals
   组件复用 chunk 9675 (同一组件按 hash 路由区分功能)
D. locale 增加 menu.gpuRequests / menu.gpuApprovals 文案

用法: UI_DIR=<gpustack 包内 ui 目录> python3 patch_ui_dist.py
"""
import glob
import gzip
import hashlib
import os
import re
import sys

BASE = os.environ.get("UI_DIR")
if not BASE or not os.path.isdir(BASE):
    print("!! UI_DIR not set or missing:", BASE)
    sys.exit(1)
JS = os.path.join(BASE, "js")


def read(path):
    with open(path, encoding="utf-8", errors="strict") as f:
        return f.read()


def write(path, t):
    with open(path, "w", encoding="utf-8") as f:
        f.write(t)


def regen_gz(path):
    gz = path + ".gz"
    if not os.path.exists(gz):
        return
    with open(path, "rb") as f_in:
        with open(gz + ".tmp", "wb") as f_out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=f_out, mtime=0) as g:
                g.write(f_in.read())
    os.replace(gz + ".tmp", gz)


# ================================================================ umi.js
umi_files = glob.glob(os.path.join(JS, "umi.*.js"))
if not umi_files:
    print("!! umi.js not found")
    sys.exit(1)
UMI = umi_files[0]
u = read(UMI)
orig = u

# --- A1: billing 菜单项整条删除
m = re.search(r'39:\{name:"billing",path:"/usage/billing"[^{}]*?id:"39"\},', u)
if m:
    u = u.replace(m.group(0), "")
    print("A1: billing menu removed")
else:
    print("A1: billing menu already removed")

# --- A2: 菜单组名 key 切到 usage
m2 = re.search(r'36:\{name:"billingAndUsage",path:"/usage",key:"usageGroup"', u)
if m2:
    u = u.replace(m2.group(0), m2.group(0).replace('name:"billingAndUsage"', 'name:"usage"'))
    print("A2: menu group label switched")
else:
    print("A2: menu group already switched")

# --- C1: 菜单表新增 49/50 项 (模型服务组 parentId:"9" 下)
# 插入锚点: 48:{...} 项末尾。先找 id:"48" 项。
anchor_m = re.search(r'(48:\{[^{}]*?id:"48"\},)', u)
if not anchor_m:
    # 兜底: 找最后一个菜单项 (id:"47" 或更小)
    ids = sorted({int(x) for x in re.findall(r'id:"(\d+)"', u) if int(x) < 100})
    if not ids:
        print("!! cannot locate menu table")
        sys.exit(1)
    last = ids[-1]
    anchor_m = re.search(r'(%d:\{[^{}]*?id:"%d"\},)' % (last, last), u)
    if not anchor_m:
        print("!! cannot anchor menu insert")
        sys.exit(1)

new_items = (
    '49:{name:"console",path:"/models/console",key:"console",'
    'icon:"icon-rocket-launch1",selectedIcon:"icon-rocket-launch-fill",'
    'defaultIcon:"icon-rocket-launch1",parentId:"9",id:"49"},'
    '50:{name:"deployWizard",path:"/models/deploy-wizard",key:"deployWizard",'
    'icon:"icon-rocket-launch1",selectedIcon:"icon-rocket-launch-fill",'
    'defaultIcon:"icon-rocket-launch1",parentId:"9",id:"50"},'
)
if 'id:"49"' not in u:
    u = u.replace(anchor_m.group(1), anchor_m.group(1) + new_items, 1)
    print("C1: menu item 49 added (模型服务组 → 控制台)")
else:
    print("C1: menu item already exists")

# --- C2: 组件绑定表新增 49/50 (复用 42 组织页的 chunk 加载链)
# 宽松正则: 官方 UI tarball 更新会改变 chunk id/模块 id, 精确字符串会漂移;
# 用正则从 42 的绑定链提取实际加载代码, 49/50 复用同一段。
if "49:k.lazy" in u:
    print("C2: binding 49 already exists")
else:
    i42 = u.find("42:k.lazy")
    if i42 == -1:
        print("!! cannot anchor component binding (42 pattern changed)")
        sys.exit(1)
    # 按括号配平提取 42 的完整 lazy 表达式
    depth = 0
    j = i42 + len("42:")
    start_expr = j
    while j < len(u):
        c = u[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                j += 1
                break
        j += 1
    lazy_expr = u[start_expr:j]  # 42 完整的 lazy 加载链
    extra = ",49:" + lazy_expr + ",50:" + lazy_expr
    u = u[:j] + extra + u[j:]
    print("C2: component binding 49/50 added (balanced anchor)")

if u != orig:
    write(UMI, u)
    regen_gz(UMI)
    print("umi.js updated + .gz regenerated")

# ================================================================ locale 文案 (菜单聚合: 控制台 + 组织改用户组)
LABELS = {
    "menu.models.console": "控制台",
    "menu.models.deployWizard": "部署向导",
    "menu.accessControl.organizations": "用户组",
}
patched = 0
for lf in sorted(glob.glob(os.path.join(JS, "*.chunk.js")) + [UMI]):
    try:
        t = read(lf)
    except Exception:
        continue
    if '"menu.accessControl.organizations":"' not in t:
        continue  # locale 文件特征 (zh/en/tr 语言包)
    t2 = t
    for k, v in LABELS.items():
        if '"%s":"' % k in t2:
            t2 = re.sub(r'"%s":"[^"]*"' % re.escape(k), '"%s":"%s"' % (k, v), t2)
        else:
            # 新增 key: 在『每一处』menu.accessControl.organizations 前都插入,
            # 保证 en + zh (乃至 tr/ru) 每套 locale 都拿到这个 key。
            # 之前用 replace(..., 1) 只插第一处, 中文界面查不到就回显原始 key。
            t2 = t2.replace(
                '"menu.accessControl.organizations":"',
                '"%s":"%s","menu.accessControl.organizations":"' % (k, v),
            )
    # 组名兜底
    t2 = t2.replace("使用量与计费", "使用量").replace("Usage & Billing", "Usage")
    if t2 != t:
        write(lf, t2)
        regen_gz(lf)
        patched += 1
        print(f"D: labels added to {os.path.basename(lf)}")
print(f"D: {patched} locale file(s) patched")

# ================================================================ usage 页 tabs
# 三个计量 tab 调用已删除的计量 API (/usage/resource|gpu-instances|storage/*).
# tab 项是固定字面量, 直接按精确片段删除 (逗号随项走, 保持数组语法有效).
_Tabs = [
    '{key:"gpu-instances",label:t.formatMessage({id:"usage.tabs.gpuInstances"}),access:"canSeeGpuService",children:(0,Y.jsx)(Mt,{})}',
    '{key:"storage",label:t.formatMessage({id:"usage.tabs.storage"}),access:"canSeeGpuService",children:(0,Y.jsx)(zt,{})}',
    '{key:"resource-events",label:t.formatMessage({id:"usage.tabs.resourceEvents"}),access:"canSeeGpuService",children:(0,Y.jsx)(F,{})}',
]
files = glob.glob(os.path.join(JS, "p__usage__index*.js"))
if files:
    f = files[0]
    t = read(f)
    n = 0
    for frag in _Tabs:
        if frag + "," in t:
            t = t.replace(frag + ",", "", 1)
            n += 1
        elif "," + frag in t:
            t = t.replace("," + frag, "", 1)
            n += 1
    if n:
        write(f, t)
        regen_gz(f)
        print(f"tabs: {n} metering tabs removed")
    elif not any('key:"%s"' % k in t for k in ("gpu-instances", "storage", "resource-events")):
        print("tabs: already removed")
    else:
        print("!! usage tab patch failed — patterns drifted")
        sys.exit(1)
else:
    print("!! usage page chunk not found")
    sys.exit(1)

# ================================================================ B: 组织页面 → 真实功能 (iframe 嵌 console)
org_files = glob.glob(os.path.join(JS, "p__organizations__index*.js"))
if not org_files:
    print("!! organizations page chunk not found")
    sys.exit(1)

# 组件: iframe 嵌 console 控制台/部署向导. 按 hash 路由决定目标:
#   /models/deploy-wizard (菜单"部署向导") → /console/deploy_wizard.html
#   /access-control/organizations (用户组)  → tab=groups
#   /models/console (菜单"控制台")          → 默认视图 (申请 + admin审批)
# (二开修复: 原注释里的 wizard.html 已删除 — 它调用 deploy-topologies
#  API 且无任何入口引用; 统一走 deploy_wizard.html + deploy-presets.)
STUB = (
    '"use strict";(self.webpackChunk=self.webpackChunk||[]).push([[9675],{'
    '87924:function(e,t,i){i.r(t);'
    'var R=i(75271);'
    't.default=function(){'
    'var src="/console/?embed=1&tab=";'
    'if(typeof window!=="undefined"){'
    'var h=window.location.hash||"";'
    'if(h.indexOf("/models/deploy-wizard")!==-1){src="/console/deploy_wizard.html?embed=1"}'
    'else if(h.indexOf("/access-control/organizations")!==-1){src="/console/?embed=1&tab=groups"}'
    '}'
    'return R.createElement("iframe",{'
    'src:src,'
    'style:{width:"100%",height:Math.max(360,(typeof window!=="undefined"?window.innerHeight:600)-96)+"px",border:0,display:"block"},'
    'frameBorder:"0"'
    '})'
    '}'
    '}}]);'
)
write(org_files[0], STUB)
if os.path.exists(org_files[0] + ".gz"):
    os.remove(org_files[0] + ".gz")

# 关键: org chunk 文件名是 <name>.<hash>.chunk.js 形式, 前面 rehash 步
# (只处理数字 id) 覆盖不到它。内容改成 stub 后必须改文件名 hash + 同步
# umi.js 里 9675 的 hash 映射, 否则浏览器缓存旧 URL (旧"了解企业版"页)。
_org_data = open(org_files[0], "rb").read()
_org_hash = hashlib.md5(_org_data).hexdigest()[:8]
_org_new = os.path.join(JS, "p__organizations__index.%s.chunk.js" % _org_hash)
os.rename(org_files[0], _org_new)
for _suf in (".gz",):
    if os.path.exists(org_files[0] + _suf):
        os.remove(org_files[0] + _suf)

_u = read(UMI)
_u2 = re.sub(r'9675:"[a-f0-9]{8}"', '9675:"%s"' % _org_hash, _u)
if _u2 != _u:
    write(UMI, _u2)
    regen_gz(UMI)
    print("B: org chunk rehashed -> p__organizations__index.%s.chunk.js (umi map updated)" % _org_hash)

# 同步 CSS chunk: webpack 加载 chunk 9675 时同时按同一 hash 拼 CSS 路径.
# 上面的 9675 hash 映射改动同时命中了 JS 和 CSS 两张映射表, 但 CSS 文件还
# 停在旧 hash 名, 必须同步复制, 否则报 "Loading CSS chunk 9675 failed".
_css_dir = os.path.join(BASE, "css")
_css_globs = glob.glob(os.path.join(_css_dir, "p__organizations__index.*.chunk.css"))
if _css_globs:
    _src_css = _css_globs[0]
    _dst_css = os.path.join(_css_dir, "p__organizations__index.%s.chunk.css" % _org_hash)
    if os.path.abspath(_src_css) != os.path.abspath(_dst_css):
        import shutil
        shutil.copyfile(_src_css, _dst_css)
        print("B: org CSS chunk copied -> p__organizations__index.%s.chunk.css" % _org_hash)

print("B: organizations page replaced with console iframe component")

# ================================================================ billing 页面 chunk 清空 + chunk map 移除
bill_files = glob.glob(os.path.join(JS, "p__billing__index*.js"))
for bf in bill_files:
    write(bf, '"use strict";/* 二开: 计费功能已移除 */')
    if os.path.exists(bf + ".gz"):
        os.remove(bf + ".gz")
    print("A3: billing chunk emptied")

u2 = read(UMI)
u3 = u2.replace('1276:"p__billing__index",', "").replace('1276:"p__billing__index"', "")
if u3 != u2:
    write(UMI, u3)
    regen_gz(UMI)
    print("A3: billing removed from chunk map")

print("UI dist patched OK")

# ================================================================
# 历史: 此处曾有内嵌 rehash_chunks(), 其正则双重转义匹配字面 '\d',
# 实为 no-op; 真正的 rehash 由构建链末尾的 rehash_umi_entry.py 完成
# (见 Dockerfile). 已删除死代码.
