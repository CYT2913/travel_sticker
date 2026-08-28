#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
impose_a3  ·  A3 四宫格拼版 + 裁切标记 + kiss cut 刀线
========================================================

生产条件（决定了这个脚本的每一个设计）
----------------------------------------
工厂只做 A3 不干胶数码印刷 + 模切，一张 A3 = 20 元，数量多也不便宜。
A3 = 297×420mm，正好放 4 张 A5（148.5×210mm）。
所以把 4 个不同客户的稿子拼到一张 A3 上，单客户成本直接除以 4。

一张 A3 上要走两种刀：
  · kiss cut（半切）：只切面材、不切底纸 → 每张 A5 是「一整版可撕贴纸」
  · through cut（全切）：沿中缝把 A3 切成 4 张 A5 → 分别寄给 4 个客户

两家工厂要的文件还不一样：一家要 SVG + 原始图片，一家只要图片。
所以必须同时出高分位图和矢量，而且两者**必须严格对齐**。

怎么保证对齐（这是本脚本唯一重要的技术决定）
---------------------------------------------
不先算刀线再拼图，而是**先把 A3 位图拼完，再直接在这张位图的像素上分割、
求 offset、描轮廓**。刀线坐标 = A3 位图像素坐标 ÷ ppmm，
中间没有任何一次坐标系变换，所以错位在原理上不可能发生。
SVG 用 mm 为单位、viewBox 与 A3 实际尺寸 1:1，工厂打开就是实际大小。

白边 offset 复用 print_ready_doctor.solve_offset()，不另写一套。

用法
----
  python3 impose_a3.py a.png b.png c.png d.png --outdir a3_out/
  python3 impose_a3.py a.png --outdir a3_out/ --order-labels ORD-001
  python3 impose_a3.py *.png --outdir a3_out/ --dpi 400 --bleed 3

输出
----
  A3_print.png               只给图案，无任何刀线标记（刀线不能印出来）
  A3_production.svg          分层矢量：Artwork / KissCut / ThroughCut / TrimMarks / Registration
  A3_production_embedded.svg 同上，但位图 base64 内嵌，防工厂丢图
  A3_preview.png             人看的核对图（图案+洋红半切+青色全切+标记+订单号）
  imposition_report.md       拼版报告
  厂家须知.txt                可直接复制发给工厂的说明
