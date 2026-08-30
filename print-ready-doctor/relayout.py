#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
relayout  ·  把贴纸版的排版从「靠模型运气」改成「靠代码保证」
================================================================

为什么需要它
------------
生成模型再怎么写 prompt，也只能"倾向于"把元素排开。实测踩过的坑：
目视看着分离得很开，print-ready-doctor 一测邻距只有 1.85mm ——
白边一外扩就粘连，切成 A5 时必报废。
排版这件事根本不该交给模型：它是纯几何问题，代码能 100% 保证。

做法
----
1. 复用 print_ready_doctor.segment_artwork() 把每一枚元素连同原始像素抠出来
   （抠的是"图案本体掩膜 + 0.3mm 外扩 + 羽化"，所以暖白手切边、投影都在里面）
2. 在一张干净的 A5 白底画布上按网格重排：
       cell_w = (板宽 - 2*margin - (cols-1)*gap) / cols
       cell_h = (板高 - 2*margin - (rows-1)*gap) / rows
   每枚等比缩放到刚好塞进自己的格子并居中。
   因为格子之间本来就隔着 gap，元素又不出格，
   **邻距 ≥ gap、边距 ≥ margin 在几何上被强制成立**，不是猜的。
3. 排完实测（距离变换）复核；万一没达标（理论上不会）就整体缩 3% 重排，
   最多 8 轮，还不行就非零退出 —— 绝不输出一张"看起来还行"的稿子。

不做的事
--------
- 不放大：元素只会等比缩小，不会超过原始物理尺寸（放大 = 糊）
- 不旋转、不裁剪、不重绘：像素原样搬运，风格 100% 保留

已知副作用（别装看不见）
------------------------
缩小是等比的，**细笔画会跟着一起变细**：缩到 72% 时 0.4mm 的笔画变 0.29mm，
可能跌破 0.3mm 印刷下限。所以重排解决的是"排版类失败"，不是全部失败，
重排后仍然必须跑 print_ready_doctor.py 复验。

用法
----
  python3 relayout.py FINAL.png --outdir out/
  python3 relayout.py FINAL.png --gap 8 --margin 8 --dpi 400
