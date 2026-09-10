"""构建期 UI 补充清理 (在 patch_ui_dist.py 之后运行):

语言包里的计费死文案清零 — billing 菜单/页面已删, 但 zh/en/tr 语言包
里 billing.upsell.* / menu.billingAndUsage.billing 等键值还在, 一旦有
组件 (或未来代码路径) 引用就会重新露出企业版文案. 全部替换为空串,
并把 billing 页 chunk 从 webpack 分发表里移除引用.

用法: UI_DIR=<pkg>/ui python3 purge_billing_locale.py
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


def regen_gz(path):
    gz = path + ".gz"
    if not os.path.exists(gz):
        return
    with open(path, "rb") as f_in:
        with open(gz + ".tmp", "wb") as f_out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=f_out, mtime=0) as g:
                g.write(f_in.read())
    os.replace(gz + ".tmp", gz)


# 1) 所有 chunk + umi: 计费相关 i18n 键值清空
KEYS = [
    "billing.upsell.title",
    "billing.upsell.subtitle",
    "billing.upsell.cta",
    "billing.upsell.featuresTitle",
    "billing.upsell.feature.usage",
    "billing.upsell.feature.invoices",
    "billing.upsell.feature.budgets",
    "billing.upsell.feature.chargeback",
    "menu.billingAndUsage.billing",
]
pattern = re.compile(
    r'"(' + "|".join(re.escape(k) for k in KEYS) + r')":"(?:[^"\\]|\\.)*"'
)

patched_files = 0
for path in sorted(glob.glob(os.path.join(JS, "*.js"))):
    try:
        with open(path, encoding="utf-8", errors="strict") as f:
            t = f.read()
    except Exception:
        continue
    if not pattern.search(t):
        continue
    t2 = pattern.sub(lambda m: '"%s":""' % m.group(1), t)
    # menu.billingAndUsage (组名) 已改为 使用量, 这里再兜底: 若还有 "使用量与计费" 文案一并清
    t2 = t2.replace("使用量与计费", "使用量").replace("Usage & Billing", "Usage")
    with open(path, "w", encoding="utf-8") as f:
        f.write(t2)
    regen_gz(path)
    patched_files += 1
    print(f"purged billing locale keys in {os.path.basename(path)}")

print(f"{patched_files} file(s) purged")

# 2) umi.js: webpack 分发表里移除 billing 页 chunk 映射 (1276)
umi = glob.glob(os.path.join(JS, "umi.*.js"))
if umi:
    with open(umi[0], encoding="utf-8", errors="strict") as f:
        u = f.read()
    u2 = u.replace("1276:\"p__billing__index\",", "")
    if u2 != u:
        with open(umi[0], "w", encoding="utf-8") as f:
            f.write(u2)
        regen_gz(umi[0])
        print("removed p__billing__index from webpack chunk map")

print("billing purge OK")
