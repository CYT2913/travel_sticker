#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
print-ready-doctor  ·  贴纸版模切体检 / 修复 / 生产文件导出
===========================================================

为什么需要它
------------
AI 生成的贴纸版，纤细结构（麦克风支架、鼓腿、手指缝）和元素间距问题，
在屏幕上完全看不出来，但送到工厂模切时会变成三种事故：

  A 断裂   面材太窄，刀一过就断
  B 粘连   两枚贴纸挨太近，白边一外扩就连成一片 → 撕不开
  C 糊掉   白边把手指缝、鼓腿间隙桥接掉 → 细节丢失，变成"连指手套"

这三类都属于「设计文件问题」——工厂不赔，而且一定延期。
唯一的解法是在生成阶段扫出来并修掉。

核心思路
--------
模切走的不是图案轮廓，而是图案外扩一圈白边后的『刀线』。
所以修复动作不是改画面，而是**为每枚元素单独求解最优 offset**：

  offset 太小 → 纤细处面材不够 → 断（A）
  offset 太大 → 吃掉邻居间距 → 粘连（B）；桥接内部缝隙 → 糊掉（C）

本工具对每枚元素在可行区间内搜索，找到同时满足 A/B/C 的最小 offset。
若区间为空，说明排版本身有问题，会明确报出来要求调整间距。

用法
----
  python3 print_ready_doctor.py <贴纸版图片> --sheet-width 148 --outdir out/
  python3 print_ready_doctor.py <图片> --report-only        # 只体检

输出
----
  report.md / report.json    体检报告（含风险排名 → 决定备损备哪枚）
  preview_risk.png           风险标注预览
  cutline.svg                生产文件：原图 + CutContour 专色刀线
  cutline_only.svg           纯刀版
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict, field

import numpy as np
import cv2
from PIL import Image
from scipy import ndimage

try:
    from skimage.morphology import skeletonize
    HAS_SKIMAGE = True
except Exception:
    HAS_SKIMAGE = False

MIN_PRINT_STROKE_MM = 0.3      # 印刷可还原的最小笔画
# 测量精度下限：低于这个像素数的测量值是抗锯齿噪声，不是设计里的细线。
# 历史 bug：骨架化会在抗锯齿边缘长出单像素毛刺，导致「最细笔画」恒等于 2×像素尺寸
# （233dpi 时恒为 0.22mm，404dpi 时恒为 0.13mm），与画面内容无关，每版都挂一条假告警。
MEASURE_FLOOR_PX = 3.0
NEIGHBOR_CLEARANCE_MM = 0.4    # 两枚刀线之间必须保留的净距


# ---------------------------------------------------------------- 基础工具

def mm2px(mm, ppmm):
    return max(1, int(round(mm * ppmm)))


def disk(radius_px):
    r = max(1, int(radius_px))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def estimate_paper(img):
    h, w = img.shape[:2]
    band = max(4, min(h, w) // 100)
    ring = np.concatenate([
        img[:band].reshape(-1, 3), img[-band:].reshape(-1, 3),
        img[:, :band].reshape(-1, 3), img[:, -band:].reshape(-1, 3),
    ])
    return np.median(ring, axis=0)


def raw_artwork_mask(img, ppmm=None, denoise=True):
    """
    图案本体的掩膜。用于测真实缝隙和细笔画，所以不做闭运算/填洞。
    但必须去掉纸张纹理噪点 —— 否则单像素噪点会让「最细笔画」永远等于 0.08mm，
    也会被误当成邻居把间距压到 0.1mm。
    """
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
    S = hsv[:, :, 1].astype(np.int16)
    V = hsv[:, :, 2].astype(np.int16)
    paper = estimate_paper(img)
    dd = np.linalg.norm(img.astype(np.int16) - paper.astype(np.int16), axis=2)
    m = ((S >= 45) | (V <= 210) | (dd > 34)).astype(np.uint8)

    if denoise and ppmm:
        # 去掉小于 0.3mm² 的碎点（纸纹、压缩噪点）
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, disk(max(1, mm2px(0.12, ppmm))))
        n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
        min_px = max(4, int(0.3 * ppmm * ppmm))
        clean = np.zeros_like(m)
        for i in range(1, n):
            if st[i, cv2.CC_STAT_AREA] >= min_px:
                clean[lab == i] = 1
        m = clean
    return m


