#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 SVG 里引用的外部位图内嵌成 base64，让 SVG 变成单文件。

为什么需要：print_ready_doctor 产出的 cutline.svg 里写的是
    <image xlink:href="../FINAL.png" .../>
你要是只把这个 svg 发给工厂，图就丢了，工厂只会看到一堆刀线。
内嵌之后一个文件就能发，代价是体积变大。

用法：
    python3 embed_svg.py 输入.svg -o 输出.svg
    python3 embed_svg.py 输入.svg            # 原地生成 xxx_embedded.svg
"""
import argparse
import base64
import os
import re
import sys

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif"}


def embed(svg_path, out_path=None):
    svg_dir = os.path.dirname(os.path.abspath(svg_path))
    with open(svg_path, "r", encoding="utf-8") as f:
        svg = f.read()

    hits = []

    def repl(m):
        attr, href = m.group(1), m.group(2)
        if href.startswith("data:"):
            return m.group(0)
        src = href if os.path.isabs(href) else os.path.join(svg_dir, href)
        if not os.path.isfile(src):
            hits.append((href, "找不到文件，保持外链"))
            return m.group(0)
        ext = os.path.splitext(src)[1].lower()
        mime = MIME.get(ext)
        if not mime:
            hits.append((href, "不认识的图片格式，保持外链"))
            return m.group(0)
        with open(src, "rb") as fp:
            b64 = base64.b64encode(fp.read()).decode("ascii")
        hits.append((href, "已内嵌 %.1f MB" % (len(b64) / 1024 / 1024)))
        return '%s="data:%s;base64,%s"' % (attr, mime, b64)

    svg = re.sub(r'(xlink:href|href)="([^"]+)"', repl, svg)

    if out_path is None:
        base, ext = os.path.splitext(svg_path)
        out_path = base + "_embedded" + ext
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    return out_path, hits


def main():
    ap = argparse.ArgumentParser(description="把 SVG 引用的外部位图内嵌成 base64")
    ap.add_argument("svg")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    if not os.path.isfile(a.svg):
        print("找不到：%s" % a.svg, file=sys.stderr)
        sys.exit(1)
    out, hits = embed(a.svg, a.out)
    for href, status in hits:
        print("  %s → %s" % (href, status))
    if not hits:
        print("  （没有发现外部图片引用）")
    print("已写出：%s（%.1f MB）" % (out, os.path.getsize(out) / 1024 / 1024))


if __name__ == "__main__":
    main()