"""

import argparse
import base64
import os
import sys
from datetime import datetime

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

from print_ready_doctor import (segment_artwork, raw_artwork_mask, solve_offset,
                                mm2px, disk)

Image.MAX_IMAGE_PIXELS = None

A3_W, A3_H = 297.0, 420.0
A5_W, A5_H = 148.5, 210.0          # A3 的 1/4，注意宽是 148.5 不是 148
PT = 25.4 / 72.0                    # 1pt = 0.3528mm
STROKE_MM = 0.25 * PT               # 0.25pt 线宽，换算成 mm（SVG 单位就是 mm）

# 中文字体候选: Linux / macOS / Windows 全覆盖, 找不到则自动降级为纯英文标注
CJK_FONTS = [
    os.environ.get("CJK_FONT_PATH", ""),
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/source-han-sans/SourceHanSansSC-VF.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/SimHei.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]


def has_cjk_font():
    """是否找到可渲染中文的字体。找不到时图上标注自动改英文, 避免豆腐块。"""
    for p in CJK_FONTS:
        if p and os.path.isfile(p):
            return True
    return False


_EN = {
    "裁切线": "TRIM", "半切线": "KISS CUT", "全切线": "CUT",
    "刀线": "CUT LINE", "出血": "BLEED", "预览": "PREVIEW",
    "拼版": "IMPOSITION", "格": "CELL", "毫米": "mm",
    "厂家须知": "PRINT SPEC", "不印刷": "DO NOT PRINT",
}


def _txt(s):
    """无 CJK 字体时把标注降级为英文/ASCII, 避免渲染成方块。"""
    s = str(s)
    if has_cjk_font():
        return s
    for k, v in _EN.items():
        s = s.replace(k, v)
    out = "".join(c if ord(c) < 128 else "" for c in s).strip()
    return out or "-"


def load_font(size):
    for p in CJK_FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ---------------------------------------------------------------- 稿件放置

def artwork_bbox_mm(img, ppmm, min_area):
    """整张稿子上『所有元素』的外接框（mm）。用来判断离 A5 裁切线够不够远。"""
    lab, stats, keep = segment_artwork(img, ppmm, min_area)
    if not keep:
        return None, 0
    m = np.isin(lab, keep)
    ys, xs = np.where(m)
    return (xs.min() / ppmm, ys.min() / ppmm,
            (xs.max() + 1) / ppmm, (ys.max() + 1) / ppmm), len(keep)


def plan_placement(img, in_width_mm, min_safe_mm, min_area):
    """
    决定这张稿子在 148.5×210 格子里的缩放系数。
      s_fit  : 等比塞进格子
      s_safe : 保证任何贴纸离 A5 裁切线 ≥ min_safe_mm（不满足就自动内缩）
    """
    ppmm = img.shape[1] / float(in_width_mm)
    w_mm, h_mm = in_width_mm, img.shape[0] / ppmm
    s_fit = min(A5_W / w_mm, A5_H / h_mm)
    bbox, n_elem = artwork_bbox_mm(img, ppmm, min_area)
    s_safe = float("inf")
    if bbox:
        x0, y0, x1, y1 = bbox
        for d, half in ((w_mm / 2 - x0, A5_W / 2), (x1 - w_mm / 2, A5_W / 2),
                        (h_mm / 2 - y0, A5_H / 2), (y1 - h_mm / 2, A5_H / 2)):
            if d > 1e-6:
                s_safe = min(s_safe, (half - min_safe_mm) / d)
    # 不放大：源稿比格子小就原样放，只补白边。放大 = 有效 dpi 下降 = 糊。
    s = min(s_fit, s_safe, 1.0)
    return {"ppmm": ppmm, "w_mm": w_mm, "h_mm": h_mm, "s_fit": s_fit,
            "s_safe": s_safe, "s": s, "bbox_mm": bbox, "n_elem": n_elem,
            "shrunk": s < min(s_fit, 1.0) - 1e-6,
            "orig_margin_mm": (min(bbox[0], bbox[1], w_mm - bbox[2], h_mm - bbox[3])
                               if bbox else None)}


# ---------------------------------------------------------------- 刀线

def analyze_cell(cell_rgb, ppmm, args):
    """
    在【已经拼好的 A3 位图】的某个 A5 区域上，一次算完这一格需要的全部信息：
    kiss cut 轮廓（A5 局部像素坐标）、元素数、最小邻距、每枚 offset、
    以及画面离该 A5 裁切线的最近距离。

    性能说明：solve_offset 内部要做距离变换 + 骨架化，直接喂 2339×3307 的整格
    掩膜的话一张 A3 要跑 2 分多钟。这里改成只喂「元素外接框 + 足够 padding」的
    子图 —— padding 取 offset_max + corner_radius + 1mm，形态学不会跨过 padding
    影响结果，所以结果与整格计算完全一致，只是快 5~8 倍。
    """
    lab, stats, keep = segment_artwork(cell_rgb, ppmm, args.min_area)
    if not keep:
        return {"cnts": [], "n_elem": 0, "min_gap": None, "offsets": [], "edge_mm": None}
    raw = raw_artwork_mask(cell_rgb, ppmm, denoise=True)
    all_mask = np.isin(lab, keep).astype(np.uint8)
    H, W = cell_rgb.shape[:2]
    cut = np.zeros((H, W), np.uint8)
    gaps, offs = [], []
    pad = mm2px(args.offset_max + args.corner_radius + 1.0, ppmm)
    for cid in keep:
        comp_full = (lab == cid).astype(np.uint8)
        others = ((all_mask > 0) & (comp_full == 0)).astype(np.uint8)
        gap = (cv2.distanceTransform(1 - others, cv2.DIST_L2, 5)[comp_full.astype(bool)].min()
               / ppmm) if others.sum() else 999.0
        r = stats[cid]
        x0 = max(0, r[cv2.CC_STAT_LEFT] - pad); y0 = max(0, r[cv2.CC_STAT_TOP] - pad)
        x1 = min(W, r[cv2.CC_STAT_LEFT] + r[cv2.CC_STAT_WIDTH] + pad)
        y1 = min(H, r[cv2.CC_STAT_TOP] + r[cv2.CC_STAT_HEIGHT] + pad)
        comp = comp_full[y0:y1, x0:x1]
        raw_comp = ((raw[y0:y1, x0:x1] > 0) & (comp > 0)).astype(np.uint8)
        off, lo, hi, cut_w, bridged, feasible, path = solve_offset(
            comp, raw_comp, ppmm, args.safe_width, args.offset_min,
            args.offset_target, args.offset_max, gap, args.corner_radius)
        cut[y0:y1, x0:x1] = np.maximum(cut[y0:y1, x0:x1], path)
        gaps.append(float(gap))
        offs.append(round(off, 2))
    cnts, _ = cv2.findContours(cut, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    eps = max(1.0, 0.15 * ppmm)
    out = []
    for c in cnts:
        if cv2.contourArea(c) < (2 * ppmm) ** 2:      # 小于 2×2mm 的碎轮廓不出刀
            continue
        a = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
        if len(a) >= 3:
            out.append(a)
    ys, xs = np.where(all_mask > 0)
    edge_mm = round(min(xs.min(), ys.min(), W - 1 - xs.max(), H - 1 - ys.max()) / ppmm, 2)
    return {"cnts": out, "n_elem": len(keep), "min_gap": (min(gaps) if gaps else None),
            "offsets": offs, "edge_mm": edge_mm}


# ---------------------------------------------------------------- SVG

def path_d(pts_mm):
    return ("M " + " L ".join("%.3f,%.3f" % (x, y) for x, y in pts_mm) + " Z")


def build_marks(cells, mark_len, mark_off):
    """
    TrimMarks：每个 A5 格子四角的 L 形角标。
    A3 上 4 个 A5 是紧密相邻的（没有中缝），传统「向外伸出」的角标会伸进
    邻格的画面，所以这里做成**向内偏移 mark_off(3mm) 的角括号**：
    角标上的点离裁切线恒为 3mm，而拼版已强制任何贴纸离裁切线 ≥5mm，
    所以角标永远不会压到画面。裁切以 ThroughCut 图层为准，角标只是对位参考。
    """
    segs = []
    for (x0, y0) in cells:
        x1, y1 = x0 + A5_W, y0 + A5_H
        for cx, sx in ((x0, 1), (x1, -1)):
            for cy, sy in ((y0, 1), (y1, -1)):
                ax, ay = cx + sx * mark_off, cy + sy * mark_off
                segs.append(((ax, ay), (ax + sx * mark_len, ay)))
                segs.append(((ax, ay), (ax, ay + sy * mark_len)))
    # A3 外边缘上标出中缝位置，方便切纸机直接对刀
    tick = 5.0
    segs += [((A3_W / 2, 0), (A3_W / 2, tick)), ((A3_W / 2, A3_H - tick), (A3_W / 2, A3_H)),
             ((0, A3_H / 2), (tick, A3_H / 2)), ((A3_W - tick, A3_H / 2), (A3_W, A3_H / 2))]
    return segs


def build_svg(kiss_paths, cells, args, image_href, bleed):
    vb = "%.3f %.3f %.3f %.3f" % (-bleed, -bleed, A3_W + 2 * bleed, A3_H + 2 * bleed)
    sw = "%.4f" % STROKE_MM
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"',
         '     width="%.2fmm" height="%.2fmm" viewBox="%s">'
         % (A3_W + 2 * bleed, A3_H + 2 * bleed, vb),
         '  <!-- impose_a3 | 单位 mm，viewBox 与实际尺寸 1:1 -->',
         '  <!-- KissCut  : 半切（只切面材，不要切透底纸），100% Magenta, 0.25pt -->',
         '  <!-- ThroughCut: 全切（把 A3 切成 4 张 A5），100% Cyan, 0.25pt -->',
         '  <!-- 只切 KissCut 与 ThroughCut 两个图层，其它图层不要切 -->',
         '  <g id="Artwork">',
         '    <image xlink:href="%s" x="%.3f" y="%.3f" width="%.3f" height="%.3f"'
         ' preserveAspectRatio="none"/>' % (image_href, -bleed, -bleed,
                                            A3_W + 2 * bleed, A3_H + 2 * bleed),
         '  </g>',
         '  <g id="KissCut" fill="none" stroke="#FF00FF" stroke-width="%s"'
         ' stroke-linejoin="round" data-spot="CutContour">' % sw]
    for p in kiss_paths:
        L.append('    <path d="%s"/>' % path_d(p))
    L += ['  </g>',
          '  <g id="ThroughCut" fill="none" stroke="#00FFFF" stroke-width="%s"'
          ' data-spot="ThroughCut">' % sw,
          '    <rect x="0" y="0" width="%.3f" height="%.3f"/>' % (A3_W, A3_H),
          '    <line x1="%.3f" y1="0" x2="%.3f" y2="%.3f"/>'
          % (A3_W / 2, A3_W / 2, A3_H),
          '    <line x1="0" y1="%.3f" x2="%.3f" y2="%.3f"/>'
          % (A3_H / 2, A3_W, A3_H / 2),
          '  </g>',
          '  <g id="TrimMarks" fill="none" stroke="#000000" stroke-width="%s">' % sw]
    for (a, b) in build_marks(cells, args.mark_len, args.mark_offset):
        L.append('    <line x1="%.3f" y1="%.3f" x2="%.3f" y2="%.3f"/>'
                 % (a[0], a[1], b[0], b[1]))
    L += ['  </g>',
          '  <g id="Registration" fill="none" stroke="#000000" stroke-width="%s">' % sw]
    for (rx, ry) in [(A5_W / 2, 4.5), (A5_W + A5_W / 2, 4.5),
                     (A5_W / 2, A3_H - 4.5), (A5_W + A5_W / 2, A3_H - 4.5)]:
        L += ['    <circle cx="%.3f" cy="%.3f" r="1.5"/>' % (rx, ry),
              '    <line x1="%.3f" y1="%.3f" x2="%.3f" y2="%.3f"/>'
              % (rx - 2.5, ry, rx + 2.5, ry),
              '    <line x1="%.3f" y1="%.3f" x2="%.3f" y2="%.3f"/>'
              % (rx, ry - 2.5, rx, ry + 2.5)]
    L += ['  </g>', '</svg>', '']
    return "\n".join(L)


# ---------------------------------------------------------------- 预览

def build_preview(a3_rgb, kiss_paths, cells, slots, args, out_path):
    """人看的核对图。图例画在 A3 画面之外的附加条上，不遮挡任何内容。"""
    k = args.preview_dpi / 25.4          # mm → preview px
    pw, ph = int(round(A3_W * k)), int(round(A3_H * k))
    strip = int(round(27 * k))
    im = Image.new("RGB", (pw, ph + strip), (255, 255, 255))
    im.paste(Image.fromarray(a3_rgb).resize((pw, ph), Image.LANCZOS), (0, 0))
    d = ImageDraw.Draw(im)

    for p in kiss_paths:
        pts = [(x * k, y * k) for x, y in p]
        d.line(pts + [pts[0]], fill=(255, 0, 255), width=max(1, int(0.3 * k)))
    for (x0, y0) in cells:
        d.rectangle([x0 * k, y0 * k, (x0 + A5_W) * k, (y0 + A5_H) * k],
                    outline=(0, 190, 220), width=max(1, int(0.4 * k)))
        # 离裁切线 min-safe 的安全线，画成灰色短划线
        ms = args.min_safe
        for (ax, ay, bx, by) in [(x0 + ms, y0 + ms, x0 + A5_W - ms, y0 + ms),
                                 (x0 + ms, y0 + A5_H - ms, x0 + A5_W - ms, y0 + A5_H - ms),
                                 (x0 + ms, y0 + ms, x0 + ms, y0 + A5_H - ms),
                                 (x0 + A5_W - ms, y0 + ms, x0 + A5_W - ms, y0 + A5_H - ms)]:
            n = int(max(abs(bx - ax), abs(by - ay)) / 6.0)
            for t in range(n):
                u0, u1 = t / float(n), (t + 0.5) / float(n)
                d.line([((ax + (bx - ax) * u0) * k, (ay + (by - ay) * u0) * k),
                        ((ax + (bx - ax) * u1) * k, (ay + (by - ay) * u1) * k)],
                       fill=(165, 165, 165), width=1)
    for (a, b) in build_marks(cells, args.mark_len, args.mark_offset):
        d.line([(a[0] * k, a[1] * k), (b[0] * k, b[1] * k)],
               fill=(0, 0, 0), width=max(1, int(0.35 * k)))

    font = load_font(max(11, int(3.6 * k)))
    small = load_font(max(10, int(3.0 * k)))
    # 格子内没有任何空白能安全放长文字（四边留白只有 8mm，中缝只有 8mm），
    # 所以格内只放一个角标数字，明细统一列在画面外的附加条上。
    for i, (x0, y0) in enumerate(cells):
        # 位置选在「上边留白带、既不压角标也不压套准标记」的空区
        cx, cy = (x0 + A5_W / 2 + 20) * k, (y0 + 5.5) * k
        r = 3.4 * k
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 190, 220))
        bb = d.textbbox((0, 0), str(i + 1), font=font)
        d.text((cx - (bb[2] - bb[0]) / 2, cy - (bb[3] - bb[1]) / 2 - bb[1]),
               str(i + 1), font=font, fill=(255, 255, 255))
    lines = [("洋红 = KissCut 半切　│　青色 = ThroughCut 全切（切成 4 张 A5）　│　"
              "黑色 = TrimMarks 角标　│　灰虚线 = 离 A5 裁切线 %.0fmm 安全线" % args.min_safe)]
    for i, s in enumerate(slots):
        if s is None:
            lines.append("格%d：（空位，无稿件；该格裁切标记仍已画出）" % (i + 1))
        else:
            lines.append("格%d：%s　%s　%d 枚元素 · %d 条半切轮廓 · 最小邻距 %.2fmm · "
                         "离 A5 裁切线 %.2fmm" %
                         (i + 1, s["label"], os.path.basename(s["file"]), s["n_elem"],
                          s["n_kiss"], s["min_gap"] or 0, s["edge_mm"] or 0))
    lines.append("以上线条与文字均为核对用，A3_print.png 里没有它们，不会被印出来。")
    for j, t in enumerate(lines):
        col = (140, 60, 60) if j == len(lines) - 1 else (50, 50, 50)
        d.text((int(3 * k), ph + int((1.2 + j * 3.6) * k)), _txt(t), font=small, fill=col)
    im.save(out_path)


# ---------------------------------------------------------------- 厂家须知

FACTORY_NOTE = """【定制手账贴纸 · A3 不干胶印刷 + 模切 下单说明】

