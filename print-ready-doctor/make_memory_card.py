#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_memory_card.py —— 生成「卡纸打印图」（现场记忆卡）

────────────────────────────────────────────────────────────────────────
这张图是什么，和 A5 贴纸版有什么区别
────────────────────────────────────────────────────────────────────────
一单交付里有两件要印的东西，用途完全不同：

    A5 贴纸版        不干胶 + 模切 → 撕下来贴手账，是「耗材」
    卡纸打印图（本脚本） 厚卡纸整张打印 → 收藏 / 摆台 / 送人，是「留念品」

卡纸图 = 一整幅主视觉场景（左） + 几枚贴纸样（右） + 一行英文小标题（下）。
客户拿到的是「一张能直接看的作品」，而不是六个飘在白底上的图案。

────────────────────────────────────────────────────────────────────────
为什么排版必须交给代码，而不是让模型一次性画出整张卡
────────────────────────────────────────────────────────────────────────
早期版本是让模型直接出整张卡（scene + 贴纸 + 标题一次画完），三个问题：

  1. 标题是模型「画」出来的字母，几乎必然拼错或糊掉，而且不可修改；
  2. 卡上的贴纸和真正要模切的 A5 贴纸不是同一批图案 —— 客户会发现「卡上的
     蛋糕和我贴纸里的蛋糕长得不一样」；
  3. 出血、页边距、成品尺寸全靠运气，印厂那关过不了。

本脚本的做法：**卡上的贴纸就是从 A5 成品图里抠出来的那六枚本体**，
标题用真字体排，尺寸/出血/dpi 由代码算死。模型只负责画左边那幅场景。

────────────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────────────
    python3 make_memory_card.py \
        --scene    run/p5/scene.png \
        --stickers run/p5/FINAL.png \
        --caption  "CANDLELIGHT · WISH · SPARKLER" \
        --outdir   交付/05_鸟巢

    # 先看看哪几枚要上卡（会输出带编号的预览图）
    python3 make_memory_card.py --scene ... --stickers ... --outdir ... --preview-only
    # 然后排除人物那一枚
    python3 make_memory_card.py ... --drop 6

产出：
    卡纸打印图_A5横版_<dpi>dpi.png     成品尺寸，无出血
    卡纸打印图_含出血3mm.png            四边各 +3mm，给需要出血的印厂
    卡纸打印图.pdf                      同上，PDF
    卡纸_元素编号预览.png                标了序号，方便挑要上卡的元素
