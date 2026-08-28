#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数码模切专用导出：把 A5 整版拆成数码模切店能直接收的文件。

为什么单独写这个：
    传统印刷厂吃「整版图 + 专色刀线 SVG/AI」，但淘宝那种数码模切小店
    （Roland/Graphtec 割字机 + RIP 软件）通常是两种收件方式之一：
      方式A 打印切割一体：要 PDF（SVG 很多店打不开），刀线要矢量
      方式B 来图定制：只要每枚贴纸的「透明底 PNG」，刀线他们自己描
    这个脚本两种都产出，你拿去问店家要哪种就行。

用法：
    python3 export_for_digital_cut.py FINAL.png --outdir 导出目录
    python3 export_for_digital_cut.py FINAL.png --outdir 出 --cutline production/cutline.svg
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

A5_W_MM = 148.0
A5_H_MM = 210.0


def load_grab():
    """复用 relayout 的元素切割（带 alpha 软掩膜），避免重复实现。"""
    try:
        from relayout import grab_elements
        return grab_elements
    except Exception as e:
        print("无法从 relayout.py 导入 grab_elements：%s" % e, file=sys.stderr)
        sys.exit(1)


def export_singles(final_png, outdir, min_area_mm2, pad_mm):
    grab = load_grab()
    im = Image.open(final_png).convert("RGB")
    arr = np.array(im)
    ppmm = im.width / A5_W_MM

    elems = grab(arr, ppmm, min_area_mm2)
    if not elems:
        print("没有切出任何元素，检查输入是否为贴纸版成品图", file=sys.stderr)
        sys.exit(2)

    d = os.path.join(outdir, "单枚透明底PNG")
    os.makedirs(d, exist_ok=True)
    pad = int(round(pad_mm * ppmm))
    rows = []
    for i, el in enumerate(elems, 1):
        rgb, alpha = el["rgb"], el["alpha"]
        h, w = alpha.shape
        canvas = np.zeros((h + 2 * pad, w + 2 * pad, 4), np.uint8)
        canvas[pad:pad + h, pad:pad + w, :3] = rgb
        canvas[pad:pad + h, pad:pad + w, 3] = np.clip(alpha * 255, 0, 255).astype(np.uint8)
        out = os.path.join(d, "%02d.png" % i)
        img = Image.fromarray(canvas, "RGBA")
        # 写入 dpi，店家用 PS/AI 打开就能看到正确物理尺寸
        img.save(out, dpi=(ppmm * 25.4, ppmm * 25.4))
        w_mm = el["w_mm"] + 2 * pad_mm
        h_mm = el["h_mm"] + 2 * pad_mm
        rows.append((i, img.width, img.height, w_mm, h_mm))
        print("  %02d.png  %4dx%-4d px  = %5.1f x %5.1f mm"
              % (i, img.width, img.height, w_mm, h_mm))

    with open(os.path.join(d, "尺寸清单.txt"), "w", encoding="utf-8") as f:
        f.write("每枚贴纸的成品尺寸（已含四周留白 %.1fmm）\n" % pad_mm)
        f.write("务必按此尺寸印刷，请勿等比缩放或自动排版\n\n")
        f.write("文件      像素          成品尺寸(mm)\n")
        for i, pw, ph, wm, hm in rows:
            f.write("%02d.png    %4dx%-4d    %5.1f x %5.1f\n" % (i, pw, ph, wm, hm))
        f.write("\n共 %d 枚。整版为 A5 148x210mm。\n" % len(rows))
    return len(rows)


def to_pdf(svg_path, pdf_path):
    """SVG → PDF。

    坑：cairosvg 不会去解析 SVG 里 <image xlink:href="../FINAL.png"> 这种外部引用，
    直接转出来的 PDF 只有刀线、没有图，发给店家会印出空白。
    所以先把位图 base64 内嵌进去，再转。
    """
    import tempfile
    import cairosvg
    from embed_svg import embed
    tmp = os.path.join(tempfile.mkdtemp(prefix="svg2pdf_"), "embedded.svg")
    try:
        embed(svg_path, tmp)
        src = tmp
    except Exception:
        src = svg_path
    cairosvg.svg2pdf(url=src, write_to=pdf_path,
                     output_width=A5_W_MM * 72 / 25.4,
                     output_height=A5_H_MM * 72 / 25.4)
    return pdf_path


def png_to_pdf(png_path, pdf_path):
    im = Image.open(png_path).convert("RGB")
    im.save(pdf_path, "PDF", resolution=im.width / A5_W_MM * 25.4)
    return pdf_path


def main():
    ap = argparse.ArgumentParser(description="导出数码模切店能直接收的文件")
    ap.add_argument("final", help="A5 贴纸版成品 PNG")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--cutline", help="cutline.svg 路径，用于转 PDF；默认自动找同级 production/")
    ap.add_argument("--min-area", type=float, default=25.0, help="最小元素面积 mm²")
    ap.add_argument("--pad", type=float, default=1.0, help="单枚 PNG 四周留白 mm")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    print("【1】拆单枚透明底 PNG")
    n = export_singles(a.final, a.outdir, a.min_area, a.pad)

    print("【2】整版 PDF")
    pdf1 = png_to_pdf(a.final, os.path.join(a.outdir, "整版_印刷图_A5.pdf"))
    print("  " + os.path.basename(pdf1))

    cut = a.cutline
    if not cut:
        guess = os.path.join(os.path.dirname(os.path.abspath(a.final)),
                             "production", "cutline.svg")
        cut = guess if os.path.isfile(guess) else None
    if cut and os.path.isfile(cut):
        try:
            p = to_pdf(cut, os.path.join(a.outdir, "整版_图加刀线.pdf"))
            print("  " + os.path.basename(p))
            only = cut.replace("cutline.svg", "cutline_only.svg")
            if os.path.isfile(only):
                p2 = to_pdf(only, os.path.join(a.outdir, "整版_纯刀线.pdf"))
                print("  " + os.path.basename(p2))
        except Exception as e:
            print("  刀线转 PDF 失败（不影响单枚 PNG 使用）：%s" % e)
    else:
        print("  未找到 cutline.svg，跳过刀线 PDF")

    print("\n完成，共 %d 枚 → %s" % (n, a.outdir))


if __name__ == "__main__":
    main()