下单日期：{date}
文件清单：
  · A3_print.png              印刷用图（{dpi} dpi，297×420mm 满版，只有图案，无任何线条标记）
  · A3_production.svg         生产用矢量（分图层：Artwork / KissCut / ThroughCut / TrimMarks / Registration）
  · A3_production_embedded.svg  同上，图片已内嵌进 SVG，防止转发时丢图（二选一即可）

一、成品要求
  1. A3（297×420mm）不干胶数码印刷，1 张。
  2. 印刷后先做 kiss cut（半切）：**只切面材，不要切透底纸**，底纸必须完整。
  3. 再沿全切线把整张 A3 切成 4 张 A5（148.5×210mm），4 张分别包装。
  4. 每张 A5 是一整版可撕贴纸，客户自己一枚一枚撕下来用。

二、刀线怎么走（重要）
  · SVG 里的 `KissCut` 图层 = 半切线（洋红 100% Magenta，0.25pt）→ 半切。
  · SVG 里的 `ThroughCut` 图层 = 全切线（青色 100% Cyan，0.25pt）→ 切成 4 张 A5。
    其中外框是 A3 幅面边界，若用定尺 A3 材料可不切外框，只切中间十字即可。
  · **只切这两个图层，其余图层（Artwork / TrimMarks / Registration）不要切、不要印。**
  · TrimMarks 是角标，只作对位参考，裁切请以 ThroughCut 的矢量线为准。