def segment_artwork(img, ppmm, min_area_mm2):
    """
    分割独立元素。
    参数经三张实测贴纸版校准（close 0.7mm / 1 次迭代 / open 0.25mm），
    结果与独立视觉核对一致（均为 7 枚）。iterations 一定是 1：
    用 2 次会把间距近的元素糊成一块。
    """
    # 分割必须用未去噪的掩膜：去噪的开运算会切断元素与其投影的连接，
    # 把一枚元素拆成本体 + 细长投影两块（实测会让 7 枚变成 9/13/17 枚）。
    m = raw_artwork_mask(img, ppmm, denoise=False)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, disk(mm2px(0.7, ppmm)), iterations=1)
    m = ndimage.binary_fill_holes(m).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, disk(mm2px(0.25, ppmm)))

    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    min_area_px = min_area_mm2 * ppmm * ppmm
    keep = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_area_px]
    keep.sort(key=lambda i: -stats[i, cv2.CC_STAT_AREA])
    return lab, stats, keep


def width_percentile(mask, ppmm, pct=5, prune_aa=False):
    """
    用距离变换 + 骨架测面材宽度（某点到边界距离 r → 该处宽度约 2r）。

    prune_aa=True 时先剪掉抗锯齿毛刺再测：骨架化会沿元素边缘生出大量
    半径约 1px 的伪分支，它们不是设计里的细线，会把测量值永久钉在 2 像素。
    """
    if mask.sum() == 0:
        return 0.0
    m = mask
    if prune_aa:
        # 开运算削掉 1px 级的边缘锯齿，再测
        m = cv2.morphologyEx(mask, cv2.MORPH_OPEN, disk(1))
        if m.sum() == 0:
            m = mask
    dist = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    if HAS_SKIMAGE:
        skel = skeletonize(m.astype(bool))
    else:
        skel = dist > 0.9 * cv2.dilate(dist, disk(2))
    if prune_aa:
        # 只保留半径 ≥1.2px 的骨架点，丢弃贴着边界的伪分支
        skel = skel & (dist >= 1.2)
    vals = 2.0 * dist[skel]
    if vals.size == 0:
        vals = 2.0 * dist[m.astype(bool)]
    if vals.size == 0:
        return 0.0
    return float(np.percentile(vals, pct)) / ppmm


def count_sharp_corners(mask, ppmm, angle_deg=50.0):
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    total, eps = 0, max(2.0, 0.5 * ppmm)
    for c in cnts:
        if cv2.contourArea(c) < (2 * ppmm) ** 2:
            continue
        ap = cv2.approxPolyDP(c, eps, True).reshape(-1, 2).astype(np.float64)
        m = len(ap)
        for i in range(m):
            a, b, cc = ap[(i - 1) % m], ap[i], ap[(i + 1) % m]
            v1, v2 = a - b, cc - b
            n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
            if n1 < 1e-6 or n2 < 1e-6:
                continue
            if np.degrees(np.arccos(np.clip(np.dot(v1, v2) / (n1 * n2), -1, 1))) < angle_deg:
                total += 1
    return total


def build_cutpath(comp, ppmm, offset_mm, corner_radius_mm):
    path = cv2.dilate(comp, disk(mm2px(offset_mm, ppmm)))
    r = mm2px(corner_radius_mm, ppmm)
    if r >= 1:
        path = cv2.morphologyEx(path, cv2.MORPH_CLOSE, disk(r))
        path = cv2.morphologyEx(path, cv2.MORPH_OPEN, disk(r))
    return path


# ---------------------------------------------------------------- 元素模型

@dataclass
class Element:
    idx: int
    area_mm2: float
    bbox_mm: tuple
    bbox_px: tuple
    print_stroke_mm: float      # 图案最细笔画（印刷能力）
    neighbor_gap_mm: float      # 到最近邻元素的净距
    edge_margin_mm: float
    sharp_corners: int
    offset_mm: float = 0.0      # 求解出的最优白边
    offset_lo: float = 0.0
    offset_hi: float = 0.0
    cut_width_mm: float = 0.0   # 刀线面材最窄处
    bridged_pct: float = 0.0    # 内部缝隙被糊掉的比例
    feasible: bool = True
    risk: float = 0.0
    grade: str = ""
    reasons: list = field(default_factory=list)


