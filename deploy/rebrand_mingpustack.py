"""构建期 patch: 品牌更名 GPUStack -> minGPUstack (二开).

只改用户可见的品牌文案, 不动任何技术标识:
  改: <title>GPUStack</title>, 侧栏官网链接 label:"GPUStack",
      locale 文案里的 GPUStack (中/英/日/俄/土 全语言),
      Helm values 模板注释里的 GPUStack's own
  不改 (词边界排除):
  - GPUSTACK_* 环境变量/全局变量 (GPUSTACK_API_KEY 等 56+ 处)
  - gpustack 小写 (包名/URL/API 路径/api.github 等链接)
  - GPUStack.ai 域名 (链接, 保留跳官方)
  - GPUStack-Model (模型品类名)

替换规则 (对大小写敏感, 词边界):
  "GPUStack" -> "minGPUstack"   (前面不是字母数字, 后面不是小写字母——
                                避免 GPUStack'ai / GPUStack'te 这类撇号
                                组合词被截断; 撇号组合词整体替换)

用法: UI_DIR=<pkg>/ui python3 rebrand_mingpustack.py
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

NEW_BRAND = "minGPUstack"


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


# 组合词形态 (先整体替换, 防止词边界规则把 GPUStack'te 截成
# minGPUstack'te 之外更怪的形状 — 撇号后是品牌所有格, 整体换):
POSSESSIVE = re.compile(r"GPUStack(['’](?:in|te|nin|teki|nde|ndeki|nin)?)")
# 普通词边界形态: 前面不是 [A-Za-z0-9_], 后面不是小写字母 (排除
# GPUSTACK_/gpustack 已由大小写天然区分; 后接大写/标点/空格/中文都换)
WORD = re.compile(r"(?<![A-Za-z0-9_])GPUStack(?![a-z])")

total_files = 0
total_hits = 0

# ---- index.html: <title> ----
ih = os.path.join(BASE, "index.html")
if os.path.isfile(ih):
    t = read(ih)
    n = t.count("<title>GPUStack</title>")
    if n:
        t = t.replace("<title>GPUStack</title>", f"<title>{NEW_BRAND}</title>")
        write(ih, t)
        total_files += 1
        total_hits += n
        print(f"index.html: title -> {NEW_BRAND}")

# ---- js: 全部产物 ----
for f in sorted(
    glob.glob(os.path.join(BASE, "js", "*.js"))
    + glob.glob(os.path.join(BASE, "js", "*.chunk.js"))
):
    if f.endswith(".gz"):
        continue
    try:
        t = read(f)
    except Exception:
        continue
    orig = t
    hits = 0

    # 1) 侧栏链接 label / title 等纯品牌 (词边界规则内)
    # 2) 组合词 (整体)
    t2 = POSSESSIVE.sub(lambda m: NEW_BRAND + m.group(1), t)
    hits += len(POSSESSIVE.findall(t))
    # 3) 普通词边界
    t3 = WORD.sub(NEW_BRAND, t2)
    hits += len(WORD.findall(t2))

    if t3 != orig:
        write(f, t3)
        regen_gz(f)
        total_files += 1
        total_hits += hits
        print(f"{os.path.basename(f)}: {hits} 处品牌替换")

# ---- css (如有 brand 字样) ----
for f in sorted(glob.glob(os.path.join(BASE, "css", "*.css"))):
    if f.endswith(".gz"):
        continue
    try:
        t = read(f)
    except Exception:
        continue
    if "GPUStack" in t:
        t2 = WORD.sub(NEW_BRAND, t)
        write(f, t2)
        regen_gz(f)
        total_files += 1
        print(f"{os.path.basename(f)}: css 品牌替换")

print(f"rebrand done: {total_files} files, {total_hits} 处 GPUStack -> {NEW_BRAND}")

# ---- 守卫: 技术标识绝不能被改 ----
# 抽验关键串仍在 (读回刚写的文件)
for probe, where in (
    ("GPUSTACK_API_KEY", "umi"),
    ("gpustack.ai", "umi"),
):
    found = False
    for f in glob.glob(os.path.join(BASE, "js", "*.js")) + glob.glob(
        os.path.join(BASE, "js", "*.chunk.js")
    ):
        if f.endswith(".gz"):
            continue
        try:
            if probe in read(f):
                found = True
                break
        except Exception:
            continue
    status = "OK" if found else "MISSING!"
    print(f"guard {probe}: {status} ({where})")
    if not found:
        sys.exit(1)