"""
import argparse
import os
import sys

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from print_ready_doctor import segment_artwork, mm2px, disk   # noqa: E402

# ── 视觉常量（都以 mm 为单位，和 dpi 解耦） ────────────────────────────────
BG_CREAM = (247, 241, 222)      # 卡纸底色，暖奶油白
SHADOW_RGB = (150, 132, 104)    # 阴影色：暖灰，不用纯黑，纯黑在暖底上会发脏
CAPTION_RGB = (122, 84, 60)     # 标题色：深棕
FRAME_BORDER_MM = 2.6           # 场景图外面那圈手撕纸白边
TEAR_AMPL_MM = 0.9              # 撕纸边缘的低频起伏
TEAR_FINE_MM = 0.28             # 高频毛边
SHADOW_OFFSET_MM = 0.9
SHADOW_BLUR_MM = 1.2
MARGIN_MM = 9.0
CAPTION_BAND_MM = 13.0
SCENE_RATIO = 0.545             # 场景图占可用宽度的比例
GRAB_DILATE_MM = 0.35
GRAB_FEATHER_PX = 1.6
MIN_ELEM_AREA_MM2 = 120.0
# A5 贴纸成品版的物理宽度。原来 148.0 直接写死在两处除法里，
# 改一处漏一处就会让右栏贴纸整体缩放算错，抽成常量。
STICKER_SHEET_WIDTH_MM = 148.0


def log(m):
    print(m, flush=True)


def imread_rgb(path, what):
    """
    读一张图并转成 RGB。

    为什么不直接用 cv2.imread：
      1. cv2.imread 走的是 C 库的 fopen，在 Windows 上遇到【中文路径】会直接
         返回 None（本项目的交付目录、文件名几乎全是中文，如 交付_20260828/06_银杏）。
         改成 np.fromfile + imdecode 后路径由 Python 打开，中文路径不再有问题。
      2. 原来三个调用点里有一个没判 None，会在下一行 cvtColor 抛一句看不懂的
         cv2.error，而真实原因只是路径写错。
    """
    try:
        buf = np.fromfile(path, dtype=np.uint8)
    except OSError as e:
        raise SystemExit("❌ 读不到%s：%s（%s）" % (what, path, e))
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
    if img is None:
        raise SystemExit("❌ 读不到%s：%s（文件不存在、为空，或不是 OpenCV 认识的图片格式）"
                         % (what, path))
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


# ── 手撕纸边缘 ─────────────────────────────────────────────────────────────

def _noise1d(n, ampl_px, smooth, rng):
    """一维低频噪声：撕纸边缘的起伏是连续的，不是逐像素随机的。"""
    if n <= 1:
        return np.zeros(max(n, 1), np.float32)
    raw = rng.normal(0, 1, n).astype(np.float32)
    k = max(3, int(smooth) | 1)
    sm = cv2.GaussianBlur(raw.reshape(-1, 1), (1, k), 0).ravel()
    if sm.std() > 1e-6:
        sm = sm / sm.std()
    return sm * ampl_px


def torn_mask(w, h, ppmm, seed=0, ampl_mm=TEAR_AMPL_MM, fine_mm=TEAR_FINE_MM):
    """
    生成一张 w×h 的「手撕纸片」掩膜（0/1 float）。
    做法：先画满，再从四条边分别啃掉一条宽度随位置起伏的带子。
    低频起伏给撕纸的大波浪，高频毛刺给纸纤维的碎口。
    """
    rng = np.random.default_rng(seed)
    a = mm2px(ampl_mm, ppmm)
    f = mm2px(fine_mm, ppmm)
    base = a + f + 2

    col = np.arange(w)[None, :]      # x 坐标
    row = np.arange(h)[:, None]      # y 坐标

    def edge(n, span):
        o = base + _noise1d(n, a, span / 14, rng) + _noise1d(n, f, 5, rng)
        return np.clip(o, 0, min(w, h) // 4)

    left = edge(h, h)[:, None]
    right = (w - edge(h, h))[:, None]
    top = edge(w, w)[None, :]
    bottom = (h - edge(w, w))[None, :]

    m = ((col >= left) & (col < right) & (row >= top) & (row < bottom)).astype(np.float32)
    # 轻微羽化：撕口不是刀切的硬边
    return np.clip(cv2.GaussianBlur(m, (0, 0), max(0.6, ppmm * 0.05)), 0, 1)


# ── 合成基元 ───────────────────────────────────────────────────────────────

def paste_rgba(canvas, rgb, alpha, x, y, ppmm, shadow=True):
    """把一块带 alpha 的图贴到画布上，可选在下方投一层暖灰软阴影。"""
    h, w = alpha.shape
    H, W = canvas.shape[:2]
    if shadow:
        off = mm2px(SHADOW_OFFSET_MM, ppmm)
        blur = max(1.0, mm2px(SHADOW_BLUR_MM, ppmm))
        pad = int(blur * 3)
        sa = np.zeros((h + 2 * pad, w + 2 * pad), np.float32)
        sa[pad:pad + h, pad:pad + w] = alpha
        sa = cv2.GaussianBlur(sa, (0, 0), blur) * 0.42
        sy, sx = y - pad + off, x - pad + off
        y0, x0 = max(0, sy), max(0, sx)
        y1, x1 = min(H, sy + sa.shape[0]), min(W, sx + sa.shape[1])
        if y1 > y0 and x1 > x0:
            sub = sa[y0 - sy:y1 - sy, x0 - sx:x1 - sx][:, :, None]
            roi = canvas[y0:y1, x0:x1]
            canvas[y0:y1, x0:x1] = roi * (1 - sub) + np.array(SHADOW_RGB, np.float32) * sub

    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(H, y + h), min(W, x + w)
    if y1 <= y0 or x1 <= x0:
        return
    a = alpha[y0 - y:y1 - y, x0 - x:x1 - x][:, :, None]
    c = rgb[y0 - y:y1 - y, x0 - x:x1 - x]
    canvas[y0:y1, x0:x1] = canvas[y0:y1, x0:x1] * (1 - a) + c * a


def paper_grain(canvas, ppmm, seed=7):
    """卡纸底纹。没有它，大片纯色底在实物上会显得很塑料。"""
    rng = np.random.default_rng(seed)
    H, W = canvas.shape[:2]
    # 纸纹是【高频细颗粒】。早期版本先降采样 6 倍再放大，得到的是低频云斑，
    # 印出来像脏底而不是纸纤维，所以这里直接在全分辨率上加细噪声再微糊。
    g = rng.normal(0, 1, (H, W)).astype(np.float32)
    g = cv2.GaussianBlur(g, (0, 0), max(0.5, ppmm * 0.035))
    if g.std() > 1e-6:
        g /= g.std()
    return np.clip(canvas + g[:, :, None] * 1.9, 0, 255)


# ── 元素抠取 ───────────────────────────────────────────────────────────────

def grab_elements(sheet_bgr, ppmm):
    """从 A5 贴纸成品图里把每一枚元素连同它的暖白撕边一起抠出来。"""
    lab, stats, keep = segment_artwork(sheet_bgr, ppmm, MIN_ELEM_AREA_MM2)
    all_mask = np.isin(lab, keep)
    dil = disk(mm2px(GRAB_DILATE_MM, ppmm))
    out = []
    for cid in keep:
        comp = (lab == cid).astype(np.uint8)
        grown = cv2.dilate(comp, dil)
        grown[(all_mask) & (comp == 0)] = 0
        ys, xs = np.where(grown > 0)
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        a = cv2.GaussianBlur(grown[y0:y1, x0:x1].astype(np.float32), (0, 0), GRAB_FEATHER_PX)
        out.append({"rgb": sheet_bgr[y0:y1, x0:x1].astype(np.float32),
                    "alpha": np.clip(a, 0, 1),
                    "cy": float(ys.mean()), "cx": float(xs.mean())})
    # 按阅读顺序编号，和交付包里的 01.png~06.png 对得上
    out.sort(key=lambda e: (round(e["cy"] / max(1, sheet_bgr.shape[0]) * 6), e["cx"]))
    return out


# ── 标题排字 ───────────────────────────────────────────────────────────────

def _font(size, text):
    p = os.environ.get("CARD_FONT_PATH")
    cands = [p] if p else []
    cjk = any("\u4e00" <= ch <= "\u9fff" for ch in text)
    if cjk:
        cands += ["/usr/share/fonts/truetype/source-han-serif/SourceHanSerifSC-VF.ttf",
                  "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
                  "/System/Library/Fonts/Songti.ttc",
                  "C:/Windows/Fonts/simsun.ttc"]
    cands += ["/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
              "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
              "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
              "C:/Windows/Fonts/times.ttf"]
    for c in cands:
        if c and os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    return None


def draw_caption(canvas, text, cx_px, y_px, size_px, tracking):
    """
    逐字排字，手动加字距。
    参考稿那种「C A N D L E L I G H T」的呼吸感全靠字距，
    PIL 没有 tracking 参数，只能一个字一个字量宽度再摆。
    """
    if not text:
        return
    f = _font(size_px, text)
    if f is None:
        log("⚠️ 没找到可用衬线字体，跳过标题（可用 CARD_FONT_PATH 指定）")
        return
    img = Image.fromarray(canvas.astype(np.uint8))
    d = ImageDraw.Draw(img)
    widths = [d.textlength(ch, font=f) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = cx_px - total / 2.0
    for ch, w in zip(text, widths):
        d.text((x, y_px), ch, font=f, fill=CAPTION_RGB)
        x += w + tracking
    canvas[:] = np.asarray(img).astype(np.float32)


# ── 主流程 ─────────────────────────────────────────────────────────────────

def build_card(scene_path, sheet_path, caption, w_mm, h_mm, dpi,
               only=None, drop=None, max_elems=6, seed=3):
    ppmm = dpi / 25.4
    W, H = int(round(w_mm * ppmm)), int(round(h_mm * ppmm))
    canvas = np.zeros((H, W, 3), np.float32)
    canvas[:, :] = np.array(BG_CREAM, np.float32)
    canvas = paper_grain(canvas, ppmm)

    m = mm2px(MARGIN_MM, ppmm)
    cap_h = mm2px(CAPTION_BAND_MM, ppmm)
    area_w, area_h = W - 2 * m, H - 2 * m - cap_h

    # ① 主视觉场景：贴在一张手撕白纸上，再压一层软阴影
    scene = imread_rgb(scene_path, "场景图")
    fb = mm2px(FRAME_BORDER_MM, ppmm)
    box_w = int(area_w * SCENE_RATIO)
    box_h = area_h
    inner_w, inner_h = box_w - 2 * fb, box_h - 2 * fb
    sh, sw = scene.shape[:2]
    s = max(inner_w / sw, inner_h / sh)          # 填满内框，多余的裁掉
    rs = cv2.resize(scene, (int(round(sw * s)), int(round(sh * s))),
                    interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    oy = (rs.shape[0] - inner_h) // 2
    ox = (rs.shape[1] - inner_w) // 2
    rs = rs[oy:oy + inner_h, ox:ox + inner_w]

    sheet_mask = torn_mask(box_w, box_h, ppmm, seed=seed)
    inner_mask = torn_mask(inner_w, inner_h, ppmm, seed=seed + 91,
                           ampl_mm=TEAR_AMPL_MM * 0.55, fine_mm=TEAR_FINE_MM * 0.7)
    plate = np.zeros((box_h, box_w, 3), np.float32)
    plate[:, :] = (252, 249, 240)                # 撕纸白边，不是纯白
    im = inner_mask[:, :, None]
    plate[fb:fb + inner_h, fb:fb + inner_w] = (
        plate[fb:fb + inner_h, fb:fb + inner_w] * (1 - im) + rs.astype(np.float32) * im)
    paste_rgba(canvas, plate, sheet_mask, m, m, ppmm)

    # ② 右栏贴纸样：就是 A5 成品里的那几枚本体
    sheet = imread_rgb(sheet_path, "贴纸成品图")
    sheet_ppmm = sheet.shape[1] / STICKER_SHEET_WIDTH_MM   # A5 竖版成品宽 148mm
    elems = grab_elements(sheet, sheet_ppmm)
    log("· 贴纸成品里检出 %d 枚元素" % len(elems))

    idx = list(range(1, len(elems) + 1))
    if only:
        idx = [i for i in idx if i in only]
    if drop:
        idx = [i for i in idx if i not in drop]
    idx = idx[:max_elems]
    chosen = [elems[i - 1] for i in idx]
    log("· 上卡元素编号：%s" % ", ".join(map(str, idx)))

    rx = m + box_w + mm2px(6.0, ppmm)
    rw = W - m - rx
    n = len(chosen)
    cols = 1 if n <= 2 else 2
    rows = int(np.ceil(n / cols))
    cw, ch = rw // cols, area_h // rows
    pad = mm2px(3.0, ppmm)

    # 全局统一缩放：保住元素之间的大小关系（蛋糕就该比玻璃杯大），
    # 逐个撑满格子会把所有元素拉成一样大，那就不像手摆的了
    scales = []
    for e in chosen:
        eh, ew = e["alpha"].shape
        sc = (sheet_ppmm and ppmm / sheet_ppmm) or 1.0
        scales.append(min((cw - 2 * pad) / (ew * sc), (ch - 2 * pad) / (eh * sc)))
    g = min(scales) * 0.98 if scales else 1.0

    rng = np.random.default_rng(seed)
    for k, e in enumerate(chosen):
        r, c = divmod(k, cols)
        # 最后一行不满时居中，否则孤零零一枚吊在左边很像漏排
        in_last = (r == rows - 1)
        n_last = n - cols * (rows - 1)
        row_off = ((cols - n_last) * cw) // 2 if (in_last and n_last < cols) else 0
        eh, ew = e["alpha"].shape
        sc = (ppmm / sheet_ppmm) * g
        tw, th = max(1, int(ew * sc)), max(1, int(eh * sc))
        rgb = cv2.resize(e["rgb"], (tw, th), interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
        al = cv2.resize(e["alpha"], (tw, th), interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
        ang = float(rng.uniform(-2.2, 2.2))      # 轻微旋转 = 手摆的感觉
        M = cv2.getRotationMatrix2D((tw / 2, th / 2), ang, 1.0)
        cosv, sinv = abs(M[0, 0]), abs(M[0, 1])
        nw, nh = int(th * sinv + tw * cosv), int(th * cosv + tw * sinv)
        M[0, 2] += nw / 2 - tw / 2
        M[1, 2] += nh / 2 - th / 2
        rgb = cv2.warpAffine(rgb, M, (nw, nh), flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_REPLICATE)
        al = np.clip(cv2.warpAffine(al, M, (nw, nh), flags=cv2.INTER_CUBIC,
                                    borderValue=0), 0, 1)
        jx = int(rng.uniform(-1, 1) * mm2px(1.6, ppmm))
        jy = int(rng.uniform(-1, 1) * mm2px(1.6, ppmm))
        x = rx + row_off + c * cw + (cw - nw) // 2 + jx
        y = m + r * ch + (ch - nh) // 2 + jy
        paste_rgba(canvas, rgb, al, x, y, ppmm)

    # ③ 标题
    draw_caption(canvas, caption, W / 2.0, H - m - cap_h + mm2px(1.0, ppmm),
                 int(mm2px(5.2, ppmm)), mm2px(2.4, ppmm))
    return np.clip(canvas, 0, 255).astype(np.uint8), elems, idx


def preview_index(elems, path):
    """带编号的元素预览，用来决定哪几枚上卡。"""
    cells = []
    for i, e in enumerate(elems, 1):
        a = e["alpha"][:, :, None]
        img = (e["rgb"] * a + 255 * (1 - a)).astype(np.uint8)
        img = cv2.resize(img, (420, int(420 * img.shape[0] / img.shape[1])))
        pane = np.full((520, 460, 3), 255, np.uint8)
        pane[70:70 + min(440, img.shape[0]), 20:20 + img.shape[1]] = img[:440]
        cv2.putText(pane, "#%d" % i, (22, 52), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (30, 30, 30), 3)
        cells.append(pane)
    if not cells:
        return
    per = 3
    rows = [np.hstack(cells[i:i + per] + [np.full((520, 460, 3), 255, np.uint8)]
                      * (per - len(cells[i:i + per]))) for i in range(0, len(cells), per)]
    Image.fromarray(np.vstack(rows)).save(path)


def main():
    ap = argparse.ArgumentParser(description="生成卡纸打印图（现场记忆卡）")
    ap.add_argument("--scene", required=True, help="主视觉场景图（forge_scene.py 产出）")
    ap.add_argument("--stickers", required=True, help="A5 贴纸成品图 FINAL.png")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--caption", default="", help="底部小标题，留空则不排字")
    ap.add_argument("--width", type=float, default=210.0, help="成品宽 mm，默认 A5 横版 210")
    ap.add_argument("--height", type=float, default=148.0, help="成品高 mm，默认 148")
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--bleed", type=float, default=3.0, help="出血 mm，0 为不出血版")
    ap.add_argument("--only", default="", help="只用这几枚，如 1,2,3,5")
    ap.add_argument("--drop", default="", help="排除这几枚，如 6（人物那枚通常不上卡）")
    ap.add_argument("--max-elements", type=int, default=6,
                    help="右栏最多放几枚，默认 6（产品规格：5 物品 + 1 人物）")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--preview-only", action="store_true", help="只出编号预览图")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    parse = lambda s: {int(x) for x in s.replace("，", ",").split(",") if x.strip().isdigit()}

    if args.preview_only:
        sheet = imread_rgb(args.stickers, "贴纸成品图")
        elems = grab_elements(sheet, sheet.shape[1] / STICKER_SHEET_WIDTH_MM)
        p = os.path.join(args.outdir, "卡纸_元素编号预览.png")
        preview_index(elems, p)
        log("✅ 预览：%s（共 %d 枚）" % (p, len(elems)))
        return

    card, elems, used = build_card(
        args.scene, args.stickers, args.caption, args.width, args.height, args.dpi,
        only=parse(args.only), drop=parse(args.drop),
        max_elems=args.max_elements, seed=args.seed)

    base = os.path.join(args.outdir, "卡纸打印图_%.0fx%.0fmm_%ddpi" %
                        (args.width, args.height, args.dpi))
    Image.fromarray(card).save(base + ".png", dpi=(args.dpi, args.dpi))
    log("✅ 成品图：%s.png  %dx%dpx" % (base, card.shape[1], card.shape[0]))

    if args.bleed > 0:
        ppmm = args.dpi / 25.4
        b = mm2px(args.bleed, ppmm)
        # 出血靠镜像外扩，不是简单填底色 —— 底纹在裁切线两侧才连得上
        bl = cv2.copyMakeBorder(card, b, b, b, b, cv2.BORDER_REFLECT_101)
        Image.fromarray(bl).save(base + "_含出血%gmm.png" % args.bleed,
                                 dpi=(args.dpi, args.dpi))
        log("✅ 出血版：%s_含出血%gmm.png" % (base, args.bleed))

    Image.fromarray(card).save(base + ".pdf", "PDF", resolution=float(args.dpi))
    log("✅ PDF：%s.pdf" % base)

    preview_index(elems, os.path.join(args.outdir, "卡纸_元素编号预览.png"))
    with open(os.path.join(args.outdir, "卡纸说明.txt"), "w", encoding="utf-8") as f:
        f.write("成品尺寸：%.0f × %.0f mm\n分辨率：%d dpi\n出血：%g mm\n"
                "上卡元素编号：%s（共检出 %d 枚）\n标题：%s\n"
                % (args.width, args.height, args.dpi, args.bleed,
                   ", ".join(map(str, used)), len(elems), args.caption or "（无）"))


if __name__ == "__main__":
    main()
