#!/usr/bin/env python3
"""构建链最后一步 (在 patch_ui_dist / purge_billing_locale / inject_deploy_arch
之后运行): 按『最终内容』rehash 全部 chunk + umi.js 本体, 并更新 index.html.

为什么必须放最后: patch 链会修改 chunk 内容 (菜单/locale/多机注入等).
webpack 产物带 `cache-control: max-age=31536000, immutable`, 内容变了但
文件名不变 => 浏览器永远用旧缓存 ("de.formatMessage is not a function"
和 "Loading chunk XXX failed" 都是这一类). 只有在全部修改完成后按最终
内容重算 md5 hash 改名 (URL 变化), 浏览器才会重新拉取.

处理顺序:
1. 遍历 js/*.chunk.js, 内容 md5 前 8 位 != 文件名 hash 的 => 改名,
   并把 umi.js 里 `cid:"old_hash"` 映射同步改为新 hash
   (数字 chunk 直接用 id; p__ 命名 chunk 先从 name 表反查 cid;
    同 chunk 的 .chunk.css 若与 js 同 hash 一并改名, 保持表项一致).
2. umi.js 内容已变 => 按内容重命名 umi.<newhash>.js + 更新 index.html.
3. umi.css 同理.

用法: UI_DIR=<pkg>/ui python3 rehash_umi_entry.py
"""
import glob
import gzip
import hashlib
import os
import re
import sys


def regen_gz(path):
    gz = path + ".gz"
    with open(path, "rb") as fi:
        data = fi.read()
    with open(gz + ".tmp", "wb") as fo:
        g = gzip.GzipFile(filename="", mode="wb", fileobj=fo, mtime=0)
        g.write(data)
        g.close()
    os.replace(gz + ".tmp", gz)


def rehash_chunks(base, umi_path):
    js_dir = os.path.join(base, "js")
    css_dir = os.path.join(base, "css")
    umi = open(umi_path, encoding="utf-8").read()
    n = 0
    for fp in sorted(glob.glob(os.path.join(js_dir, "*.chunk.js"))):
        name = os.path.basename(fp)
        stem = name[: -len(".chunk.js")]
        parts = stem.rsplit(".", 1)
        if len(parts) != 2:
            continue
        base_id, old_hash = parts
        if not re.fullmatch(r"[a-f0-9]{8}", old_hash):
            continue
        with open(fp, "rb") as fi:
            data = fi.read()
        new_hash = hashlib.md5(data).hexdigest()[:8]
        if new_hash == old_hash:
            continue
        # 数字 chunk 直接用 id; 命名 chunk (p__xxx) 从 name 表反查 cid
        if base_id.isdigit():
            cid = base_id
        else:
            m = re.search(r'(\d+):"' + re.escape(base_id) + '"', umi)
            if not m:
                print("  skip(no-cid): " + name)
                continue
            cid = m.group(1)
        # js 改名 + gz
        new_fp = os.path.join(js_dir, base_id + "." + new_hash + ".chunk.js")
        if os.path.exists(new_fp):
            os.remove(new_fp)
        os.rename(fp, new_fp)
        if os.path.exists(fp + ".gz"):
            os.remove(fp + ".gz")
        regen_gz(new_fp)
        # 同 chunk 的 css 若与 js 同名 hash, 一并改名 (两张表共用同一表项)
        old_css = os.path.join(css_dir, base_id + "." + old_hash + ".chunk.css")
        if os.path.exists(old_css):
            new_css = os.path.join(css_dir, base_id + "." + new_hash + ".chunk.css")
            if os.path.exists(new_css):
                os.remove(new_css)
            os.rename(old_css, new_css)
            if os.path.exists(old_css + ".gz"):
                os.remove(old_css + ".gz")
            regen_gz(new_css)
        # 更新 umi 映射 (JS/CSS 两张表凡 old_hash 的全部替换)
        pat = cid + ':"' + old_hash + '"'
        if pat in umi:
            umi = umi.replace(pat, cid + ':"' + new_hash + '"')
        print("  chunk: " + name + " -> " + base_id + "." + new_hash + ".chunk.js")
        n += 1
    if n:
        open(umi_path, "w", encoding="utf-8").write(umi)
    print("chunks rehashed: %d" % n)
    return n


def main():
    base = os.environ.get("UI_DIR")
    if not base or not os.path.isdir(base):
        print("!! UI_DIR not set:", base)
        sys.exit(1)
    js_dir = os.path.join(base, "js")
    idx = os.path.join(base, "index.html")
    umis = [p for p in glob.glob(os.path.join(js_dir, "umi.*.js"))
            if not p.endswith(".gz")]
    if not umis or not os.path.exists(idx):
        print("!! umi.js or index.html not found")
        sys.exit(1)
    umi_path = umis[0]

    # 1. 按最终内容 rehash 全部 chunk (会改写 umi.js 内容)
    rehash_chunks(base, umi_path)

    # 2. umi.js 本体: 内容已变 => 重命名 + 更新 index.html
    t = open(idx, encoding="utf-8").read()
    m = re.search(r'src="([^"]*umi\.[a-f0-9]+\.js)"', t)
    if not m:
        print("!! umi src not found in index.html")
        sys.exit(1)
    umi_ref = m.group(1)
    umi_name = os.path.basename(umi_ref)
    data_hash = hashlib.md5(open(umi_path, "rb").read()).hexdigest()[:8]
    if data_hash in umi_name:
        print("umi.js unchanged: " + umi_name)
    else:
        new_name = "umi." + data_hash + ".js"
        new_path = os.path.join(js_dir, new_name)
        if os.path.exists(new_path):
            os.remove(new_path)
        os.rename(umi_path, new_path)
        if os.path.exists(umi_path + ".gz"):
            os.remove(umi_path + ".gz")
        regen_gz(new_path)
        t = t.replace(umi_ref, umi_ref.replace(umi_name, new_name))
        print("umi.js: " + umi_name + " -> " + new_name)

    # 3. umi.css 同理
    m2 = re.search(r'href="([^"]*umi\.[a-f0-9]+\.css)"', t)
    if m2:
        css_ref = m2.group(1)
        css_name = os.path.basename(css_ref)
        css_path = os.path.join(base, "css", css_name)
        if os.path.exists(css_path):
            h = hashlib.md5(open(css_path, "rb").read()).hexdigest()[:8]
            if h not in css_name:
                new_css = "umi." + h + ".css"
                new_path = os.path.join(base, "css", new_css)
                if os.path.exists(new_path):
                    os.remove(new_path)
                os.rename(css_path, new_path)
                if os.path.exists(css_path + ".gz"):
                    os.remove(css_path + ".gz")
                regen_gz(new_path)
                t = t.replace(css_ref, css_ref.replace(css_name, new_css))
                print("umi.css: " + css_name + " -> " + new_css)

    open(idx, "w", encoding="utf-8").write(t)
    print("rehash_umi_entry DONE")


if __name__ == "__main__":
    main()