三、必须避免的三件事（都是我们之前踩过的坑）
  1. **刀线中已包含 1.5mm 白边，请勿二次外扩、勿重新描轮廓。**
     再外扩一次会让相邻贴纸的白边连成一片，整版撕不开，直接报废。
  2. **画面里的暖白/米白色块是设计的一部分，不是留白，请勿去除、请勿"优化"成纯白。**
     那是手工剪纸的纸边效果，去掉整个风格就没了。
  3. 请勿自行套用锐化、自动对比度、自动色彩增强。

四、材质与表面
  · 不干胶面材请用**哑光/亚光**（光面会让水粉纸质感变成塑料感）。
  · **不覆光膜**。如工艺上必须覆膜，请用**可书写哑膜**（客户要在手账上写字）。

五、颜色
  · 文件为 RGB。请按贵方设备的 ICC 特性文件转 CMYK，**不要用软件默认转换**
    （默认转换下朱红会偏橙、暖白边会发灰）。
  · 如首次合作，希望先出一张数码样确认颜色，确认后再批量。

六、其他
  · 有效分辨率 {dpi} dpi，已满足印刷要求，请勿再做插值放大。
  · 四周为纯白留白（≥{margin:.1f}mm），等同出血；任何贴纸距 A5 裁切线 ≥{minsafe:.0f}mm，
    正常裁切公差不会切到图案。
  · 如任何一项无法满足，请先联系我确认，不要自行调整文件。