def solve_offset(comp, raw_comp, ppmm, safe_w, off_min, off_target, off_max,
                 neighbor_gap_mm, corner_radius_mm):
    """
    求这枚元素的白边 offset。

    白边不是「够用就行」——行业建议 1.5~2mm，用来吸收模切位移误差。
    所以策略是：以 off_target(默认1.5mm) 为目标值，
      · 邻居太近 → 往下压，但不低于 off_min，压不住就是排版冲突
      · 纤细结构面材不够 → 往上加，直到 safe_w 或撞到上界
    """
    hi = min(off_max, (neighbor_gap_mm - NEIGHBOR_CLEARANCE_MM) / 2.0)
    lo = off_min
    feasible = hi >= lo
    if not feasible:
        chosen = max(0.2, hi if hi > 0 else 0.2)
    else:
        chosen = min(off_target, hi)          # 先取理想白边（受邻距上限约束）
        if width_percentile(build_cutpath(comp, ppmm, chosen, corner_radius_mm),
                            ppmm) < safe_w:   # 面材不够 → 继续加大
            for off in np.arange(chosen, hi + 1e-6, 0.1):
                if width_percentile(build_cutpath(comp, ppmm, float(off), corner_radius_mm),
                                    ppmm) >= safe_w:
                    chosen = float(off)
                    break
            else:
                chosen = hi
    path = build_cutpath(comp, ppmm, chosen, corner_radius_mm)
    cut_w = width_percentile(path, ppmm)

    # 细节丢失：闭运算会把内部缝隙桥接掉，用『被填掉的面积 / 填实本体面积』衡量。
    # 分母必须用填实后的本体(comp)，用稀疏的 raw_comp 会算出 >100% 的荒谬值。
    closed = cv2.morphologyEx(raw_comp, cv2.MORPH_CLOSE, disk(mm2px(chosen, ppmm)))
    bridged = float(((closed > 0) & (raw_comp == 0)).sum()) / max(1, int(comp.sum()))
    bridged = min(1.0, max(0.0, bridged))

    return chosen, lo, hi, cut_w, bridged * 100.0, feasible, path


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="贴纸版模切体检 + 修复 + 生产文件导出")
    ap.add_argument("image")
    ap.add_argument("--sheet-width", type=float, default=148.0, help="成品整版宽度 mm（A5=148）")
    ap.add_argument("--safe-width", type=float, default=1.5, help="模切安全最小面材宽度 mm")
    ap.add_argument("--offset-min", type=float, default=0.8, help="白边 offset 下限 mm")
    ap.add_argument("--offset-target", type=float, default=1.5, help="白边 offset 目标值 mm（行业建议 1.5~2）")
    ap.add_argument("--offset-max", type=float, default=2.5, help="白边 offset 上限 mm")
    ap.add_argument("--corner-radius", type=float, default=1.0, help="尖角圆角化半径 mm")
    ap.add_argument("--min-area", type=float, default=25.0, help="忽略小于此面积的碎片 mm²")
    ap.add_argument("--bridge-limit", type=float, default=2.0, help="内部缝隙糊掉比例上限 %%")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    pil = Image.open(args.image)
    src_mode = pil.mode
    img = np.array(pil.convert("RGB"))
    H, W = img.shape[:2]
    ppmm = W / args.sheet_width
    dpi = ppmm * 25.4
    os.makedirs(args.outdir, exist_ok=True)

    print(f"[印前] {os.path.basename(args.image)}  {W}x{H}px  "
          f"成品 {args.sheet_width:.0f}x{H/ppmm:.0f}mm  {dpi:.0f} dpi  {src_mode}")

    prepress = []
    prepress.append(f"{'✅' if dpi >= 300 else '❌'} 有效分辨率 {dpi:.0f} dpi"
                    f"（要求 ≥300）")
    if src_mode != "CMYK":
        prepress.append(f"⚠️ 当前 {src_mode}，印刷需 CMYK。建议交工厂按其 ICC 转换，"
                        f"不要用软件默认转换（朱红会偏橙、暖白边会发灰）")

    raw = raw_artwork_mask(img, ppmm, denoise=True)   # 仅用于测细笔画/缝隙
    lab, stats, keep = segment_artwork(img, ppmm, args.min_area)
    print(f"[分割] {len(keep)} 枚独立元素\n")

    # 预先算每枚到其他元素的净距。
    # 注意：必须只用 keep 里的元素构建，若用 (lab>0) 会把已被过滤的碎点
    # 当成邻居，把间距误报成 0.1mm，进而虚报大量『排版冲突』。
    all_mask = np.isin(lab, keep).astype(np.uint8)
    elements, cut_full = [], np.zeros((H, W), np.uint8)

    for n, cid in enumerate(keep, start=1):
        comp = (lab == cid).astype(np.uint8)
        raw_comp = (raw > 0) & (comp > 0)
        raw_comp = raw_comp.astype(np.uint8)
        row = stats[cid]
        x, y, w, h = (row[cv2.CC_STAT_LEFT], row[cv2.CC_STAT_TOP],
                      row[cv2.CC_STAT_WIDTH], row[cv2.CC_STAT_HEIGHT])
        area_mm2 = row[cv2.CC_STAT_AREA] / (ppmm * ppmm)

        others = ((all_mask > 0) & (comp == 0)).astype(np.uint8)
        gap = (cv2.distanceTransform(1 - others, cv2.DIST_L2, 5)[comp.astype(bool)].min()
               / ppmm) if others.sum() else 999.0

        e = Element(
            idx=n, area_mm2=round(area_mm2, 1),
            bbox_mm=(round(w / ppmm, 1), round(h / ppmm, 1)),
            bbox_px=(int(x), int(y), int(w), int(h)),
            print_stroke_mm=round(width_percentile(raw_comp, ppmm, pct=5, prune_aa=True), 2),
            neighbor_gap_mm=round(float(gap), 2),
            edge_margin_mm=round(min(x, y, W - (x + w), H - (y + h)) / ppmm, 1),
            sharp_corners=count_sharp_corners(comp, ppmm),
        )

        off, lo, hi, cut_w, bridged, feasible, path = solve_offset(
            comp, raw_comp, ppmm, args.safe_width, args.offset_min,
            args.offset_target, args.offset_max, e.neighbor_gap_mm, args.corner_radius)
        e.offset_mm, e.offset_lo, e.offset_hi = round(off, 2), round(lo, 2), round(hi, 2)
        e.cut_width_mm, e.bridged_pct, e.feasible = round(cut_w, 2), round(bridged, 2), feasible
        cut_full = np.maximum(cut_full, path)

        # ---- 风险评分
        score, why = 0.0, []
        if not feasible:
            score += 55
            why.append(f"🔴 排版冲突：与邻居仅 {e.neighbor_gap_mm:.2f}mm，"
                       f"放不下 {args.offset_min}mm 白边 → 刀线必然粘连，必须调整间距")
        if e.cut_width_mm < args.safe_width:
            d = min(1.0, (args.safe_width - e.cut_width_mm) / args.safe_width)
            score += 35 * d
            why.append(f"刀线最窄处 {e.cut_width_mm:.2f}mm < 安全线 {args.safe_width}mm → 会被切断")
        if e.bridged_pct > args.bridge_limit:
            score += min(20.0, 20 * e.bridged_pct / (args.bridge_limit * 4))
            why.append(f"{e.bridged_pct:.1f}% 的内部缝隙被白边桥接 → 细节糊掉"
                       f"（如手指缝并成连指手套）")
        if feasible and e.offset_mm < args.offset_target - 0.05:
            score += 14
            why.append(f"白边被邻距压到 {e.offset_mm:.2f}mm（理想 {args.offset_target}mm）→ "
                       f"模切位移容错变小，建议加大元素间距")
        floor_mm = MEASURE_FLOOR_PX / ppmm
        if e.print_stroke_mm < floor_mm:
            # 低于测量精度：这是抗锯齿噪声，不是设计问题。不计分、不报警，
            # 只在报告里留一条说明，避免假告警把真告警淹掉。
            why.append(f"最细笔画测量值 {e.print_stroke_mm:.2f}mm 低于本图测量精度 "
                       f"{floor_mm:.2f}mm（{MEASURE_FLOOR_PX:.0f}px）→ 不作判定")
        elif e.print_stroke_mm < MIN_PRINT_STROKE_MM:
            score += 12
            why.append(f"图案最细笔画仅 {e.print_stroke_mm:.2f}mm < "
                       f"{MIN_PRINT_STROKE_MM}mm → 印刷可能断线")
        if e.sharp_corners > 0:
            score += min(10.0, 1.6 * e.sharp_corners)
            why.append(f"{e.sharp_corners} 处锐角 → 切不干净、一撕就烂")
        if e.area_mm2 < args.min_area * 1.6:
            score += 8
            why.append(f"面积仅 {e.area_mm2:.0f}mm² → 偏小不易撕取")
        if e.edge_margin_mm < 3.0:
            score += 8
            why.append(f"离整版边缘仅 {e.edge_margin_mm:.1f}mm → 出血不足，"
                       f"裁切偏移会切到图案")

        e.risk = round(min(100.0, score), 1)
        e.grade = ("必须修" if e.risk >= 55 else "高风险" if e.risk >= 35
                   else "注意" if e.risk >= 15 else "安全")
        e.reasons = why or ["未发现模切风险"]
        elements.append(e)

        flag = {"必须修": "🔴", "高风险": "🟠", "注意": "🟡", "安全": "🟢"}[e.grade]
        print(f"{flag} #{e.idx}  风险 {e.risk:5.1f} [{e.grade}]  "
              f"{e.bbox_mm[0]:.0f}x{e.bbox_mm[1]:.0f}mm  邻距 {e.neighbor_gap_mm:5.2f}mm  "
              f"offset {e.offset_mm:.2f}mm  刀线最窄 {e.cut_width_mm:.2f}mm")
        for r in e.reasons:
            print(f"      · {r}")

    # ---- 全版粘连复核
    nn, _, st2, _ = cv2.connectedComponentsWithStats(cut_full, 8)
    real = [i for i in range(1, nn) if st2[i, cv2.CC_STAT_AREA] > (3 * ppmm) ** 2]
    merged = len(elements) - len(real)
    layout_ok = merged <= 0
    print(f"\n[全版复核] {len(elements)} 枚元素 → 刀线轮廓 {len(real)} 个  "
          + ("✅ 无粘连" if layout_ok else f"🔴 有 {merged} 处粘连，撕不开"))

    ranked = sorted(elements, key=lambda x: -x.risk)

    # ---- 预览图
    prev = img.copy()
    colors = {"必须修": (220, 30, 30), "高风险": (240, 130, 20),
              "注意": (225, 200, 30), "安全": (40, 165, 80)}
    cnts, _ = cv2.findContours(cut_full, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(prev, cnts, -1, (255, 0, 255), max(2, int(0.25 * ppmm)))
    for e in elements:
        x, y, w, h = e.bbox_px
        c = colors[e.grade]
        cv2.rectangle(prev, (x, y), (x + w, y + h), c, max(2, int(0.3 * ppmm)))
        cv2.putText(prev, f"#{e.idx} {e.risk:.0f}", (x, max(24, y - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9 * ppmm / 12, c,
                    max(2, int(0.25 * ppmm)), cv2.LINE_AA)
    Image.fromarray(prev).save(os.path.join(args.outdir, "preview_risk.png"))

    # ---- 生产文件
    if not args.report_only:
        simp = max(1.0, 0.15 * ppmm)
        paths = []
        for c in cnts:
            if cv2.contourArea(c) < 16:
                continue
            apx = cv2.approxPolyDP(c, simp, True).reshape(-1, 2)
            if len(apx) >= 3:
                paths.append("M " + " L ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in apx) + " Z")
        art_rel = os.path.relpath(os.path.abspath(args.image), os.path.abspath(args.outdir))
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'width="{args.sheet_width:.2f}mm" height="{H/ppmm:.2f}mm" '
                f'viewBox="0 0 {W} {H}">\n'
                f'  <!-- print-ready-doctor | 刀线图层 CutContour, '
                f'专色 100%% Magenta, 0.25pt。工厂只切该图层。 -->\n')
        cut_layer = ('  <g id="CutContour" fill="none" stroke="#FF00FF" '
                     f'stroke-width="{max(1.0, 0.09*ppmm):.2f}">\n'
                     + "".join(f'    <path d="{d}"/>\n' for d in paths) + "  </g>\n")
        with open(os.path.join(args.outdir, "cutline.svg"), "w") as f:
            f.write(head + f'  <g id="Artwork"><image xlink:href="{art_rel}" '
                           f'x="0" y="0" width="{W}" height="{H}"/></g>\n' + cut_layer + "</svg>\n")
        with open(os.path.join(args.outdir, "cutline_only.svg"), "w") as f:
            f.write(head + cut_layer + "</svg>\n")

    # ---- 报告
    n_must = sum(1 for e in elements if e.grade == "必须修")
    n_high = sum(1 for e in elements if e.grade == "高风险")
    res = {"source": os.path.basename(args.image),
           "sheet_mm": [round(args.sheet_width, 1), round(H / ppmm, 1)],
           "effective_dpi": round(dpi), "color_mode": src_mode,
           "safe_width_mm": args.safe_width, "element_count": len(elements),
           "layout_ok": layout_ok, "merged_pairs": max(0, merged),
           "prepress": prepress, "elements": [asdict(e) for e in elements],
           "spare_priority": [e.idx for e in ranked]}
    with open(os.path.join(args.outdir, "report.json"), "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    L = [f"# 模切体检报告 · {os.path.basename(args.image)}", "",
         f"- 成品尺寸：{args.sheet_width:.0f} × {H/ppmm:.0f} mm",
         f"- 有效分辨率：**{dpi:.0f} dpi**　色彩模式：{src_mode}",
         f"- 安全最小面材宽度：{args.safe_width} mm",
         f"- 独立元素：**{len(elements)} 枚**",
         f"- 全版粘连复核：{'✅ 无粘连' if layout_ok else f'🔴 {merged} 处粘连'}",
         "", "## 印前检查", ""] + [f"- {p}" for p in prepress]
    L += ["", "## 风险排名（= 备损优先级，从上往下备）", "",
          "| 排名 | 元素 | 风险 | 判定 | 尺寸mm | 邻距mm | offset | 刀线最窄 | 缝隙糊掉 |",
          "|---:|---:|---:|---|---|---:|---:|---:|---:|"]
    for i, e in enumerate(ranked, 1):
        L.append(f"| {i} | #{e.idx} | {e.risk} | {e.grade} | "
                 f"{e.bbox_mm[0]:.0f}×{e.bbox_mm[1]:.0f} | {e.neighbor_gap_mm:.2f} | "
                 f"{e.offset_mm:.2f} | {e.cut_width_mm:.2f} | {e.bridged_pct:.1f}% |")
    L += ["", "## 逐枚明细", ""]
    for e in ranked:
        L += [f"### #{e.idx} · 风险 {e.risk} · {e.grade}", ""]
        L += [f"- {r}" for r in e.reasons]
        L += [f"- 可行 offset 区间：{e.offset_lo:.2f} ~ {e.offset_hi:.2f} mm，"
              f"取 **{e.offset_mm:.2f} mm**", ""]
    L += ["## 结论", "",
          f"- 必须修 **{n_must}** 枚，高风险 **{n_high}** 枚",
          f"- 刀线最窄处（全版）：**{min(e.cut_width_mm for e in elements):.2f} mm**"
          f"（安全线 {args.safe_width} mm）",
          f"- 备损优先备：**{'、'.join('#'+str(e.idx) for e in ranked[:2])}**", ""]
    if not layout_ok:
        L += ["> ⚠️ 存在刀线粘连，**不要送厂**。请加大元素间距后重新导出。", ""]
    with open(os.path.join(args.outdir, "report.md"), "w") as f:
        f.write("\n".join(L))

    print(f"[结论] 必须修 {n_must} 枚 / 高风险 {n_high} 枚 | "
          f"刀线最窄 {min(e.cut_width_mm for e in elements):.2f}mm")
    print(f"[备损] 优先备：{'、'.join('#'+str(e.idx) for e in ranked[:2])}")
    print(f"[输出] {args.outdir}/")
    return 0 if layout_ok and n_must == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
