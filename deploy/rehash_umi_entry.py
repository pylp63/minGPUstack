#!/usr/bin/env python3
"""rehash umi.js 本体 + CSS + index.html 引用.

修复根因: umi.js 带 immutable 缓存头, 文件名 hash 若不随内容变化,
浏览器永远用缓存的旧 umi.js (其内 chunk map 是旧 hash), 导致请求已被
rehash 删除的旧 chunk 文件 -> "Loading chunk XXX failed (missing ...)".

用法: UI_DIR=<pkg>/ui python3 rehash_umi_entry.py
"""
import glob
import hashlib
import os
import re
import sys


def main():
    base = os.environ.get("UI_DIR")
    if not base or not os.path.isdir(base):
        print("!! UI_DIR not set:", base)
        sys.exit(1)

    js = os.path.join(base, "js")
    idx = os.path.join(base, "index.html")
    if not os.path.exists(idx):
        print("!! index.html not found")
        sys.exit(1)

    t = open(idx, encoding="utf-8").read()

    # 找到当前 umi js 引用
    m = re.search(r'src="([^"]*umi\.[a-f0-9]+\.js)"', t)
    if not m:
        print("!! umi js src not found in index.html")
        sys.exit(1)
    umi_ref = m.group(1)  # /js/umi.f4cb788e.js
    umi_name = os.path.basename(umi_ref)
    umi_path = os.path.join(js, umi_name)
    if not os.path.exists(umi_path):
        print("!! umi js file missing:", umi_path)
        sys.exit(1)

    data_hash = hashlib.md5(open(umi_path, "rb").read()).hexdigest()[:8]
    if data_hash in umi_name.split(".")[1]:
        print("umi.js hash unchanged, skip")
    else:
        new_name = "umi." + data_hash + ".js"
        new_path = os.path.join(js, new_name)
        if os.path.exists(new_path):
            os.remove(new_path)
        os.rename(umi_path, new_path)
        t = t.replace(umi_ref, umi_ref.replace(umi_name, new_name))
        print("umi.js renamed %s -> %s" % (umi_name, new_name))

    # CSS 同理
    m2 = re.search(r'href="([^"]*umi\.[a-f0-9]+\.css)"', t)
    if m2:
        css_ref = m2.group(1)
        css_name = os.path.basename(css_ref)
        css_path = os.path.join(base, "css", css_name)
        if os.path.exists(css_path):
            h = hashlib.md5(open(css_path, "rb").read()).hexdigest()[:8]
            if h not in css_name:
                new_css = "umi." + h + ".css"
                os.rename(css_path, os.path.join(base, "css", new_css))
                t = t.replace(css_ref, css_ref.replace(css_name, new_css))
                print("umi.css %s -> %s" % (css_name, new_css))

    open(idx, "w", encoding="utf-8").write(t)
    print("rehash_umi_entry DONE")


if __name__ == "__main__":
    main()