"""


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="A3 四宫格拼版 + 裁切标记 + kiss cut 刀线")
    ap.add_argument("sheets", nargs="+", help="1~4 张 A5 贴纸版 PNG（建议已经过 relayout）")
    ap.add_argument("--outdir", default="a3_out")
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--in-width", type=float, default=148.0, help="输入稿当作多宽 mm")
    ap.add_argument("--order-labels", default="", help="逗号分隔的订单号，按输入顺序对应")
    ap.add_argument("--bleed", type=float, default=0.0,
                    help="额外出血 mm（默认 0：A3 定尺印刷，四周白底留白本身就是出血）")
    ap.add_argument("--min-safe", type=float, default=5.0,
                    help="任何贴纸离 A5 裁切线的最小距离 mm，不足自动内缩")
    ap.add_argument("--preview-dpi", type=int, default=120)
    # 下面几项与 print_ready_doctor 同名同默认值，直接透传给 solve_offset
    ap.add_argument("--safe-width", type=float, default=1.5)
    ap.add_argument("--offset-min", type=float, default=0.8)
    ap.add_argument("--offset-target", type=float, default=1.5)
    ap.add_argument("--offset-max", type=float, default=2.5)
    ap.add_argument("--corner-radius", type=float, default=1.0)
    ap.add_argument("--min-area", type=float, default=25.0)
    ap.add_argument("--mark-len", type=float, default=5.0, help="裁切角标线长 mm")
    ap.add_argument("--mark-offset", type=float, default=3.0, help="角标距裁切线 mm")
    ap.add_argument("--no-embedded", action="store_true", help="不生成 base64 内嵌版 SVG")
    args = ap.parse_args()

    if len(args.sheets) > 4:
        raise SystemExit("❌ 一张 A3 只放得下 4 张 A5，收到 %d 张" % len(args.sheets))
    os.makedirs(args.outdir, exist_ok=True)
    ppmm = args.dpi / 25.4
    labels = [s.strip() for s in args.order_labels.split(",")] if args.order_labels else []

    Wpx = int(round((A3_W + 2 * args.bleed) * ppmm))
    Hpx = int(round((A3_H + 2 * args.bleed) * ppmm))
    off_px = int(round(args.bleed * ppmm))
    a3 = np.full((Hpx, Wpx, 3), 255, np.uint8)
    cells = [(0.0, 0.0), (A5_W, 0.0), (0.0, A5_H), (A5_W, A5_H)]

    print("[拼版] A3 %.0f×%.0fmm @%ddpi → %d×%dpx（含出血 %.1fmm）"
          % (A3_W, A3_H, args.dpi, Wpx, Hpx, args.bleed))

    slots = [None, None, None, None]
    for i, path in enumerate(args.sheets):
        img = np.array(Image.open(path).convert("RGB"))
        pl = plan_placement(img, args.in_width, args.min_safe, args.min_area)
        tw = max(1, int(round(pl["w_mm"] * pl["s"] * ppmm)))
        th = max(1, int(round(pl["h_mm"] * pl["s"] * ppmm)))
        interp = cv2.INTER_AREA if tw < img.shape[1] else cv2.INTER_CUBIC
        # 在「白底墨量」域重采样，避免边缘出灰边
        d = cv2.resize(255.0 - img.astype(np.float32), (tw, th), interpolation=interp)
        small = np.clip(255.0 - d, 0, 255).astype(np.uint8)
        cx0, cy0 = cells[i]
        x = off_px + int(round((cx0 + (A5_W - pl["w_mm"] * pl["s"]) / 2) * ppmm))
        y = off_px + int(round((cy0 + (A5_H - pl["h_mm"] * pl["s"]) / 2) * ppmm))
        a3[y:y + th, x:x + tw] = small
        label = labels[i] if i < len(labels) else os.path.splitext(os.path.basename(path))[0]
        slots[i] = {"file": path, "label": label, "plan": pl,
                    "placed_mm": [round(pl["w_mm"] * pl["s"], 1), round(pl["h_mm"] * pl["s"], 1)]}
        print("  格%d ← %-28s 缩放 %.3f%s  原稿 %.0f×%.0fmm → %.1f×%.1fmm"
              % (i + 1, os.path.basename(path), pl["s"],
                 "（🟠 触发 %.0fmm 安全内缩）" % args.min_safe if pl["shrunk"] else "",
                 pl["w_mm"], pl["h_mm"], *slots[i]["placed_mm"]))

    Image.fromarray(a3).save(os.path.join(args.outdir, "A3_print.png"), dpi=(args.dpi, args.dpi))

    # ---- 在拼好的 A3 位图上直接算刀线（保证与位图严格对齐）
    kiss_all, total = [], 0
    for i, s in enumerate(slots):
        if s is None:
            continue
        cx0, cy0 = cells[i]
        x0 = off_px + int(round(cx0 * ppmm)); x1 = off_px + int(round((cx0 + A5_W) * ppmm))
        y0 = off_px + int(round(cy0 * ppmm)); y1 = off_px + int(round((cy0 + A5_H) * ppmm))
        a = analyze_cell(a3[y0:y1, x0:x1], ppmm, args)
        for c in a["cnts"]:
            kiss_all.append([((x0 + px - off_px) / ppmm, (y0 + py - off_px) / ppmm)
                             for px, py in c])
        s.update({"n_elem": a["n_elem"], "min_gap": a["min_gap"],
                  "n_kiss": len(a["cnts"]), "offsets": a["offsets"],
                  "edge_mm": a["edge_mm"]})
        total += len(a["cnts"])
        flag = "✅" if a["n_elem"] == len(a["cnts"]) else "🔴 元素数≠刀线数，疑似粘连"
        print("  格%d 刀线：%d 枚元素 → %d 条半切轮廓  最小邻距 %.2fmm  offset %s  %s"
              % (i + 1, a["n_elem"], len(a["cnts"]), a["min_gap"] or 0,
                 "/".join("%.2f" % o for o in a["offsets"]), flag))
        print("       画面离 A5 裁切线最近 %.2fmm（要求 ≥%.0fmm）%s"
              % (s["edge_mm"] or 0, args.min_safe,
                 "✅" if (s["edge_mm"] or 0) >= args.min_safe - 0.05 else "🔴 不合格"))

    # ---- SVG
    svg = build_svg(kiss_all, cells, args, "A3_print.png", args.bleed)
    with open(os.path.join(args.outdir, "A3_production.svg"), "w") as f:
        f.write(svg)
    if not args.no_embedded:
        with open(os.path.join(args.outdir, "A3_print.png"), "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        with open(os.path.join(args.outdir, "A3_production_embedded.svg"), "w") as f:
            f.write(build_svg(kiss_all, cells, args, "data:image/png;base64," + b64, args.bleed))

    # ---- 预览
    build_preview(a3[off_px:off_px + int(round(A3_H * ppmm)),
                     off_px:off_px + int(round(A3_W * ppmm))],
                  kiss_all, cells, slots, args,
                  os.path.join(args.outdir, "A3_preview.png"))

    # ---- 厂家须知
    margins = [s["edge_mm"] for s in slots if s and s["edge_mm"] is not None]
    note = FACTORY_NOTE.format(date=datetime.now().strftime("%Y-%m-%d"), dpi=args.dpi,
                               margin=min(margins) if margins else args.min_safe,
                               minsafe=args.min_safe)
    with open(os.path.join(args.outdir, "厂家须知.txt"), "w") as f:
        f.write(note)

    # ---- 报告
    ok = all((s["n_elem"] == s["n_kiss"]) and (s["edge_mm"] or 0) >= args.min_safe - 0.05
             for s in slots if s)
    L = ["# A3 拼版报告", "",
         "- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
         "- 版面：A3 %.0f × %.0f mm @ **%d dpi**（%d × %d px），额外出血 %.1f mm"
         % (A3_W, A3_H, args.dpi, Wpx, Hpx, args.bleed),
         "- 排布：2 × 2，每格 A5 %.1f × %.0f mm" % (A5_W, A5_H),
         "- kiss cut 轮廓总数：**%d** 条" % total,
         "- 白边 offset：目标 %.1fmm，范围 %.1f~%.1fmm（复用 print_ready_doctor.solve_offset）"
         % (args.offset_target, args.offset_min, args.offset_max),
         "- 出血：A3 为定尺印刷，画面四周本身是纯白留白（本次实测 ≥%.1fmm），等同自带出血；"
         "若工厂明确要求实体出血边，用 `--bleed 3` 重跑即可（SVG viewBox 会同步扩展，坐标不变）。"
         % (min([s["edge_mm"] for s in slots if s and s["edge_mm"]] or [0])),
         "- 总体判定：%s" % ("✅ 可送厂" if ok else "🔴 有问题，先看下面标红项"),
         "", "## 逐格明细", "",
         "| 格 | 订单号 | 源文件 | 元素数 | 半切轮廓 | 最小邻距 mm | 离 A5 裁切线 mm | 缩放 | 安全内缩 |",
         "|---|---|---|---:|---:|---:|---:|---:|---|"]
    for i, s in enumerate(slots):
        if s is None:
            L.append("| %d | — | *（空位，仍已画出该格裁切标记）* | — | — | — | — | — | — |" % (i + 1))
            continue
        L.append("| %d | %s | `%s` | %d | %d | %.2f | %.2f | %.3f | %s |"
                 % (i + 1, s["label"], os.path.basename(s["file"]), s["n_elem"], s["n_kiss"],
                    s["min_gap"] or 0, s["edge_mm"] or 0, s["plan"]["s"],
                    "🟠 是（原稿离裁切线仅 %.1fmm）" % (s["plan"]["orig_margin_mm"] or 0)
                    if s["plan"]["shrunk"] else "否"))
    L += ["", "## 输出文件", "",
          "| 文件 | 给谁 | 说明 |", "|---|---|---|",
          "| `A3_print.png` | 只要图片的工厂 | 297×420mm 满版图案，**不含任何刀线和标记** |",
          "| `A3_production.svg` | 要 SVG 的工厂 | 分层矢量，图片按相对路径引用，需与 PNG 放同一目录 |",
          "| `A3_production_embedded.svg` | 同上（防丢图） | 位图已 base64 内嵌，单文件即可 |",
          "| `A3_preview.png` | 自己核对 | 图案 + 洋红半切 + 青色全切 + 标记 + 订单号 |",
          "| `厂家须知.txt` | 直接发工厂 | 下单说明，可整段复制 |",
          "", "## 图层约定", "",
          "| 图层 | 内容 | 工厂动作 |", "|---|---|---|",
          "| `Artwork` | A3_print.png | 印刷 |",
          "| `KissCut` | 每枚贴纸半切轮廓，100% Magenta / 0.25pt | **半切**，只切面材 |",
          "| `ThroughCut` | A3 外框 + 中缝十字，100% Cyan / 0.25pt | **全切**成 4 张 A5 |",
          "| `TrimMarks` | 各 A5 角上 L 形角标（线长 %.0fmm，距裁切线 %.0fmm）| 只作对位参考，不切 |"
          % (args.mark_len, args.mark_offset),
          "| `Registration` | 套准标记 | 不切 |",
          "", "## 对齐是怎么保证的", "",
          "刀线不是先算好再拼版的，而是**先把 A3 位图拼完，再直接在这张位图的像素上**",
          "分割、求 offset、描轮廓，坐标 = A3 像素 ÷ %.4f px/mm。" % ppmm,
          "中间没有任何坐标系变换，所以位图和矢量不可能错位。",
          "可在 `A3_preview.png` 上目视复核：洋红线应恰好套在每枚贴纸外一圈。",
          "", "## 给工厂的下单说明（全文见 厂家须知.txt）", "", "```", note.strip(), "```", ""]
    with open(os.path.join(args.outdir, "imposition_report.md"), "w") as f:
        f.write("\n".join(L))

    files = ["A3_print.png", "A3_production.svg"] + \
            ([] if args.no_embedded else ["A3_production_embedded.svg"]) + \
            ["A3_preview.png", "imposition_report.md", "厂家须知.txt"]
    print("[输出] %s/  （%s）" % (args.outdir, " / ".join(files)))
    print("[结论] %s" % ("✅ 可送厂" if ok else "🔴 有问题，见 imposition_report.md"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
