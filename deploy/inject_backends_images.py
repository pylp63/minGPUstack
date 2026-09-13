"""构建期 patch: 推理后端「版本列表」条目下新增浅灰镜像全名行 (二开).

需求: 内置推理后端 (vLLM/SGLang/...) 的版本条目 (版本号 + 内置 + 框架 tags)
下方, 以浅灰色小字显示该版本按框架解析出的 runner 镜像全名, 例如:
  镜像: gpustack/runner:cuda13.0-vllm0.27.1 · gpustack/runner:cuda12.9-vllm0.27.1 · gpustack/runner:rocm7.2-vllm0.27.1

数据: 后端 VersionConfig.framework_images (routes/inference_backend.py
get_runner_versions_and_configs 填充, {framework: "img1 · img2"})。

改动点 (p__backends__index chunk):
1. 版本弹窗数据适配: pick 列表加 "framework_images" 字段透传
2. 版本条目组件 (ft) 框架行后追加镜像行 (仅内置版本且有数据时渲染)

用法: UI_DIR=<pkg>/ui python3 inject_backends_images.py
"""
import glob
import gzip
import os
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


files = glob.glob(os.path.join(JS, "p__backends__index.*.chunk.js"))
files = [f for f in files if not f.endswith(".gz")]
if not files:
    print("!! backends page chunk not found")
    sys.exit(1)
f = files[0]
t = read(f)
orig = t

# ============================================================
# 1. 数据适配: pick 透传 framework_images
# ============================================================
PICK_ANCHOR = 'k().pick(r,["image_name","run_command","entrypoint","env","backend_source"])'
if PICK_ANCHOR not in t:
    print("!! version pick anchor not found")
    sys.exit(1)
t = t.replace(
    PICK_ANCHOR,
    'k().pick(r,["image_name","run_command","entrypoint","env","backend_source","framework_images"])',
    1,
)
print("framework_images passthrough added to version data adapter")

# ============================================================
# 2. 版本条目组件: 框架行后追加浅灰镜像行
# ============================================================
# 框架行 JSX (ft 组件尾部, dt 容器内最后一行); 行尾是
# ]})]})]})} = 框架span闭 + ut行容器闭 + dt闭 + ft根闭。
FRAME_HEAD = (
    '(0,Se.jsx)("span",{className:"text drivers",children:null===(n=a.availableFrameworks)'
    '||void 0===n?void 0:n.map((function(e){return(0,Se.jsx)(we.Z,{style:{marginRight:0},'
    'color:(0,Pe.G7)(e),children:e},e)}))})'
)
if t.count(FRAME_HEAD) != 1:
    print("!! framework row anchor not unique:", t.count(FRAME_HEAD))
    sys.exit(1)

# 镜像行: 深插到框架行 span 之后 (ut 行容器内追加一个兄弟节点),
# 文本浅灰 12px, 单行省略 + title 提示。
# framework_images = {framework: "img1 · img2"}; 有数据才渲染。
IMG_ROW = (
    FRAME_HEAD + ','
    # ---- 二开: 镜像全名行 (浅灰) ----
    '(function(){var fi=a.framework_images;'
    'if(!fi||!Object.keys(fi).length){return null}'
    # 按框架顺序拼接: "cuda: img1 · img2  /  rocm: img3"
    'var parts=[];'
    'Object.keys(fi).forEach(function(fw){'
    'if(fi[fw]){parts.push(fw+": "+fi[fw])}});'
    'if(!parts.length){return null}'
    'var txt=parts.join("  /  ");'
    'return (0,Se.jsxs)("div",{style:{marginTop:2,display:"flex",gap:6},children:['
    '(0,Se.jsx)("span",{className:"label",style:{fontSize:12,flexShrink:0},children:"镜像:"}),'
    '(0,Se.jsx)("span",{title:txt,style:{fontSize:12,color:"var(--ant-color-text-tertiary)",'
    'overflow:"hidden",whiteSpace:"nowrap",textOverflow:"ellipsis"},'
    'children:txt})'
    ']})})()'
)
t = t.replace(FRAME_HEAD, IMG_ROW, 1)
print("image row injected into version item component")

if t == orig:
    print("!! no-op")
    sys.exit(1)

# ---- 语法守卫 ----
try:
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as tf:
        tf.write(t.encode())
        tmp_name = tf.name
    try:
        subprocess.run(["node", "--check", tmp_name], timeout=30, check=True)
    finally:
        os.unlink(tmp_name)
    print("backends chunk syntax check (node): OK")
except FileNotFoundError:
    print("(node unavailable — count-only)")
except subprocess.CalledProcessError:
    print("!! backends chunk fails node --check")
    sys.exit(1)

write(f, t)
regen_gz(f)
print("wrote " + os.path.basename(f))
print("inject_backends_images ALL DONE")
