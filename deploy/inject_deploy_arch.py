"""构建期 patch: 注入「服务拓扑」字段组到部署表单 (8671 chunk).

目标: 高级 tab 之后新增「多机」分段, 支持一个模型放不下跨多台机器的场景:
  - standalone       单机部署 (默认, 现状)
  - pipeline_parallel 流水线并行 (几个节点一起跑放不下的模型)
  - pd_disaggregated  PD 分离 (prefill/decode 拆开; 组数 >1 即多 P 多 D,
                      原 multi_pd 选项已合并进来)

实现 (v4 — rank 选点框进 GPU 分配卡 + GPU 选择器让位):
1) 服务拓扑下拉 (serving_topology) + PD/PP 参数字段注入到「高级」tab
   (categories 之前), 仅 backend===SGLang 时渲染。
2) PD rank 节点分配 (pd_node_assign) 框注入到「调度」tab 的 GPU 分配方框
   (sectionCard) **内部**, 替换 GPU 选择器的位置 —
      a. backend === SGLang (vLLM/SGLang PD 参数逻辑不同)
      b. serving_topology === pd_disaggregated
      c. 调度方式 scheduleType === manual (自动调度时节点由调度器摆放)
   三条件全满足时渲染 rank 选点框, **GPU 选择器/每副本 GPU 数让位隐藏**
   (PD 分离下节点级分配与 GPU 级选择器语义冲突, 且 gpu_ids 的 required
   校验会卡住 PD 提交); 非 PD 场景渲染原 GPU 选择器 (官方行为不变)。
3) rank 选点互斥: 任一 rank (Prefill rank0..N / Decode rank0..M) 已选的
   节点在其它 rank 的下拉里置灰不可选, 保证 P/D 各 rank 节点不重叠;
   后端 build_preset_payloads 同样校验 (双保险)。

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
# 0. 幂等清理: 删除历史版本注入
#    v1/v2: rank 节点框在高级 tab; v3-v5: 在调度 tab GPU 分配卡之后。
#    v4 起 rank 框进 GPU 分配卡内 (替换 GPU 选择器位置)。
OLD_START = '(0,D.jsx)(k.Z.Item,{noStyle:!0,shouldUpdate:function(a,b){return a.backend!==b.backend}'
OLD_FETCH = '(function(){if(window.__topoWorkersFetched)'
i_old = t.find(OLD_START)
if i_old != -1:
    i_fetch = t.find(OLD_FETCH, i_old)
    if i_fetch != -1:
        i_end = t.find('})()', i_fetch)
        if i_end != -1:
            t = t[:i_old] + t[i_end + len('})(),'):]
            print("0. removed legacy field group + worker fetch (v<=3)")
    else:
        i_cat = t.find('(0,D.jsx)(k.Z.Item,{name:"categories"', i_old)
        if i_cat != -1:
            t = t[:i_old] + t[i_cat:]
            print("0. removed legacy field group (no fetch)")
if OLD_FETCH in t:
    i_fetch = t.find(OLD_FETCH)
    i_end = t.find('})()', i_fetch)
    if i_end != -1:
        t = t[:i_fetch] + t[i_end + len('})(),'):]
        print("0. removed stray worker fetch")
# v3-v5 的调度 tab 注入 (GPU 分配卡之后): shouldUpdate(ab,bb) 开头,
# 结尾是 ',p===Z.dY.Auto' 前的那个 item
OLD_SCHED = '(0,D.jsx)(k.Z.Item,{noStyle:!0,shouldUpdate:function(ab,bb){return ab.scheduleType!==bb.scheduleType'
i_s = t.find(OLD_SCHED)
if i_s != -1:
    # 注入体是一个数组元素, 以 ',' 结尾, 下一个是 ',p===Z.dY.Auto' —
    # 删除从 i_s 到 p===Z.dY.Auto 前的所有内容 (即注入的 item + 尾逗号)
    i_a = t.find('p===Z.dY.Auto&&(0,D.jsxs)(D.Fragment,{children:[(0,D.jsx)(k.Z.Item,{name:"placement_strategy"', i_s)
    if i_a != -1:
        t = t[:i_s] + t[i_a:]
        print("0. removed legacy scheduling-tab injection (v3-v5)")
# v4 幂等标记: GPU 选择器替换处 (带 __pdRankSlot 标记的版本重新运行时先还原)
if '__pdRankSlot' in t:
    # 已是 v4 注入 — 还原方式: 整段替换块在 fresh 基础上生成, 直接报错让
    # 用户用干净 dist 重跑 (构建链每次从官方 tarball 解压, 不会走到这)
    print("0. v4 injection already present (fresh dist expected)")
    sys.exit(0)

# ============================================================
# 1. 高级 tab: 服务拓扑下拉 + PD/PP 参数字段 (无节点分配框 — 节点框在调度 tab)
#    结构: Form.Item(shouldUpdate: backend 变化) -> Fragment:
#      - serving_topology 下拉 (onChange 播种 PD/PP 默认值)
#      - Form.Item(shouldUpdate: topology 变化) -> Fragment:
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
# 3. 调度 tab GPU 分配方框: PD 分离时 GPU 选择器让位给 rank 选点框。
#    原结构 (Cn 组件, manual 分支 sectionCard 内):
#      x===Z.FH.VGPU ? (wn) : (Fragment:[gpu_ids_item, per_replica_item])
#    替换为:
#      x===Z.FH.VGPU ? (wn) : (Fragment:[
#        isPD ? [rank 选点框...] : [gpu_ids_item, per_replica_item]
#      ])
#    isPD = SGLang + pd_disaggregated (调度 tab 此分支必为 manual, 无需再判)。
#    - rank 框在卡片内 (与 GPU 选择器同位), margin 与官方字段一致
#    - alwaysFocus:!0 — seal-select label 永久上浮, 空值 blur 不掉落
#      (onBlur: value||setFocus(false) — undefined 掉; alwaysFocus 钉住)
#    - 互斥置灰 + 深拷贝写回 (v5 修复) + style width 100% (v8 修复) 保留
GPU_VGPU_ANCHOR = (
    ']}),x===Z.FH.VGPU?(0,D.jsx)(wn,{}):(0,D.jsxs)(D.Fragment,{children:['
    '(0,D.jsx)(k.Z.Item,{"data-field":"gpu_selector.gpu_ids"'
)
gi = t.find(GPU_VGPU_ANCHOR)
if gi == -1:
    print("!! gpu allocation anchor not found")
    sys.exit(1)

# GPU 选择器 item + 每副本 item 的原文 (保留, 非 PD 时渲染)。
# 括号配平定位 Fragment children 数组闭合 (数组内容 = 两个 GPU item)。
gpu_ids_start = gi + len(']}),x===Z.FH.VGPU?(0,D.jsx)(wn,{}):(0,D.jsxs)(D.Fragment,{children:[')
_arr_i = gpu_ids_start  # 指向 children 数组首个元素 ('[' 已在 start 前)
_depth = 1              # 预置数组 '[' 的深度; 归零点即 ']' (数组闭合)
while _arr_i < len(t):
    _c = t[_arr_i]
    if _c in "([{":
        _depth += 1
    elif _c in ")]}":
        _depth -= 1
        if _depth == 0:
            break
    _arr_i += 1
arr_end = _arr_i                      # children 数组 ']' 下标
frag_close = arr_end + len("]})")     # Fragment 表达式结束下标
if t[frag_close:frag_close + 4] != "]}),":
    print("!! unexpected structure after gpu fragment:", repr(t[frag_close:frag_close + 20]))
    sys.exit(1)
gpu_items_src = t[gpu_ids_start:arr_end]  # 两个 item (不含数组括号与 Fragment 闭合)

# rank 选点框生成器 (children 函数内联, 挂在 __pdRankSlot 标记的组件里):
# 用一个 noStyle Form.Item(shouldUpdate) 感知 topology/backend/groups 变化,
# 返回 rank 框数组; 非手动+PD 时返回 null (此时外层三元已不进这支,
# 双保险)。
RANK_BOXES = (
    # __pdRankSlot: 构建幂等标记 (fresh dist 无此串)
    '(0,D.jsx)(k.Z.Item,{noStyle:!0,__pdRankSlot:!0,'
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
    # 多选 rank 框高度放开 (一次性注入, __pdRankCss 幂等):
    # seal-select 外壳链 (外层 div / wrapper / ant-select) 都是固定 54px,
    # multiple 的 tag 行需要 ~36px 内容高 + 20px label 区 = 放不下,
    # tag 被 overflow:hidden 裁切。:has 命中多选框后 height:auto,
    # 单选框 (无 .ant-select-multiple) 不受影响。
    'if(!window.__pdRankCss){window.__pdRankCss=1;'
    'var st=document.createElement("style");st.textContent='
    '".seal-select-wrapper:has(.ant-select-multiple){height:auto;min-height:54px}"'
    '+".seal-select-wrapper:has(.ant-select-multiple) .ant-select{height:auto}"'
    '+".seal-select-wrapper:has(.ant-select-multiple) .ant-select-content{height:auto;overflow:visible}"'
    '+".seal-select-wrapper:has(.ant-select-multiple) .__inner__{height:auto}"'
    '+":has(> .seal-select-wrapper .ant-select-multiple){height:auto;min-height:54px}";'
    'document.head.appendChild(st)}'
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
    'out.push((0,D.jsx)(k.Z.Item,{noStyle:!0,style:{marginBottom:16},children:'
    # alwaysFocus:!0 — label 永久上浮: seal-select onBlur 是
    # (allowNull&&value===null)?保持:(value||setFocus(false)) — 空值是
    # undefined 不是 null, blur 后 label 掉回框中间、focus 又弹起 = 上下动;
    # alwaysFocus 钉住 isFocus, label 不再动 (官方 run_command 同款用法)。
    # allowNull:!0 保留 (空值入场时也上浮, 避免与 placeholder 重叠)。
    # 不传 getPopupContainer — 官方 z.Z 字段均不传 (面板挂 body);
    # 挂 parentNode 会让 rc-trigger 在框内 re-mount 面板, rc-select
    # 受控状态被重置 (v6 实测回归)。
    # style width 100% — noStyle Form.Item 无 name, 拿不到
    # ant-select-in-form-item 的 width:100% CSS, 不传会收缩到内容宽。
    '(0,D.jsx)(z.Z,{mode:ps>1?"multiple":void 0,allowClear:!0,allowNull:!0,'
    'alwaysFocus:!0,style:{width:"100%"},'
    # maxTagCount:1 — multiple 模式 (PP>1) 只显示首个 tag + 「+N」计数:
    # 框高固定 54px (label 区 20px + 内容 34px), 多个 32px tag 换行时
    # 超出 content 盒被 overflow:hidden 裁切 (tag 底部切 16px, 实测);
    # 单 tag + 计数保持单行高度, 不裁切 (官方 GPU 选择器 cascader 同款做法)。
    # placeholder: 多选模式 (PP>1) 下留空 — rc-select 多选的 placeholder 字形
    # (16px, 从 padding 区起) 与上浮 label 字形真实交叠 ~5px (Range 实测);
    # 单选模式 placeholder 21px 盒字形居中, 与 label 错开无重叠。
    # 多选时 label 自带「（选N台）」已是足够提示 (官方 GPU 选择器同款留空做法)。
    'value:ps>1?v2:(v2[0]||void 0),label:lbl2,placeholder:ps>1?"":"选择节点",'
    # multiple 模式紧凑 tag: 框高 54px 里 label 区占 20px, 剩 18px 可视高度;
    # 默认 tag 32px 被 overflow:hidden 裁切 16px (文字切半)。
    # tagRender 自定义 18px 紧凑 tag (antd 标准 prop, seal-select {...S} 透传);
    # maxTagCount:1 只留首个 tag + 计数, 防多 tag 换行进一步超高
    # (官方 GPU 选择器 cascader 同款单行策略)。
    'maxTagCount:ps>1?1:void 0,maxTagTextLength:18,'
    'tagRender:function(pr){return (0,D.jsx)("span",{'
    'style:{display:"inline-flex",alignItems:"center",height:20,'
    'lineHeight:"20px",fontSize:12,padding:"0 6px",margin:"0 4px 0 0",'
    'borderRadius:4,background:"var(--ant-color-fill-secondary)",'
    'color:"var(--ant-color-text)",maxWidth:320,overflow:"hidden",'
    'whiteSpace:"nowrap",textOverflow:"ellipsis"},'
    'children:pr.label})},'
    'options:opts2,'
    'onChange:(function(role,i2,ps){return function(val){'
    # Cn 组件作用域内 d 是 form instance (k.Z.useFormInstance())。
    # 深拷贝后再写回: getFieldValue 取出的是 store 里的引用, 原地改再 set
    # 同一引用 — rc-field-form 引用相等跳过通知, shouldUpdate 不触发,
    # 其它 rank 下拉的置灰 (互斥) 不刷新。JSON 深拷贝换新引用即可。
    'var aa=JSON.parse(JSON.stringify(d.getFieldValue("pd_node_assign")||{}));'
    'aa[role]=aa[role]||[];'
    'aa[role][i2]=(ps>1||Array.isArray(val))?(val||[]):(val?[val]:[]);'
    'd.setFieldValue("pd_node_assign",aa)}})(role,i2,ps)})}))'
    '}}'
    'return (0,D.jsx)(D.Fragment,{children:out})'
    '}})'
)

# 替换: 在 Fragment children 数组开头插入条件三元 —
#   isPD ? (rank框 Form.Item) : (原 gpu_ids + per_replica)
# isPD 由外层再包一个 noStyle Form.Item(shouldUpdate: serving_topology/backend)
# 提供 (它内部渲染三元)。
# 注意: Fragment 头 '(0,D.jsxs)(D.Fragment,{children:[' 在 gpu_ids_start 之前、
# 尾 '})' 在 arr_end 之后 — 均保留不动。PD_SWITCH 只替换**数组内容**:
# children:[ PD_SWITCH ] — 一个 Form.Item(shouldUpdate) 其 children 函数按
# isPD 三元返回 rank 框数组或原 GPU items 数组 (react children 数组合法)。
PD_SWITCH = (
    '(0,D.jsx)(k.Z.Item,{noStyle:!0,__pdRankSwitch:!0,'
    'shouldUpdate:function(ab,bb){'
    'return ab.serving_topology!==bb.serving_topology||ab.backend!==bb.backend},'
    'children:function(fw){'
    'var isPD=(fw.getFieldValue("backend")==="SGLang"'
    '&&fw.getFieldValue("serving_topology")==="pd_disaggregated");'
    'return isPD?['
    + RANK_BOXES +
    ']:['
    + gpu_items_src +
    ']'
    '}})'
)

t = t[:gpu_ids_start] + PD_SWITCH + t[arr_end:]

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
balance_check(RANK_BOXES, "RANK_BOXES")
balance_check(PD_SWITCH, "PD_SWITCH")
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
