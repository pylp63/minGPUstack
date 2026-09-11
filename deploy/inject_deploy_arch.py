"""构建期 patch: 注入「服务拓扑」字段组到部署表单 (8671 chunk) 高级 tab.

目标: 高级 tab 之后新增「多机」分段, 支持一个模型放不下跨多台机器的场景:
  - standalone       单机部署 (默认, 现状)
  - pipeline_parallel 流水线并行 (几个节点一起跑放不下的模型)
  - pd_disaggregated  PD 分离 (prefill/decode 拆开; 组数 >1 即多 P 多 D,
                      原 multi_pd 选项已合并进来)

实现:
1) Ze 数组 (tab 定义) 末尾追加 multiNode 项.
2) deploy_architecture 下拉注入到 categories 之前, 并带 4 架构选项.
3) 提供数字字段 num_p / num_d / num_pipeline, onChange 把架构翻
   译成 backend_parameters (vLLM 语法) 写回表单.
4) locale 注入 deploy.arch.* / deploy.multiNode.tab 文案.

用法: UI_DIR=<pkg>/ui python3 inject_deploy-arch.py
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


# ---- 定位 8671 chunk ----
chunk = glob.glob(os.path.join(JS, "8671.*.chunk.js"))
if not chunk:
    cand = None
    for f in glob.glob(os.path.join(JS, "*.chunk.js")):
        try:
            tt = read(f)
        except Exception:
            continue
        if '"models.form.categories"' in tt and 'resources.form.advanced' in tt:
            cand = f
            break
    if not cand:
        print("!! 8671 deploy-form chunk not found")
        sys.exit(1)
    chunk = [cand]

f = chunk[0]
t = read(f)
orig = t

# ============================================================
# 0. 幂等清理: 删除历史版本注入的旧 deploy_architecture 字段
#    (旧单下拉: 含 "--pipeline-parallel-size=4" 写死 + alert 引导).
#    从旧字段的 Form.Item 起点删到下一个 k.Z.Item (或 categories) 之前,
#    防止新旧两份 deploy_architecture 并存导致下拉重复/行为错乱.
OLD_ARCH_MARK = 'params=["--pipeline-parallel-size=4"]'
idx_old = t.find(OLD_ARCH_MARK)
if idx_old != -1:
    # 回溯到旧字段 Form.Item 的起点 children:[ 或 (0,D.jsx)(k.Z.Item
    start = t.rfind('(0,D.jsx)(k.Z.Item,{name:"deploy_architecture"', 0, idx_old)
    if start == -1:
        start = t.rfind('(0,D.jsx)(k.Z.Item,{name:"serving_topology"', 0, idx_old)
    if start != -1:
        # 终点: 该字段结束后的下一个 (0,D.jsx)(k.Z.Item (即 categories)
        end = t.find('(0,D.jsx)(k.Z.Item', idx_old)
        if end != -1 and end > start:
            t = t[:start] + t[end:]
            print("0. removed legacy deploy_architecture field")

# ============================================================
# 1. (已移除) 不再单独加「多机」tab — 多机字段组直接放在「高级」tab 内
#    (categories 之前), 避免与高级 tab 重复/出现未翻译 key.

# ============================================================
# 2. deploy_architecture 字段组: 在 categories 字段前插入 (干净 chunk 无
#    deploy_architecture, 直接以 categories 为入口锚点插入多机字段组)
categ_anchor_key = '(0,D.jsx)(k.Z.Item,{name:"categories","data-field":"categories"'

ci = t.find(categ_anchor_key)
if ci == -1:
    print("!! categories anchor not found")
    sys.exit(1)

# 字段组 (用 + 拼接, 避免 f-string / 转义陷阱)
# 架构下拉: 不再本地把架构翻译成 backend_parameters (原做法只部署一个
# 带 prefill 参数的模型, decode 侧缺失, 不是真多机 — 二开修复).
# 改为记录所选架构, 提交时由表单 JS 调 /v2/deploy-presets/deploy
# 展开 (PD 分离 => prefill+decode 两个模型), backend_parameters 留空.
FIELD_GROUP = (
    '(0,D.jsx)(k.Z.Item,{name:"serving_topology","data-field":"serving_topology",'
    'style:{scrollMarginTop:200},'
    'extra:e.formatMessage({id:"models.form.servingTopology.tips"}),'
    'children:(0,D.jsx)(z.Z,{allowNull:!0,'
    'label:e.formatMessage({id:"models.form.servingTopology.mode"}),'
    'placeholder:e.formatMessage({id:"models.form.servingTopology.placeholder"}),'
    'options:['
    '{label:e.formatMessage({id:"models.form.servingTopology.standalone"}),value:"standalone"},'
    '{label:e.formatMessage({id:"models.form.servingTopology.pd"}),value:"pd_disaggregated"},'
    '{label:e.formatMessage({id:"models.form.servingTopology.pipeline"}),value:"pipeline_parallel"}],'
    'onChange:function(v){'
    'n.setFieldValue("backend_parameters",[]);'
    'n.setFieldValue("distributed_inference_across_workers",v==="pipeline_parallel");'
    '}})'
    '}),'
    '(0,D.jsx)(k.Z.Item,{noStyle:!0,'
    'shouldUpdate:function(a,b){return a.serving_topology!==b.serving_topology},'
    'children:function(fv){'
    'var arch=fv.serving_topology;'
    'var pd=(arch==="pd_disaggregated");'
    'var pp=(arch==="pipeline_parallel");'
    'if(!pd&&!pp){return null}'
    'var out=[];'
    'if(pd){out.push('
    '(0,D.jsx)(k.Z.Item,{name:"prefill_groups",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",defaultValue:1,min:1,max:64,'
    'label:e.formatMessage({id:"models.form.servingTopology.pgroups"})})}'
    '),'
    '(0,D.jsx)(k.Z.Item,{name:"decode_groups",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",defaultValue:1,min:1,max:64,'
    'label:e.formatMessage({id:"models.form.servingTopology.dgroups"})})}'
    '),'
    '(0,D.jsx)(k.Z.Item,{name:"prefill_gpu_count",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",defaultValue:1,min:1,max:64,'
    'label:e.formatMessage({id:"models.form.servingTopology.prefill"})})}'
    '),'
    '(0,D.jsx)(k.Z.Item,{name:"decode_gpu_count",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",defaultValue:1,min:1,max:64,'
    'label:e.formatMessage({id:"models.form.servingTopology.decode"})})}'
    '))}'
    'if(pp){out.push('
    '(0,D.jsx)(k.Z.Item,{name:"pipeline_parallel_size",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",defaultValue:2,min:2,max:16,'
    'label:e.formatMessage({id:"models.form.servingTopology.ppSize"})})}'
    '))}'
    'return (0,D.jsx)(D.Fragment,{children:out})'
    '}})'
    ','
)

# 在 categories 字段前插入多机字段组
t = t[:ci] + FIELD_GROUP + t[ci:]
if t == orig:
    print("!! field group no-op")
    sys.exit(1)
print("deploy_architecture field group ok")

# 配平守卫: 注入片段手写括号, 历史上出过 item 结尾多 ')' 的失衡 — 产物
# 语法坏了浏览器才在懒加载时报 "Loading chunk 8671 failed". 注入后立刻
# 用括号计数 + node 语法检查兜底 (node 不可用时退回纯计数).
_bal = t[ci:ci + len(FIELD_GROUP)]
_depth = 0
for _c in _bal:
    if _c in "([{":
        _depth += 1
    elif _c in ")]}":
        _depth -= 1
    if _depth < 0:
        print("!! injected field group unbalanced at char %d" % _bal.index(_c))
        sys.exit(1)
if _depth != 0:
    print("!! injected field group unbalanced (depth=%d)" % _depth)
    sys.exit(1)
try:
    import subprocess
    subprocess.run(["node", "--check", "/dev/stdin"],
                   input=t.encode(), timeout=30, check=True)
    print("inject syntax check (node): OK")
except FileNotFoundError:
    print("inject balance check: OK (node unavailable, count-only)")
except subprocess.CalledProcessError as e:
    print("!! injected chunk fails node --check")
    sys.exit(1)

write(f, t)
regen_gz(f)
print("wrote " + os.path.basename(f))

# ============================================================
# 3. locale 文案注入
# locale 注入范围: 不止三份语言 chunk — umi.js 本体内嵌了一份 zh 表
# (默认中文界面从它查询), 漏掉它 label 就回显 key 原文.
umis = [x for x in glob.glob(os.path.join(JS, "umi.*.js")) if not x.endswith(".gz")]
_seen = set()
for lf in [f] + glob.glob(os.path.join(JS, "*.chunk.js")) + umis:
    if lf in _seen:
        continue
    _seen.add(lf)
    try:
        lt = read(lf)
    except Exception:
        continue
    if '"models.form.categories"' not in lt:
        continue
    labels = [
        ("models.form.servingTopology.mode", "服务拓扑"),
        ("models.form.servingTopology.tips",
         "选择后按该拓扑部署，PD 分离将分别创建 Prefill 与 Decode 实例，节点数量 >1 即多 P 多 D 水平扩展"),
        ("models.form.servingTopology.placeholder", "默认单机部署"),
        ("models.form.servingTopology.standalone", "单机部署"),
        ("models.form.servingTopology.pd", "PD 分离"),
        ("models.form.servingTopology.pipeline", "流水线并行"),
        ("models.form.servingTopology.prefill", "Prefill GPU 数"),
        ("models.form.servingTopology.decode", "Decode GPU 数"),
        ("models.form.servingTopology.ppSize", "流水线并行度"),
        ("models.form.servingTopology.pgroups", "Prefill节点数量"),
        ("models.form.servingTopology.dgroups", "Decode数量"),
    ]
    changed = False
    for k, v in labels:
        pat = '"%s":"' % k
        if pat in lt:
            lt = re.sub(pat + '[^"]*"', '"%s":"%s"' % (k, v), lt)
            changed = True
        elif '"models.form.categories":"' in lt:
            # 每一处 categories (en/zh/... 每语言一张表) 都要插,
            # 只插第一处会让其余语言回显 key 原文.
            lt = lt.replace('"models.form.categories":"',
                            '"%s":"%s","models.form.categories":"' % (k, v))
            changed = True
    if changed:
        write(lf, lt)
        regen_gz(lf)
        print("locale " + os.path.basename(lf) + " updated")

print("inject_deploy_arch ALL DONE")