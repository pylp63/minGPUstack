"""构建期 patch: 注入「服务拓扑」字段组到部署表单 (8671 chunk).

目标: 高级 tab 之后新增「多机」分段, 支持一个模型放不下跨多台机器的场景:
  - standalone       单机部署 (默认, 现状)
  - pipeline_parallel 流水线并行 (几个节点一起跑放不下的模型)
  - pd_disaggregated  PD 分离 (prefill/decode 拆开; 组数 >1 即多 P 多 D,
                      原 multi_pd 选项已合并进来)

实现 (v3 — 节点分配与调度联动):
1) 服务拓扑下拉 (serving_topology) + PD/PP 参数字段注入到「高级」tab
   (categories 之前), 仅 backend===SGLang 时渲染。
2) PD rank 节点分配 (pd_node_assign) 框注入到「调度」tab 的 GPU 分配方框
   (sectionCard) 之后 — 前提条件三个:
      a. backend === SGLang (vLLM/SGLang PD 参数逻辑不同)
      b. serving_topology === pd_disaggregated
      c. 调度方式 scheduleType === manual (自动调度时节点由调度器摆放,
         不出现 rank 选点框)
3) rank 选点互斥: 任一 rank (Prefill rank0..N / Decode rank0..M) 已选的
   节点在其它 rank 的下拉里置灰不可选 (前端 disabled), 保证 P/D 各 rank
   节点不重叠; 后端 build_preset_payloads 同样校验 (双保险)。

用法: UI_DIR=<pkg>/ui python3 inject_deploy_arch.py
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
chunk = [c for c in chunk if not c.endswith(".gz")]
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
# 0. 幂等清理: 删除历史版本注入的旧字段组 / 旧 rank 分配框
#    v1/v2: rank 节点框曾跟着服务拓扑组一起注入高级 tab; v3 起拆到调度 tab
#    且只在手动调度下显示。旧注入的标志是 backend-shouldUpdate 包裹 +
#    末尾紧跟 worker-fetch IIFE。
OLD_START = '(0,D.jsx)(k.Z.Item,{noStyle:!0,shouldUpdate:function(a,b){return a.backend!==b.backend}'
OLD_FETCH = '(function(){if(window.__topoWorkersFetched)'
i_old = t.find(OLD_START)
if i_old != -1:
    i_fetch = t.find(OLD_FETCH, i_old)
    if i_fetch != -1:
        # 旧注入以 field-group + (IIFE,  ... 形式存在: 删到 IIFE 结束的 "),"
        i_end = t.find('})()', i_fetch)
        if i_end != -1:
            t = t[:i_old] + t[i_end + len('})(),'):]
            print("0. removed legacy field group + worker fetch (v<=2)")
    else:
        # 只有 field group 无 IIFE (更早版本)
        i_cat = t.find('(0,D.jsx)(k.Z.Item,{name:"categories"', i_old)
        if i_cat != -1:
            t = t[:i_old] + t[i_cat:]
            print("0. removed legacy field group (no fetch)")
# 旧版 IIFE 单独残留 (无 field group 配对) 也清掉
if OLD_FETCH in t:
    i_fetch = t.find(OLD_FETCH)
    i_end = t.find('})()', i_fetch)
    if i_end != -1:
        t = t[:i_fetch] + t[i_end + len('})(),'):]
        print("0. removed stray worker fetch")

# ============================================================
# 1. 高级 tab: 服务拓扑下拉 + PD/PP 参数字段 (无节点分配框 — 节点框在调度 tab)
#    结构: Form.Item(shouldUpdate: backend 变化) -> Fragment:
#      - serving_topology 下拉 (onChange 播种 PD/PP 默认值)
#      - Form.Item(shouldUpdate: topology/groups 变化) -> Fragment:
#          - PD: pd_node_assign(hidden 注册) + P/D 组数 + P/D GPU 数 + PP
#          - PP: pipeline_parallel_size
#    注: pd_node_assign hidden 注册必须留在高级 tab 的拓扑组里
#    (调度 tab 的框只在手动+PD 时渲染, 自动调度下字段仍需注册才能提交)。
ADV_GROUP = (
    # 外层: 仅 backend===SGLang 时渲染整个服务拓扑字段组
    '(0,D.jsx)(k.Z.Item,{noStyle:!0,'
    'shouldUpdate:function(a,b){return a.backend!==b.backend},'
    'children:function(fb){'
    'if(fb.getFieldValue("backend")!=="SGLang"){return null}'
    'return (0,D.jsx)(D.Fragment,{children:['
    '(0,D.jsx)(k.Z.Item,{name:"serving_topology","data-field":"serving_topology",'
    'style:{scrollMarginTop:200},'
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
    'if(v==="pd_disaggregated"){'
    'if(n.getFieldValue("prefill_groups")===undefined){n.setFieldValue("prefill_groups",1)}'
    'if(n.getFieldValue("decode_groups")===undefined){n.setFieldValue("decode_groups",1)}'
    'if(n.getFieldValue("prefill_gpu_count")===undefined){n.setFieldValue("prefill_gpu_count",1)}'
    'if(n.getFieldValue("decode_gpu_count")===undefined){n.setFieldValue("decode_gpu_count",1)}'
    'if(n.getFieldValue("pd_pipeline_size")===undefined){n.setFieldValue("pd_pipeline_size",1)}'
    'if(n.getFieldValue("pd_node_assign")===undefined){n.setFieldValue("pd_node_assign",{prefill:[],decode:[]})}}'
    'if(v==="pipeline_parallel"){'
    'if(n.getFieldValue("pipeline_parallel_size")===undefined){n.setFieldValue("pipeline_parallel_size",2)}}'
    '}})'
    '}),'
    '(0,D.jsx)(k.Z.Item,{noStyle:!0,'
    'shouldUpdate:function(a,b){return a.serving_topology!==b.serving_topology},'
    'children:function(fv){'
    'var arch=fv.getFieldValue("serving_topology");'
    'var pd=(arch==="pd_disaggregated");'
    'var pp=(arch==="pipeline_parallel");'
    'if(!pd&&!pp){return null}'
    'var out=[];'
    'if(pd){out.push('
    '(0,D.jsx)(k.Z.Item,{name:"pd_node_assign",hidden:!0,children:(0,D.jsx)("input",{style:{display:"none"}})}),'
    '(0,D.jsx)(k.Z.Item,{name:"prefill_groups",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",min:1,max:64,'
    'label:e.formatMessage({id:"models.form.servingTopology.pgroups"})})}'
    '),'
    '(0,D.jsx)(k.Z.Item,{name:"decode_groups",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",min:1,max:64,'
    'label:e.formatMessage({id:"models.form.servingTopology.dgroups"})})}'
    '),'
    '(0,D.jsx)(k.Z.Item,{name:"prefill_gpu_count",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",min:1,max:64,'
    'label:e.formatMessage({id:"models.form.servingTopology.prefill"})})}'
    '),'
    '(0,D.jsx)(k.Z.Item,{name:"decode_gpu_count",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",min:1,max:64,'
    'label:e.formatMessage({id:"models.form.servingTopology.decode"})})}'
    '),'
    '(0,D.jsx)(k.Z.Item,{name:"pd_pipeline_size",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",min:1,max:16,'
    'label:e.formatMessage({id:"models.form.servingTopology.ppSize"})})}'
    '))}'
    'if(pp){out.push('
    '(0,D.jsx)(k.Z.Item,{name:"pipeline_parallel_size",'
    'children:(0,D.jsx)(Y.Z.Input,{type:"number",min:2,max:16,'
    'label:e.formatMessage({id:"models.form.servingTopology.ppSize"})})}'
    '))}'
    'return (0,D.jsx)(D.Fragment,{children:out})'
    '}})'
    ']})'
    '}})'
    ','
)

categ_anchor_key = '(0,D.jsx)(k.Z.Item,{name:"categories","data-field":"categories"'
ci = t.find(categ_anchor_key)
if ci == -1:
    print("!! categories anchor not found")
    sys.exit(1)
t = t[:ci] + ADV_GROUP + t[ci:]
print("advanced-tab topology group injected")

# ============================================================
# 2. worker 列表全局注入: PD 节点分配下拉需要集群节点名列表。
#    注意: 插入点在 D.Fragment 的 children:[ 数组内 — IIFE 必须
#    以逗号结尾作为数组元素 (分号在数组字面量里是语法错误)。
#    自执行 fetch (带 cookie), 结果存 window.__topoWorkers;
#    失败静默 (下拉空, 不阻塞表单)。
WORKER_FETCH = (
    '(function(){if(window.__topoWorkersFetched){return}'
    'window.__topoWorkersFetched=!0;'
    'fetch("/v2/workers",{credentials:"include"}).then(function(r){return r.json()})'
    '.then(function(d){window.__topoWorkers=(d.items||[]).map(function(w){return w.name})})'
    '.catch(function(){})})(),'
)
ci2 = t.find(categ_anchor_key)
if ci2 == -1 or ci2 < ci:
    print("!! categories anchor lost after field group insert")
    sys.exit(1)
t = t[:ci2] + WORKER_FETCH + t[ci2:]
print("worker list fetch injected")

# ============================================================
# 3. 调度 tab: PD rank 节点分配框, 插在 GPU 分配方框 (sectionCard) 之后。
#    渲染条件: scheduleType==="manual" && serving_topology==="pd_disaggregated"
#    && backend==="SGLang"。互斥: 每个 rank 下拉里, 其它 rank 已选节点置灰。
#    锚点: GPU 分配方框 (manual 分支的 sectionCard div) 结束后、
#    p===Z.dY.Auto 放置策略 Fragment 之前 — 即 "]})," 与 ",p===Z.dY.Auto" 之间。
SCHED_ANCHOR = (
    ']}),p===Z.dY.Auto&&(0,D.jsxs)(D.Fragment,{children:['
    '(0,D.jsx)(k.Z.Item,{name:"placement_strategy"'
)
si = t.find(SCHED_ANCHOR)
if si == -1:
    print("!! scheduling anchor not found")
    sys.exit(1)

# 节点分配框组件 (插在 ]}), 之后):
# - Form.Item shouldUpdate 监听 scheduleType/serving_topology/backend/groups/
#   pipeline_size/pd_node_assign — 任何一项变化都重渲染 (置灰集合实时刷新)
# - label 用当前组件的 n.formatMessage (Cn 组件作用域内 e->n 变量名不同,
#   这里在 Cn 内, intl 变量是 n)
SCHED_GROUP = (
    # —— PD 节点分配 (仅手动调度 + PD 分离 + SGLang) ——
    '(0,D.jsx)(k.Z.Item,{noStyle:!0,'
    'shouldUpdate:function(ab,bb){'
    'return ab.scheduleType!==bb.scheduleType'
    '||ab.serving_topology!==bb.serving_topology'
    '||ab.backend!==bb.backend'
    '||ab.prefill_groups!==bb.prefill_groups'
    '||ab.decode_groups!==bb.decode_groups'
    '||ab.pd_pipeline_size!==bb.pd_pipeline_size'
    '||ab.pd_node_assign!==bb.pd_node_assign},'
    'children:function(fs){'
    'if(fs.getFieldValue("scheduleType")!=="manual"){return null}'
    'if(fs.getFieldValue("backend")!=="SGLang"){return null}'
    'if(fs.getFieldValue("serving_topology")!=="pd_disaggregated"){return null}'
    'var pg=fs.getFieldValue("prefill_groups")||1;'
    'var dg=fs.getFieldValue("decode_groups")||1;'
    'var ps=fs.getFieldValue("pd_pipeline_size")||1;'
    'if(ps<1){ps=1}'
    'var asg=fs.getFieldValue("pd_node_assign")||{};'
    'var wnames=window.__topoWorkers||[];'
    'var sides=[["prefill",pg],["decode",dg]];'
    'var out=[];'
    'for(var si2=0;si2<sides.length;si2++){var role=sides[si2][0],cnt=sides[si2][1];'
    'for(var i2=0;i2<cnt;i2++){'
    'var v2=(asg[role]||[])[i2];if(!Array.isArray(v2)){v2=v2?[v2]:[]}'
    # 该 rank 自己的选项: 全部节点, 但其它 rank 已选的置灰 (互斥)
    'var mine={};for(var m2=0;m2<v2.length;m2++){mine[v2[m2]]=1}'
    'var opts2=wnames.map(function(w){'
    'var taken=!mine[w]&&sides.some(function(sd){'
    'var arr=asg[sd[0]]||[];'
    'for(var q=0;q<sd[1];q++){var rv=arr[q];'
    'if(!Array.isArray(rv)){rv=rv?[rv]:[]}'
    'if(rv.indexOf(w)!==-1){return !0}}return !1});'
    'return {label:w,value:w,disabled:taken}});'
    'var lbl2=(role==="prefill"?"Prefill":"Decode")+" rank"+i2+" 节点"+(ps>1?"（选"+ps+"台）":"");'
    'out.push((0,D.jsx)(k.Z.Item,{noStyle:!0,children:'
    '(0,D.jsx)(z.Z,{mode:ps>1?"multiple":void 0,allowClear:!0,'
    'value:ps>1?v2:(v2[0]||void 0),label:lbl2,placeholder:"选择节点",options:opts2,'
    'onChange:(function(role,i2,ps){return function(val){'
    # Cn 组件作用域内 d 是 form instance (k.Z.useFormInstance())
    'var aa=d.getFieldValue("pd_node_assign")||{};'
    'aa[role]=aa[role]||[];'
    'aa[role][i2]=(ps>1||Array.isArray(val))?(val||[]):(val?[val]:[]);'
    'd.setFieldValue("pd_node_assign",aa)}})(role,i2,ps)})}))'
    '}}'
    'return (0,D.jsx)(D.Fragment,{children:out})'
    '}})'
    ','
)
t = t[:si] + ']}),' + SCHED_GROUP + t[si + len(']}),'):]
print("scheduling-tab rank assign group injected")

# 配平守卫: 注入片段手写括号, 历史上出过 item 结尾多 ')' 的失衡 — 产物
# 语法坏了浏览器才在懒加载时报 "Loading chunk 8671 failed". 注入后立刻
# 用括号计数 + node 语法检查兜底 (node 不可用时退回纯计数).
def balance_check(snippet, name):
    _depth = 0
    for _c in snippet:
        if _c in "([{":
            _depth += 1
        elif _c in ")]}":
            _depth -= 1
        if _depth < 0:
            print("!! %s unbalanced at char %d" % (name, snippet.index(_c)))
            sys.exit(1)
    if _depth != 0:
        print("!! %s unbalanced (depth=%d)" % (name, _depth))
        sys.exit(1)


balance_check(ADV_GROUP, "ADV_GROUP")
balance_check(WORKER_FETCH.rstrip(","), "WORKER_FETCH")
balance_check(SCHED_GROUP, "SCHED_GROUP")
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
# 4. locale 文案注入
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
        ("models.form.servingTopology.placeholder", "默认单机部署"),
        ("models.form.servingTopology.standalone", "单机部署"),
        ("models.form.servingTopology.pd", "PD 分离"),
        ("models.form.servingTopology.pipeline", "流水线并行"),
        ("models.form.servingTopology.prefill", "Prefill GPU 数"),
        ("models.form.servingTopology.decode", "Decode GPU 数"),
        ("models.form.servingTopology.ppSize", "流水线并行度PP"),
        ("models.form.servingTopology.pgroups", "Prefill节点数量"),
        ("models.form.servingTopology.dgroups", "Decode节点数量"),
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