"""

import argparse
import json
import math
import os
import sys

import numpy as np
import cv2
from PIL import Image

# 复用 doctor 里已校准过的分割逻辑，不另写一套
from print_ready_doctor import segment_artwork, mm2px, disk, open_photo

Image.MAX_IMAGE_PIXELS = None

# 抠图时把掩膜外扩一点，把抗锯齿边缘 / 暖白边 / 淡投影一起带走。
# 实测（404dpi 的 FINAL.png）：掩膜外 0.3mm 已经是纸白 251，再多带就是白搬白。
GRAB_DILATE_MM = 0.30
GRAB_FEATHER_PX = 1.2

# n 枚元素 → (列, 行)。A5 竖版下 6 枚就是 2×3。
GRID_TABLE = {1: (1, 1), 2: (1, 2), 3: (1, 3), 4: (2, 2), 5: (2, 3), 6: (2, 3),
              7: (2, 4), 8: (2, 4), 9: (3, 3), 10: (3, 4), 11: (3, 4), 12: (3, 4)}


def pick_grid(n, sheet_w, sheet_h, cols=None):
    if cols:
        return int(cols), int(math.ceil(n / float(cols)))
    if n in GRID_TABLE:
        return GRID_TABLE[n]
    c = max(1, int(round(math.sqrt(n * sheet_w / float(sheet_h)))))
    return c, int(math.ceil(n / float(c)))


# ---------------------------------------------------------------- 抠图

def grab_elements(img, ppmm, min_area_mm2):
    """
    切出每一枚元素。返回 [{rgb, alpha, w_mm, h_mm, cx, cy}]，
    rgb 是原始像素（未做任何颜色处理），alpha 是 0~1 的软掩膜。
    """
    lab, stats, keep = segment_artwork(img, ppmm, min_area_mm2)
    all_mask = np.isin(lab, keep)
    out = []
    dil = disk(mm2px(GRAB_DILATE_MM, ppmm))
    for cid in keep:
        comp = (lab == cid).astype(np.uint8)
        grown = cv2.dilate(comp, dil)
        # 外扩不能把邻居的像素捎带进来
        grown[(all_mask) & (comp == 0)] = 0
        ys, xs = np.where(grown > 0)
        x0, x1 = xs.min(), xs.max() + 1
        y0, y1 = ys.min(), ys.max() + 1
        a = cv2.GaussianBlur(grown[y0:y1, x0:x1].astype(np.float32),
                             (0, 0), GRAB_FEATHER_PX)
        a = np.clip(a, 0.0, 1.0)
        out.append({
            "rgb": img[y0:y1, x0:x1].astype(np.float32),
            "alpha": a,
            "w_mm": (x1 - x0) / ppmm, "h_mm": (y1 - y0) / ppmm,
            "cx": float(xs.mean()), "cy": float(ys.mean()),
            "src_bbox_px": (int(x0), int(y0), int(x1 - x0), int(y1 - y0)),
        })
    return out


def order_reading(elems, cols):
    """按原图的阅读顺序（先上下、再左右）分配到格子，重排后观感不突兀。"""
    idx = sorted(range(len(elems)), key=lambda i: elems[i]["cy"])
    ordered = []
    for r in range(0, len(idx), cols):
        row = idx[r:r + cols]
        row.sort(key=lambda i: elems[i]["cx"])
        ordered += row
    return ordered


# ---------------------------------------------------------------- 合成

def paste(canvas, ink, el, x_px, y_px, tw, th):
    """
    以「白底上的墨量」D = (255-RGB)*alpha 做重采样再合成。
    直接缩 RGB 再乘 alpha 会在边缘出现灰边/白圈，这样不会。
    """
    d = (255.0 - el["rgb"]) * el["alpha"][:, :, None]
    interp = cv2.INTER_AREA if (tw < d.shape[1]) else cv2.INTER_CUBIC
    d = cv2.resize(d, (tw, th), interpolation=interp)
    a = cv2.resize(el["alpha"], (tw, th), interpolation=interp)
    d = np.clip(d, 0, 255)
    a = np.clip(a, 0, 1)
    roi = canvas[y_px:y_px + th, x_px:x_px + tw]
    canvas[y_px:y_px + th, x_px:x_px + tw] = np.clip(roi - d, 0, 255)
    ink[y_px:y_px + th, x_px:x_px + tw] = np.maximum(
        ink[y_px:y_px + th, x_px:x_px + tw], (a > 0.5).astype(np.uint8))


def measure(masks, W, H, ppmm):
    """
    实测邻距 / 边距。masks 是每枚元素在成品画布上的二值掩膜。
    邻距 = 每枚到「其它所有枚」的最小欧氏距离（和 doctor 的算法一致）。
    """
    union = np.zeros((H, W), np.uint8)
    for m in masks:
        union |= m
    gaps = []
    for i, m in enumerate(masks):
        others = union.copy()
        others[m > 0] = 0
        if others.sum() == 0:
            gaps.append(999.0)
            continue
        d = cv2.distanceTransform(1 - others, cv2.DIST_L2, 5)
        gaps.append(float(d[m > 0].min()) / ppmm)
    margins = []
    for m in masks:
        ys, xs = np.where(m > 0)
        margins.append(min(xs.min(), ys.min(), W - 1 - xs.max(), H - 1 - ys.max()) / ppmm)
    return gaps, margins


# ---------------------------------------------------------------- 主流程

def relayout(img, in_width_mm, sheet_w, sheet_h, dpi, gap, margin,
             min_area, cols=None, max_shrink_rounds=8, verbose=True):
    src_ppmm = img.shape[1] / float(in_width_mm)
    out_ppmm = dpi / 25.4
    W = int(round(sheet_w * out_ppmm))
    H = int(round(sheet_h * out_ppmm))

    elems = grab_elements(img, src_ppmm, min_area)
    n = len(elems)
    if n == 0:
        raise SystemExit("❌ 没分割出任何元素，检查输入是否为白底贴纸版")
    c, r = pick_grid(n, sheet_w, sheet_h, cols)
    if c * r < n:
        raise SystemExit("❌ %d 枚元素放不进 %d×%d 网格" % (n, c, r))

    cell_w = (sheet_w - 2 * margin - (c - 1) * gap) / float(c)
    cell_h = (sheet_h - 2 * margin - (r - 1) * gap) / float(r)
    if cell_w <= 5 or cell_h <= 5:
        raise SystemExit("❌ gap/margin 太大，格子只剩 %.1f×%.1fmm" % (cell_w, cell_h))

    order = order_reading(elems, c)
    if verbose:
        print("[重排] %d 枚 → %d列×%d行 | 格子 %.1f×%.1fmm | gap %.1fmm margin %.1fmm | "
              "画布 %dx%dpx @%ddpi" % (n, c, r, cell_w, cell_h, gap, margin, W, H, dpi))

    k = 1.0                      # 全局收缩系数，自检不过才会动它
    for attempt in range(max_shrink_rounds):
        canvas = np.full((H, W, 3), 255.0, np.float32)
        masks, placed = [], []
        for slot, ei in enumerate(order):
            el = elems[ei]
            gr, gc = divmod(slot, c)
            s = min(cell_w / el["w_mm"], cell_h / el["h_mm"], 1.0) * k
            tw_mm, th_mm = el["w_mm"] * s, el["h_mm"] * s
            tw = max(1, int(round(tw_mm * out_ppmm)))
            th = max(1, int(round(th_mm * out_ppmm)))
            cx_mm = margin + gc * (cell_w + gap) + cell_w / 2.0
            cy_mm = margin + gr * (cell_h + gap) + cell_h / 2.0
            x_px = int(round((cx_mm - tw_mm / 2.0) * out_ppmm))
            y_px = int(round((cy_mm - th_mm / 2.0) * out_ppmm))
            x_px = max(0, min(W - tw, x_px))
            y_px = max(0, min(H - th, y_px))
            ink = np.zeros((H, W), np.uint8)
            paste(canvas, ink, el, x_px, y_px, tw, th)
            masks.append(ink)
            placed.append({"slot": slot, "src_index": ei, "cell": [gr, gc],
                           "src_size_mm": [round(el["w_mm"], 1), round(el["h_mm"], 1)],
                           "final_size_mm": [round(tw_mm, 1), round(th_mm, 1)],
                           "scale_pct": round(100 * s, 1),
                           "pos_mm": [round(x_px / out_ppmm, 1), round(y_px / out_ppmm, 1)]})

        gaps, margins = measure(masks, W, H, out_ppmm)
        min_gap, min_margin = min(gaps), min(margins)
        ok = (min_gap >= gap - 0.05) and (min_margin >= margin - 0.05)
        if verbose:
            print("  自检 #%d（缩放系数 %.2f）：实测最小邻距 %.2fmm / 最小边距 %.2fmm → %s"
                  % (attempt + 1, k, min_gap, min_margin, "达标" if ok else "不达标，缩 3% 重排"))
        if ok:
            out = np.clip(canvas, 0, 255).astype(np.uint8)
            for i, p in enumerate(placed):
                p["neighbor_gap_mm"] = round(gaps[i], 2)
                p["edge_margin_mm"] = round(margins[i], 2)
            return out, {"n_elements": n, "cols": c, "rows": r,
                         "cell_mm": [round(cell_w, 1), round(cell_h, 1)],
                         "sheet_mm": [sheet_w, sheet_h], "dpi": dpi,
                         "canvas_px": [W, H], "gap_mm": gap, "margin_mm": margin,
                         "shrink_k": round(k, 3),
                         "min_gap_mm": round(min_gap, 2),
                         "min_margin_mm": round(min_margin, 2),
                         "min_element_long_mm": round(
                             min(max(e["final_size_mm"]) for e in placed), 1) if placed else None,
                         "elements": placed}
        k *= 0.97
    raise SystemExit("❌ 缩了 %d 轮仍不达标，请调小 --gap/--margin 或减少元素数" % max_shrink_rounds)


def verify_with_doctor(out_img, sheet_w, min_area, expect_n):
    """
    用 doctor 自己的分割再测一遍：邻距是不是真的达标、元素数有没有变。
    上面的实测用的是"外扩 0.3mm 后的掩膜"，偏保守；这里是工厂视角的真值。
    """
    ppmm = out_img.shape[1] / float(sheet_w)
    lab, stats, keep = segment_artwork(out_img, ppmm, min_area)
    if not keep:
        return {"n": 0, "min_gap_mm": None}
    all_mask = np.isin(lab, keep).astype(np.uint8)
    gaps = []
    for cid in keep:
        comp = (lab == cid).astype(np.uint8)
        others = ((all_mask > 0) & (comp == 0)).astype(np.uint8)
        if others.sum() == 0:
            continue
        d = cv2.distanceTransform(1 - others, cv2.DIST_L2, 5)
        gaps.append(float(d[comp.astype(bool)].min()) / ppmm)
    return {"n": len(keep), "min_gap_mm": round(min(gaps), 2) if gaps else None,
            "same_count": len(keep) == expect_n}


def write_report(path, src, meta, ver):
    L = ["贴纸版程序化重排报告  ·  %s" % os.path.basename(src),
         "=" * 62,
         "成品画布 : %.0f × %.0f mm  @ %d dpi  (%d × %d px)"
         % (meta["sheet_mm"][0], meta["sheet_mm"][1], meta["dpi"],
            meta["canvas_px"][0], meta["canvas_px"][1]),
         "网格     : %d 列 × %d 行，格子 %.1f × %.1f mm"
         % (meta["cols"], meta["rows"], meta["cell_mm"][0], meta["cell_mm"][1]),
         "约束     : 元素净距 ≥ %.1fmm，四边留白 ≥ %.1fmm，不放大（缩放上限 100%%）"
         % (meta["gap_mm"], meta["margin_mm"]),
         "全局收缩 : %.3f  （1.000 = 一次排版即达标，无需收缩）" % meta["shrink_k"],
         "",
         "逐枚元素",
         "-" * 62,
         "%-4s %-6s %-14s %-14s %-7s %-9s %-9s"
         % ("序", "格子", "原始尺寸mm", "最终尺寸mm", "缩放", "邻距mm", "边距mm")]
    for p in meta["elements"]:
        L.append("%-4s %-6s %-14s %-14s %-7s %-9s %-9s" % (
            "#%d" % (p["slot"] + 1), "r%dc%d" % (p["cell"][0] + 1, p["cell"][1] + 1),
            "%.1f×%.1f" % tuple(p["src_size_mm"]),
            "%.1f×%.1f" % tuple(p["final_size_mm"]),
            "%.0f%%" % p["scale_pct"],
            "%.2f" % p["neighbor_gap_mm"], "%.2f" % p["edge_margin_mm"]))
    L += ["", "实测结论", "-" * 62,
          "最小邻距（外扩 0.3mm 的保守掩膜）: %.2f mm  要求 ≥ %.1f mm  → %s"
          % (meta["min_gap_mm"], meta["gap_mm"],
             "✅" if meta["min_gap_mm"] >= meta["gap_mm"] - 0.05 else "❌"),
          "最小边距                        : %.2f mm  要求 ≥ %.1f mm  → %s"
          % (meta["min_margin_mm"], meta["margin_mm"],
             "✅" if meta["min_margin_mm"] >= meta["margin_mm"] - 0.05 else "❌")]
    if ver:
        L += ["",
              "doctor 分割复验（工厂视角真值）",
              "-" * 62,
              "重排后分割出 %d 枚（重排前 %d 枚）→ %s"
              % (ver["n"], meta["n_elements"], "一致 ✅" if ver.get("same_count") else "不一致 ⚠️"),
              "最小邻距 %s mm" % ver["min_gap_mm"]]
    L += ["", "说明：邻距/边距为距离变换实测值，不是估计值。",
          "格子间本身就隔着 gap，元素又不出格，因此 邻距 ≥ gap 由几何强制成立。", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description="贴纸版程序化重排：硬保证邻距与边距")
    ap.add_argument("image")
    ap.add_argument("--in-width", type=float, default=148.0,
                    help="输入图当作多宽 mm（用于换算原始物理尺寸，默认 A5 宽 148）")
    ap.add_argument("--sheet-width", type=float, default=148.0, help="输出画布宽 mm")
    ap.add_argument("--sheet-height", type=float, default=210.0, help="输出画布高 mm")
    ap.add_argument("--dpi", type=int, default=400, help="输出分辨率")
    ap.add_argument("--gap", type=float, default=8.0, help="元素之间的最小净距 mm")
    ap.add_argument("--margin", type=float, default=10.0, help="四边最小留白 mm")
    ap.add_argument("--min-element-mm", type=float, default=25.0,
                    help="每枚元素成品长边的最小值 mm。防的是元素在原图里占比过小、"
                         "成品小到撕不动。默认 25mm")
    ap.add_argument("--min-input-dpi", type=float, default=300.0,
                    help="输入生成图的有效 dpi 下限（= 图宽px / in_width_mm × 25.4）。"
                         "★ 这是防低分辨率模型的关键门禁：低分辨率图会被插值放大到目标画布，"
                         "物理尺寸看起来正常、doctor 也按画布报 400dpi，但实际细节已经糊了。"
                         "默认 300")
    ap.add_argument("--cols", type=int, default=0, help="强制列数，0=按元素数自适应")
    # 25mm² = 「视为有效元素的最小面积」。它和 make_memory_card.py 的
    # MIN_ELEM_AREA_MM2=120（「值得上卡的最小面积」）是两个不同用途的阈值，
    # 故意不统一：这里调高会漏数小元素 → 元素数≠刀线数被判粘连。详见那边注释。
    ap.add_argument("--min-area", type=float, default=25.0, help="忽略小于此面积的碎片 mm²")
    ap.add_argument("--outdir", default=".", help="输出目录")
    ap.add_argument("--name", default="relayout", help="输出文件名前缀")
    ap.add_argument("--no-verify", action="store_true", help="跳过 doctor 分割复验（省几秒）")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    img = np.array(open_photo(a.image).convert("RGB"))
    in_dpi = img.shape[1] / float(a.in_width) * 25.4
    print("[输入] %s  %dx%dpx  → 按 %.0fmm 宽换算，有效 %.0f dpi"
          % (os.path.basename(a.image), img.shape[1], img.shape[0], a.in_width, in_dpi))
    if in_dpi < a.min_input_dpi:
        print("\n❌ 输入图有效分辨率仅 %.0f dpi，低于 %.0f dpi 印刷线。" % (in_dpi, a.min_input_dpi))
        print("   ⚠️ 这一步必须在这里卡住：继续跑下去，元素会被插值放大到 %d dpi 的画布，"
              % a.dpi)
        print("   成品尺寸和 doctor 的 dpi 检查都会显示正常，但水粉纸纹和边缘细节已经糊了 —— ")
        print("   这是唯一一类「全部检查通过、实物却不能用」的失败。")
        print("   根因是生图模型输出分辨率不够，请换支持 4K 输出的模型（见 providers.py 说明）。")
        return 4

    out, meta = relayout(img, a.in_width, a.sheet_width, a.sheet_height, a.dpi,
                         a.gap, a.margin, a.min_area, a.cols or None)
    png = os.path.join(a.outdir, a.name + ".png")
    Image.fromarray(out).save(png)

    ver = None
    if not a.no_verify:
        ver = verify_with_doctor(out, a.sheet_width, a.min_area, meta["n_elements"])
        print("[复验] doctor 分割 %d 枚，最小邻距 %s mm" % (ver["n"], ver["min_gap_mm"]))
        meta["verify"] = ver

    write_report(os.path.join(a.outdir, a.name + "_report.txt"), a.image, meta, ver)
    with open(os.path.join(a.outdir, a.name + ".json"), "w", encoding="utf-8") as f:
        json.dump({"source": os.path.abspath(a.image), **meta}, f,
                  ensure_ascii=False, indent=2)
    print("[输出] %s" % png)
    print("[输出] %s" % os.path.join(a.outdir, a.name + "_report.txt"))

    # ── 分辨率兜底门禁 ────────────────────────────────────────────────
    # 元素只会被缩小、绝不放大，所以生成图分辨率不足的表现不是"dpi 不合格"，
    # 而是"每枚贴纸悄悄变小"。这里显式卡住，避免静默出小到没法用的成品。
    tiny = [e for e in meta["elements"] if max(e["final_size_mm"]) < a.min_element_mm]
    if tiny:
        print("\n❌ 有 %d 枚元素成品长边 < %.0fmm（最小 %.1fmm）" %
              (len(tiny), a.min_element_mm, meta["min_element_long_mm"]))
        for e in tiny:
            print("   格%s 长边仅 %.1fmm（缩放 %.0f%%）"
                  % (e["cell"], max(e["final_size_mm"]), e["scale_pct"]))
        print("   根因几乎总是生成图分辨率不够：元素不会被放大，只会变小。")
        print("   正确做法是换用支持 4K 输出的生图模型，而不是调低 --min-element-mm。")
        return 3

    print("[门禁] 最小元素长边 %.1fmm ≥ %.0fmm ✅"
          % (meta["min_element_long_mm"], a.min_element_mm))
    return 0


if __name__ == "__main__":
    sys.exit(main())
