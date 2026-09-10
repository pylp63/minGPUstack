"""构建期 patch: 在官方模型部署表单 (8671 chunk) 注入「多机」tab + 多机字段.

改动点:
1. Ze 数组 (tab 定义) 末尾追加「多机」tab, value=multiNode,
   field=deploy_architecture (锚点滚动到多机字段).
2. deploy_architecture 下拉升级为字段组: 架构下拉 + 动态数字字段
   (P 数 / D 数 / P 副本 / D 副本 / 流水线并行度), onChange 翻译成
   backend_parameters 写回表单.
3. locale 注入 deploy.multiNode.tab 等文案.

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
        with open(gz + ".tmp", "wb") as fo:
            g = gzip.GzipFile(filename="", mode="wb", fileobj=fo, mtime=0)
            g.write(fi.read())
            g.close()
    os.replace(gz + ".tmp", gz)


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

# ---- 1. Ze 数组追加「多机」tab ----
tab_anchor = 'field:"categories"}]'
if tab_anchor not in t:
    print("!! Ze tab anchor not found")
    sys.exit(1)
tab_new = (
    'field:"categories"},'
    '{value:"multiNode",label:de.formatMessage({id:"deploy.multiNode.tab"}),'
    'icon:(0,D.jsx)(_.Z,{type:"icon-cluster-outlined"}),'
    'field:"deploy_architecture"'
    '}]'
)
t = t.replace(tab_anchor, tab_new, 1)
print("1. Ze 数组已加 multiNode tab")

# ---- 2. 升级 deploy_architecture 为多机字段组 ----
arch_anchor = '{children:[(0,D.jsx)(k.Z.Item,{name:"deploy_architecture"'
if arch_anchor not in t:
    print("!! deploy_architecture anchor not found")
    sys.exit(1)

# 找到 deploy_architecture 字段的完整结束（到下一个 k.Z.Item name:"categories"）
categ_anchor = '(0,D.jsx)(k.Z.Item,{name:"categories","data-field":"categories"'
categ_idx = t.find(categ_anchor时事)
arch_idx = t.find(arch_anchor)
if arch_idx == -1 or categ_idx == -1 or categ_idx <= arch_idx:
    print("!! field group anchors drifted")
    sys.exit(1)

# 把 arch_idx 到 categ_idx 之间替换为新的字段组
multi_node_fields = (
    '(0,D.jsx)(k.Z.Item,{name:"deploy_architecture",label:'
    'e.formatMessage({id:"deploy.arch.label"}),"data-field":"deploy_architecture",'
    'style:{scrollMarginTop:200},'
    'children:(0,D.jsx)(z.Z,{allowNull:!0,'
    'placeholder:e.formatMessage({id:"deploy.arch.placeholder"}),'
    'options:[{label:"单机部署",value:"standalone"},'
    '{label:"PD 分离",value:"pd_disaggregated"},'
    '{label:"多P多D",value:"multi_pd"},'
    '{label:"流水线并行",value:"pipeline_parallel"}],'
    'onChange:function(v){'
    'var params=[];'
    'if(v==="pipeline_parallel"){'
    'params=["--pipeline-parallel-size="+(n.getFieldValue("num_pipeline")||4)]'
    '}else if(v==="pd_disaggregated"){'
    'params=["--tensor-parallel-size="+(n.getFieldValue("num_p")||1),"--api-server-type=prefill"]'
    '}else if(v==="multi_pd"){'
    'params=["--tensor-parallel-size="+(n.getFieldValue("num_p")||1),"--api-server-type=prefill"]'
    '}'
    'n.setFieldValue("backend_parameters",params)'
    '}})} ),'
    '(0,D.jsx)(k.Z.Item,{name:"num_p",label:"P (prefill) 数量","data-field":"deploy_architecture",children:(0,D.jsx)(Y.Z.Input,{type:"number",defaultValue:1})}),'
    '(0,D.jsx)(k.Z.Item,{name:"num_d",label:"D (decode) 数量",children:(0,D.jsx)(Y.Z.Number,{type:"number",defaultValue:1})})'

t = t[:arch_idx] + multi_node_fields_replace + t[categ_idx:]