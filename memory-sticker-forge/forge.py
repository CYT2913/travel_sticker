#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory-sticker-forge —— 照片 → 6 枚水粉剪纸风格定制贴纸元素

设计目标：不是"一条 prompt 碰运气"，而是"用户侧一次成功"。
内部闭环：照片体检 → 场景解析 → 自动组装 prompt → 4k 生成 → **程序化重排** → 双重质检 → 定向重试 → 只交付合格稿。

v1.4 关键改动：排版不再交给模型
--------------------------------
模型出图后先过 print-ready-doctor/relayout.py：把每枚元素抠出来，
按网格重排到干净 A5 画布上，硬保证「元素净距 ≥8mm、四边留白 ≥8mm」。
排版是纯几何问题，代码能 100% 保证，没必要用重试去赌。
这一步之后，量化质检里的三类失败（邻距<3mm / 必须修>0 / 元素数≠刀线数）
在物理上不再可能发生，重试只用来解决风格类问题。
用 --no-relayout 可退回旧行为做对比。

用法：
    python3 forge.py <photo> [--outdir out] [--max-rounds 4] [--elements 6]
    python3 forge.py <photo> --preflight-only      # 只做 G0 体检，不生成（不花 token）
    python3 forge.py <photo> --no-relayout         # 关掉程序化重排（旧行为）

退出码：0 = 交付合格稿；1 = G0 拒稿；2 = 达到重试上限仍不合格
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import providers

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _find_tool(filename, env_key):
    """定位 print-ready-doctor 的脚本。
    优先级: 环境变量 > 兄弟目录 > 父目录 > 同目录 > PATH 搜索。
    这样 Codex / 本地环境下无论怎么摆目录都能跑。
    """
    env = os.environ.get(env_key)
    if env and os.path.isfile(env):
        return env
    d = os.environ.get("PRINT_READY_DOCTOR_DIR")
    cands = []
    if d:
        cands.append(os.path.join(d, filename))
    cands += [
        os.path.join(ROOT, "print-ready-doctor", filename),
        os.path.join(HERE, "print-ready-doctor", filename),
        os.path.join(HERE, "..", "print-ready-doctor", filename),
        os.path.join(HERE, filename),
        os.path.join(os.getcwd(), "print-ready-doctor", filename),
    ]
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return os.path.join(ROOT, "print-ready-doctor", filename)  # 让后续报错信息带路径


DOCTOR = _find_tool("print_ready_doctor.py", "DOCTOR_PATH")
RELAYOUT = _find_tool("relayout.py", "RELAYOUT_PATH")

VISION_MAX_BYTES = 3_800_000
SHEET_WIDTH_MM = 148          # A5 竖版宽
SHEET_HEIGHT_MM = 210         # A5 竖版高（重排后的成品画布）

# ── 工具 ───────────────────────────────────────────────────────────────────

def log(msg): print(msg, flush=True)

def open_photo(path):
    """
    读取【用户原始照片】的唯一入口。所有读图都必须走这里，不要直接 Image.open()。

    为什么必须有这个函数（2026-08-30 · 06 银杏实拍回归）：
    手机拍的照片普遍把方向记在 EXIF Orientation 里（横拍竖持 = 6），像素本身是
    躺着的。直接按像素读图，生成的场景图就整幅横躺 —— 而这类问题所有自动检查
    都发现不了（dpi/邻距/枚数全合格），只有肉眼看成品才知道。
    `ImageOps.exif_transpose` 按 Orientation 旋转并抹掉该标签；对没有 EXIF 的
    PNG（我们自己生成的中间产物）是无操作，因此统一走这里不会有副作用。
    """
    from PIL import Image, ImageOps
    return ImageOps.exif_transpose(Image.open(path))

def shrink(src, dst, box=1600, q=88, limit=VISION_MAX_BYTES):
    """压到视觉模型能吃的尺寸。质检必须用原图跑 doctor，压缩图只给视觉模型。"""
    im = open_photo(src); im.thumbnail((box, box))
    im.convert("RGB").save(dst, quality=q)
    while os.path.getsize(dst) > limit and q > 40:
        q -= 8
        im.convert("RGB").save(dst, quality=q)
    return dst

def call_vision(paths, task):
    """视觉理解。实现在 providers.py，换厂商不用动这里。"""
    return providers.analyze_images(paths, task)

def grab_json(txt):
    """从视觉模型回答里抠出第一个 JSON 对象。"""
    depth, start = 0, None
    for i, c in enumerate(txt):
        if c == "{":
            if depth == 0: start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try: return json.loads(txt[start:i+1])
                except Exception: start = None
    return None

# ── 步骤 1：G0 照片体检 + 场景解析（一次调用拿全） ──────────────────────────

PREFLIGHT_TASK = """你是定制贴纸产品的照片准入审核员 + 元素规划师。分析这张照片，只输出一个 JSON 对象，不要任何解释文字。

JSON 字段：
{
  "scene": "一句话场景描述",
  "lighting": "day" 或 "night",          // 舞台强色光/夜景/暗光算 night；日光/室内暖光算 day
  "people_count": 整数,                   // 画面中可辨识的人数
  "has_minor": true/false,                // 是否可能含未成年人
  "ip_items": ["..."],                    // 所有品牌logo、赞助商字样、产品字标、大屏画面内容、海报图案、动漫形象。没有则空数组
  "standalone_objects": ["...","..."],    // 至少6个、最多8个【能单独抠出来做贴纸的实体物品】，按"纪念价值"从高到低排（不是按体量）。
                                          // 必须是物品不是人；必须是画面里真实存在的；用英文，简短具体，如 "electric guitar","drum kit","birthday cake"
  "keepsake_objects": ["..."],            // 从 standalone_objects 中挑出【最能代表这个场合、最有纪念意义】的物品，2~4个。
                                          // 例如生日场合的 "candle","sparkler","balloon"；演出场合的主乐器；旅行场合的地标构件。
                                          // 这些即使体量小、即使偏细，也必须做成贴纸，绝不可省略。
  "container_pairs": [["A","B"]],         // 【容器/承载关系】画面中 A 物品本来就盛放/承载/附着在 B 物品上的所有配对。
                                          // 如 [["cheesecake","plate"],["drink","glass cup"]]。没有则空数组。用英文，名称必须与上面完全一致
  "similar_pairs": [["A","B"]],           // 【外形雷同关系】简化成扁平剪纸后轮廓会几乎一样、放在同一张贴纸上会显得重复的配对。
                                          // 如 [["electric guitar","bass guitar"],["snare drum","tom drum"]]。没有则空数组
  "thin_parts": ["..."],                  // 仅填【附属的细杆/细线/细丝】本身，如 "mic stand pole","cable","candle wick","guitar strings"。
                                          // ⚠️ 严禁填写主体物品名。蜡烛整体、仙女棒整体、麦克风整体都是主体物品，不是 thin_parts，
                                          //    只有它们身上那根细杆/细芯才算。填错会导致该纪念物品被整个删掉。
  "reject_reason": null 或 "拒稿原因"       // 无法做贴纸时填写
}

判定 standalone_objects 时注意：
1. 优先选有辨识度、轮廓完整的物品；体量小但有纪念意义的（蜡烛、仙女棒、气球）要选进来并同时写进 keepsake_objects。
2. 不要选人体部位，不要选背景墙面。
3. 同一类物品只保留最有代表性的一个（有两把吉他就只留主吉他），其余的写进 similar_pairs。
4. 如果一个物品总是盛在另一个物品上（蛋糕在盘子上），两个都可以列出，但必须在 container_pairs 里如实标注。
5. ⚠️ 绝对不要选【整套组合设备】。贴纸只能是单个厚实的物体，凡是由多根支架/细杆撑起来的成套装备都做不了模切。
   看到成套装备时，只挑其中体量最厚实的那一件单品：
   看到 drum kit / drum set（整套架子鼓）→ 只选 "bass drum"；
   看到 microphone stand（麦架）→ 只选 "microphone"；
   看到 camera tripod（三脚架）→ 只选 "camera"；
   看到 cymbal stand（镲架）→ 只选 "cymbal"。
   清单里出现 "kit"、"set"、"rig"、"stand"、"tripod"、"rack" 这类词，几乎都是选错了。"""

# ── G0 字段兜底词库 ────────────────────────────────────────────────────────
# 为什么要有这两张表：container_pairs / keepsake_objects 是 G0 的「可选字段」，
# 视觉模型在实拍里经常直接返回 null（run_regress 复现）。一旦为空，
# 「容器剔除」和「纪念物置顶」两条保护就全部失效 —— 结果就是空盘子单独成一枚贴纸。
# 这两张表把判断从「模型自觉」下沉到「代码兜底」，模型标了就用模型的，没标就用这里的。

# 单独做成贴纸毫无纪念价值的容器/承载面。它们只在「托着别的东西」时有意义。
CONTAINER_WORDS = {
    "plate", "dish", "saucer", "platter", "tray", "bowl", "stand", "holder",
    "coaster", "napkin", "placemat", "table", "tabletop", "desk", "counter",
    "surface", "floor", "ground", "wall", "shelf", "rack", "base", "pedestal",
}
# 承载对象：食物/蛋糕/甜点等，出现其一就说明同场的容器是多余的
CONTAINED_WORDS = {
    "cake", "slice", "dessert", "pastry", "cheesecake", "cupcake", "pie",
    "food", "dish", "meal", "fruit", "bread", "sandwich", "noodles", "sushi",
}
# 承载了「场合记忆」的物件，偏细也必须加粗保留，绝不能被截断或当细部删掉
KEEPSAKE_WORDS = {
    "candle", "sparkler", "firework", "balloon", "gift", "present", "ribbon",
    "bouquet", "flower", "rose", "cake", "ticket", "medal", "trophy", "ring",
    "lantern", "wish", "card", "letter", "badge", "crown", "toast",
    "champagne", "confetti", "lucky", "charm", "souvenir", "postcard", "stamp",
}
# 「主体价值依赖文字」的物件 —— 合规硬要求是画面里不许出现任何可读文字，
# 这类东西去掉字就只剩一个纯色空框，单独做贴纸几乎没有价值。
# 实拍证据（2026-08-30）：03 故宫匾额去字后是纯蓝空框、04 志愿活动展板是一大块
# 空深棕、05 鸟巢告示牌是空色块。
# ⚠️ 只降权、不硬删：候选实在不够时，一枚空色块仍然好过整版缺一枚。
#    降权逻辑在 select_objects 的排序里，兜底回填照旧生效。
# 注：banner 原本在 KEEPSAKE_WORDS 里（生日横幅），但横幅的内容就是那行字，
#    去字后同样只剩色块，两条规则冲突时以「去字后还剩什么」为准，故移到这里。
TEXT_DEPENDENT_WORDS = {
    "sign", "signs", "signage", "signboard", "signpost", "banner", "plaque",
    "billboard", "poster", "nameplate", "placard", "board", "boards",
    "noticeboard", "notice", "screen", "display", "label", "tag", "menu",
    "certificate", "scoreboard", "marquee", "leaflet", "flyer", "brochure",
    "inscription", "tablet",
}


def _words(name):
    return set(_norm(name).split())


def infer_container_pairs(objs, given):
    """模型给了就沿用；没给就按词库推断 (内容物, 容器) 对。"""
    pairs = [p for p in (given or []) if isinstance(p, (list, tuple)) and len(p) == 2]
    if pairs:
        return pairs
    containers = [o for o in objs if _words(o) & CONTAINER_WORDS]
    contents = [o for o in objs if (_words(o) & CONTAINED_WORDS)
                and not (_words(o) & CONTAINER_WORDS)]
    out = []
    for c in containers:
        # 有内容物 → 容器随内容物一起画；没有内容物 → 容器仍然是废件，
        # 用它自己配对，select_objects 会把它排到最后
        out.append([contents[0] if contents else c, c])
    return out


def infer_keepsakes(objs, given):
    if given:
        return given
    return [o for o in objs if _words(o) & KEEPSAKE_WORDS]


def _text_dependent(name):
    """这枚物体的主体价值是不是全在文字上（按合规去字后只剩一块纯色空框）。"""
    return bool(_words(name) & TEXT_DEPENDENT_WORDS)


def preflight(photo, workdir):
    # 尺寸也必须按 EXIF 摆正后再取：Orientation=6 的竖拍照片，原始像素是
    # 3024x4032 记成 4032x3024，_size_px 会写反（短边不受影响，但报告会误导）。
    _w, _h = open_photo(photo).size
    small = shrink(photo, os.path.join(workdir, "src_small.jpg"))
    txt = call_vision([small], PREFLIGHT_TASK)
    data = grab_json(txt)
    if not data:
        raise SystemExit("❌ 场景解析失败，视觉模型未返回可解析 JSON：\n" + txt[:600])
    # ⚠️ 不能用 setdefault：视觉模型经常把可选字段显式写成 null（实拍 run_regress
    #    的 keepsake_objects / container_pairs 全是 null），setdefault 只在「键不存在」
    #    时生效，null 会原样留下，后面 `for x in None` 直接把整套保护逻辑跳过。
    #    这就是生日单出现「空盘子单独成为一枚贴纸」的真正原因。
    for _k, _dv in (("ip_items", []), ("thin_parts", []), ("standalone_objects", []),
                    ("keepsake_objects", []), ("container_pairs", []),
                    ("similar_pairs", []), ("people_count", 0)):
        if data.get(_k) is None:
            data[_k] = _dv
    # 模型漏标时用内置词库补全，不依赖模型自觉
    data["container_pairs"] = infer_container_pairs(
        data["standalone_objects"], data["container_pairs"])
    data["keepsake_objects"] = infer_keepsakes(
        data["standalone_objects"], data["keepsake_objects"])
    # 保险：模型仍可能把主体物品误填进 thin_parts，导致该物品被整个省略。
    # 凡是同时出现在 standalone_objects 里的，一律从 thin_parts 剔除。
    _objs = {str(o).strip().lower() for o in data["standalone_objects"]}
    _kept, _rescued = [], []
    for t in data["thin_parts"]:
        if str(t).strip().lower() in _objs:
            _rescued.append(t)
        else:
            _kept.append(t)
    data["thin_parts"] = _kept
    if _rescued:
        data["_rescued_from_thin"] = _rescued
    data["_short_edge_px"] = min(_w, _h)
    data["_size_px"] = "%dx%d" % (_w, _h)
    return data, small

MIN_SHORT_EDGE_PX = 1500   # PRD 18.3 的照片准入标准
HARD_SHORT_EDGE_PX = 600   # 低于此值细节不足以拆出可辨识的独立元素


def g0_gate(info, n_elements):
    """G0 照片准入。返回 (是否通过, 阻断项, 警告项)"""
    blocks, warns = [], []
    # 客观分辨率门禁必须放在最前面。
    # 历史 bug：这一项只靠视觉模型主观判断，结果 205px 的照片被拒、92px 的却放过了。
    se = info.get("_short_edge_px")
    if se is not None:
        if se < HARD_SHORT_EDGE_PX:
            blocks.append("照片短边仅 %dpx，低于 %dpx —— 细节不足以拆出可辨识的独立元素，"
                          "生成结果只能靠模型凭空补，与客户原照片对不上"
                          % (se, HARD_SHORT_EDGE_PX))
        elif se < MIN_SHORT_EDGE_PX:
            warns.append("照片短边仅 %dpx，低于建议的 %dpx —— 可以做，但小物件细节会丢失，"
                         "需与客户说明" % (se, MIN_SHORT_EDGE_PX))
    if info.get("reject_reason"):
        blocks.append("视觉审核判定不可用：%s" % info["reject_reason"])
    # 未成年人：视觉模型在人群照上误报率高（把成年观众判成未成年），因此不做硬阻断，
    # 改为强制人工确认项 —— 真正的判断人是你，不是模型。
    if info.get("has_minor"):
        warns.append("⚠️ 需人工确认：疑似含未成年人。若确为未成年人，须取得监护人书面授权后再接单")
    objs = info.get("standalone_objects", [])
    need = max(4, n_elements - (0 if (info.get("people_count") or 0) == 0 else 1))
    if len(objs) < need:
        blocks.append("可独立成件的物品仅 %d 个，少于 %d 个 —— 做出来会全是人物剪影，装饰性不足" % (len(objs), need))
    if info.get("people_count", 0) > 3:
        warns.append("画面 %d 人，超过 3 人建议标准 —— 人物会被压缩为背景群像，可接单但需与客户说明"
                     % info["people_count"])
    if info.get("ip_items"):
        warns.append("检测到 %d 处第三方 IP，将在生成阶段强制剔除并复检：%s"
                     % (len(info["ip_items"]), ", ".join(info["ip_items"][:8])))
    return (len(blocks) == 0), blocks, warns

# ── 步骤 2：组装 prompt ────────────────────────────────────────────────────

STYLE = """STYLE: Opaque gouache painting assembled as a hand-cut paper collage. Flat matte pigment in clean crisp edges like scissors-cut paper, flat opaque color blocks, each block one even tone carrying only a faint cold-pressed paper grain. Every shape reads as a separately painted piece of paper cut out by hand and laid down. Each cut-out has a warm off-white (cream, NOT pure white) hand-cut border with a soft short drop shadow. Simplified, poster-like shapes. No photorealism, no gradients, no gloss, no visible brush strokes, no digital smoothness."""

PALETTE_DAY = """PALETTE: restricted earthy palette. The DOMINANT colours are the warm ones: terracotta, burnt sienna, mustard yellow, warm ochre, sand beige and deep umber. Olive green, forest green and warm grey are SUPPORTING only and must never become the overall cast - if the result reads sage, olive, khaki or grey overall, it is wrong.
ACCENT COLOR: vermilion red must actually appear on at least one small object (a candle, a flame, a small prop), but stays SMALL - never on tablecloths, walls, ceilings, backgrounds, large panels or furniture, which take terracotta, mustard, beige or olive instead.
Overall mood: warm, muted, slightly faded like a risograph print."""

PALETTE_NIGHT = """NIGHT PALETTE: deep indigo, ink navy, charcoal plum, slate blue-grey, with warm amber and pale gold as the light sources. Stage or street lighting is reinterpreted as flat amber and gold paper shapes, NOT as neon glow, NOT as purple-magenta wash, NOT as light bleed or lens flare. No saturated cyan, no hot pink, no RGB screen colors. Overall mood: quiet, warm-in-the-dark, like a hand-printed gig poster.
ACCENT COLOR RULE - STRICT: warm amber/gold is the only accent and may cover at most 20% of the artwork, concentrated in small light shapes. Everything else stays in the dark blue-plum range."""

# v3.3.2 增补：跨枚色彩分布。v34 p1 实拍暴露「6 枚里 4 枚全落在黄/金/米色、
# 金属勺被染成金黄、整版发闷」，根因是缺少跨枚层面的配色约束。只增不改，
# 不触碰 STYLE / STYLE_REMINDER / THICKNESS / NEGATIVE 的既有措辞。
COLOR_SPREAD = """PALETTE SPREAD ACROSS THE ELEMENTS: the elements together must read as a high-contrast set - never let them all land in the same yellow / gold / beige hue range. Keep each object's own local colour from the photograph: metal stays cool grey-silver, white or cream objects stay off-white, and nothing gets dyed warm yellow just to match the palette. At least one element must be a DEEP DARK anchor (deep umber, ink brown or deep red) and at least one a COOL note (forest green, olive or slate blue-grey)."""

# ── 人物两档 ──────────────────────────────────────────────────────────────
# 档 A silhouette：单色深色剪影。最保守，绝无肖像争议，但视觉上"没有皮肤和衣服颜色"。
# 档 B collage（默认）：分色剪纸拼贴人物。仍然完全无脸，但头发/皮肤/上衣/下装各是
#     一块独立的平涂彩纸，块间留暖白纸缝 —— 这才是"水粉剪纸拼贴"的本来做法。
#     无脸保留了绝大部分"像不像"与肖像合规的安全性，同时把颜色还给人物。

FIGURE_SILHOUETTE = """HUMAN FIGURES (only for the elements that contain people): Every human figure is a SINGLE FLAT SILHOUETTE in ONE dark opaque color (%(dark)s). Completely faceless: no eyes, no mouth, no nose. No skin tone of any kind - no beige, tan, yellow or pink on any human. Hair, clothing and body are the SAME single dark color as one solid shape. Only the outline carries identity."""

FIGURE_COLLAGE = """HUMAN FIGURES (only for the elements that contain people): build each person from SEVERAL SEPARATE PIECES of flat painted paper, never one single dark blob. HAIR: one solid dark shape (%(hair)s). SKIN (face, neck, hands, arms): one flat stylised warm tone (%(skin)s), a painted paper colour with no blush, shading, gradient or highlight. UPPER GARMENT: one flat palette colour (%(top)s). LOWER GARMENT or remaining clothing: a CLEARLY DIFFERENT flat palette colour (%(bottom)s). Leave a thin warm off-white paper seam where two pieces meet.
FACE - STRICT: one clean flat skin-coloured shape, COMPLETELY FEATURELESS - no eyes, eyebrows, mouth, nose, ears, glasses or facial lines. Identity comes only from hair shape, posture and clothing colour.
Keep every piece large and chunky; hands are simple rounded mitten shapes with no individual fingers, and no thin strips anywhere."""

FIGURE_COLORS_DAY = {
    "dark": "deep umber",
    "hair": "deep umber, almost black-brown",
    "skin": "pale sand beige with a faint terracotta warmth",
    "top": "olive green or mustard yellow",
    "bottom": "warm grey or deep forest green",
}
FIGURE_COLORS_NIGHT = {
    "dark": "ink navy",
    "hair": "ink navy, almost black",
    "skin": "muted warm amber-beige, as if dimly side-lit",
    "top": "deep indigo or charcoal plum",
    "bottom": "slate blue-grey",
}

NO_FIGURE = """NO HUMAN FIGURES: This photograph has no people in it. Do NOT invent, add or imagine any human figure, silhouette, face, hand or body part anywhere in the artwork. Every element is an object, a building detail or a natural form."""

# ⚠️ 「不许靠删物品来满足粗度要求」是 v1.3 的核心修复，压缩措辞时必须保留这半句
THICKNESS = """MANUFACTURING RULE - THICKNESS: this sheet is machine cut, so no stroke may fall below the die-cut minimum - thicken every too-thin shape into a chunky solid form with blunt rounded corners instead of deleting the object, and only these auxiliary parts may be dropped: %s."""

COMPLIANCE = """MUST OMIT - copyright safety: no brand logos, sponsor decals, team liveries, wordmarks, screen graphics, poster or album artwork, cartoon characters or any other recognizable third-party IP. Specifically remove: %s - replace each with a plain flat painted colour block. Render NO readable text, letterforms, numbers or watermarks anywhere."""

# ⚠️ 元素构成配比 —— 解决"全是人物剪影"的关键
# 「不许把切片补成整只」的形状忠实度约束也放这里：客户实拍中三角蛋糕被泛化成圆蛋糕。
COMPOSITION = """ELEMENT COMPOSITION - MANDATORY, THIS IS THE MOST IMPORTANT RULE:
Produce exactly %(n)d separate sticker elements: %(nobj)d STANDALONE OBJECT stickers, each a single object cut out alone with NO person, NO scenery, NO stage, NO floor, NO background, plus exactly %(nppl)d element(s) that may contain a human figure (0 means none anywhere). One object per element, exactly these: %(objs)s.
DRAW EVERY LISTED OBJECT - NO SUBSTITUTION, NO OMISSION: each keeps the exact noun it is listed as; never drop, swap, duplicate or merge one. Draw small or delicate objects BIGGER and CHUNKIER.
SHAPE FIDELITY: keep the real shape, proportion and orientation the object has in the photograph. Never complete a partial or cut item into a whole one - a slice of cake stays a wedge, never becomes a whole round cake.
NO DUPLICATES, NO CONTAINERS: cut each object off the table, floor, stage or shelf it rested on. A vessel that genuinely belongs to it may stay (a cake slice keeps its small plate), but must not appear again on its own. No two elements may look alike."""

# 纪念物保护：即使偏细也必须加粗保留，不能被 THICKNESS 规则误删
KEEPSAKE = """MUST KEEP - these objects are the whole reason the customer ordered and each MUST appear as its own sticker: %s. Being small or slender in the photo is a reason to draw them LARGER and THICKER, never to omit or replace them."""


EXCLUDE = """ALREADY DRAWN ON OTHER SHEETS - DO NOT REPEAT: the following subjects have already been drawn as stickers on other sheets of this same order: %s.
You must NOT draw any of them again, and you must NOT include them as part of another element.
In particular, if one of your assigned objects normally sits on, in or next to one of those subjects, draw your object COMPLETELY ALONE - separated from it, not together with it on the same plate, tray, stand or surface.
Every element on this sheet must be visibly different from that list at a glance."""

LAYOUT = """LAYOUT: a sticker sheet on plain solid pure white, the %(n)d elements in a %(cols)d-column by %(rows)d-row grid. Leave a white gap between neighbours of at least one third of the larger element's width - much wider than looks natural - and an empty white margin of at least one tenth of the sheet width on all four edges. Each element is a compact chunky shape alone inside its own cell, never touching or overlapping. No connecting lines, frame, border, caption, numbering or cast shadow."""

# 只保留其它段落没说过的禁项：笔触/写实/文字/过细 分别由 STYLE、COMPLIANCE、
# THICKNESS 各说一次，这里不再重复，避免印刷约束堆叠稀释风格段权重。
NEGATIVE = """DO NOT: black outlines, black keylines, 3D render, plastic surface, airbrush, dry brush streaks, neon glow, lens flare, bokeh, pure white cut-out borders"""

HEAD = """Reinterpret this photograph as a set of die-cut stickers in the illustration style below. Keep the recognizable subjects, but do NOT copy the photo's lighting or exact colours - MAP each real colour onto its nearest palette colour below (a red candle stays red, a wooden table becomes terracotta, a green wall becomes olive). Never wash the picture into one single hue."""

# 长 prompt 里排在最前的风格段容易被后面成堆的约束条款稀释，
# 收尾再压一次风格（近因效应）—— 实拍中「质感变软、颜色发灰」就是被稀释掉的。
STYLE_REMINDER = """FINAL STYLE CHECK - the artwork must read as: flat solid colour blocks, crisp cut-paper silhouette, minimal internal texture. Every piece is one clean opaque shape of gouache-painted paper with a warm cream cut edge. Rich saturated earthy pigment - NOT pale, NOT washed out, NOT brush-streaked, NOT soft-blended, NOT a uniform sage-green cast."""

GRID = {4: (2, 2), 5: (2, 3), 6: (2, 3), 7: (2, 4), 8: (2, 4), 9: (3, 3)}

def plan_mix(info, n):
    """
    决定「纯物品 : 人物」配比。
    人物贴纸对手账的可搭配性远低于物品件，因此人物只保留 1 枚；
    照片里本来没有人时，绝不能凭空造人 —— 全部出物品。
    """
    ppl = info.get("people_count") or 0
    n_ppl = 0 if ppl == 0 else 1
    return max(1, n - n_ppl), n_ppl


def _norm(s):
    return " ".join(str(s).strip().lower().replace("-", " ").split())

# 同类词库兜底：G0 漏标 similar_pairs 时，靠关键词把同族物品折叠成一枚。
# 简化成扁平剪纸后，同族物品的轮廓几乎无法区分，并排放就是肉眼可见的重复。
#
# ⚠️ 这里折叠的不只是「同类物品」，还有【部件 ↔ 整体】关系。
#    2026-08-30 · 06 银杏实拍：ginkgo tree / tree branch / tree trunk 三枚同时入选，
#    三枚都是同一棵树的一部分，扁平剪纸下就是三块相似的褐色形状。
#    过度折叠导致候选不足不是问题 —— select_objects 第 ④ 步的分层兜底会回填。
SIMILAR_FAMILIES = [
    {"guitar", "bass", "ukulele"},
    {"drum", "snare", "tom", "kick"},
    {"cymbal", "hi hat", "hihat"},
    {"cup", "glass", "mug", "tumbler"},
    {"plate", "dish", "saucer"},
    {"spoon", "fork", "knife", "cutlery"},
    {"speaker", "amplifier", "amp", "monitor"},
    {"lamp", "light", "lantern", "bulb"},
    {"phone", "smartphone", "camera"},
    {"chair", "stool", "seat", "bench"},
    # 树体：整棵树与它的枝/干/树冠/叶簇是部件-整体关系，最多留 1 枚。
    # （原来拆成 {leaf...} 和 {trunk, branch...} 两族，所以 tree+branch+trunk 全过）
    {"tree", "trees", "treetop", "sapling", "trunk", "branch", "branches", "bough",
     "boughs", "twig", "twigs", "limb", "canopy", "crown", "foliage",
     "leaf", "leaves", "leafage", "cluster", "frond"},
    # 花：花朵与花瓣/花蕊/花茎同样是部件-整体关系
    {"flower", "flowers", "blossom", "blossoms", "bloom", "petal", "petals",
     "stem", "stalk", "bud", "floret"},
    # 建筑构件：屋顶/屋檐/墙面/立柱/横梁都是同一栋建筑的部件，
    # 平涂剪纸后就是几块相似的大色块（03 故宫实拍多次出现两枚建筑构件）
    {"roof", "rooftop", "eave", "eaves", "cornice", "gable", "ridge", "rafter",
     "wall", "facade", "parapet", "pillar", "pillars", "column", "colonnade",
     "beam", "balustrade"},
    {"stone", "brick", "block", "rock", "slab"},
    {"window", "lattice", "shutter", "pane"},
    {"door", "doors", "gate", "gateway", "doorway", "archway"},
    {"bag", "backpack", "handbag", "tote", "purse"},
    # canopy 已归入树体族（树冠），这里只留真正的遮阳器具
    {"tent", "umbrella", "parasol", "awning"},
]

# 复合体拆解：整套装备天然由多根细杆支撑，画成贴纸必然卡在"结构过细 + 自带支架"，
# 无论重试多少轮都过不了质检。统一替换成该套装里体量最厚实的单件。
COMPOSITE_REPLACE = {
    "drum kit": "bass drum", "drum set": "bass drum", "drums": "bass drum",
    "drum rack": "bass drum",
    "microphone stand": "microphone", "mic stand": "microphone",
    "cymbal stand": "cymbal", "hi hat stand": "cymbal",
    "camera tripod": "camera", "tripod": "camera",
    "guitar stand": "electric guitar", "keyboard stand": "keyboard",
    "lighting rig": "stage light", "light rig": "stage light",
    "speaker stand": "speaker", "music stand": "sheet music",
}

def _decompose(name):
    """把成套装备换成单件；返回 (替换后名称, 是否发生替换)"""
    n = _norm(name)
    if n in COMPOSITE_REPLACE:
        return COMPOSITE_REPLACE[n], True
    for k, v in COMPOSITE_REPLACE.items():
        if n.endswith(" " + k) or n == k:
            return v, True
    return name, False


def _singular(word):
    """极简去复数。只在中心词本身没命中词库时兜底，所以不怕把 glass 削成 glas ——
    那种情况根本走不到这里（glass 自己就在杯族里）。
    实拍漏判：tree branches / stone slabs / petals 的中心词带 s，词库全是单数。"""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _family(name):
    """
    用【中心词】判定所属物品族，而不是子串匹配。
    英文复合名词的中心词在最后：electric guitar→guitar，bass drum→drum，
    guitar amplifier→amplifier，microphone→microphone。
    子串匹配会把 microphone 误判成 phone、guitar amplifier 误判成 guitar、
    bass drum 误判成 bass(guitar)，导致候选被过度砍光。
    """
    words = _norm(name).split()
    if not words:
        return None
    head = words[-1]
    for cand in (head, _singular(head)):
        for i, fam in enumerate(SIMILAR_FAMILIES):
            if cand in fam:
                return i
    # 中心词没命中时，再用整名做一次严格的词组匹配（如 "hi hat"）
    full = _norm(name)
    for i, fam in enumerate(SIMILAR_FAMILIES):
        if full in fam:
            return i
    return None


def select_objects(info, n_obj):
    """
    从 G0 清单里挑出 n_obj 个【互不重复、互不包含】的物品。

    取代旧的 objs[:n_obj] —— 旧写法有三个致命缺陷，已在实拍中全部复现：
      1. 纪念物被截断：生日照的 "candle" 排第 7，直接被切掉；
      2. 容器重复：cheesecake 与 plate 同时入选，蛋糕自带盘子 → 两枚重叠；
      3. 同族重复：electric guitar 与 bass guitar 同时入选 → 看起来是两把吉他。
    返回 (picked, dropped_log)
    """
    objs, seen, dropped = [], set(), []
    for o in info.get("standalone_objects") or []:
        o2, changed = _decompose(o)
        if changed:
            dropped.append("%s → 改用单件 %s（整套装备带支架细杆，模切做不了）" % (o, o2))
        k = _norm(o2)
        if k and k not in seen:
            seen.add(k); objs.append(o2)

    # ① 容器剔除：A 盛放在 B 上时，两者只留一个。
    #
    # ⚠️ 这里不能无脑「丢外层」。实拍踩过的坑：G0 把生日单标成
    #        [["birthday cake","plate"], ["candle","birthday cake"], ["sparkler","glass cup"]]
    #    第二对的语义是「蜡烛插在蛋糕上」，无脑丢外层就把【蛋糕本身】删了 ——
    #    整单最重要的纪念物没了，比留一个空盘子还糟。
    #    所以要比「价值」：容器词最低，纪念物最高；两个都是纪念物时丢里层
    #    （蜡烛本来就画在蛋糕上，蛋糕带蜡烛才是那枚经典图案）。
    _keepset = {_norm(_decompose(x)[0]) for x in info.get("keepsake_objects") or []}

    def _value(name):
        if _words(name) & CONTAINER_WORDS:
            return 0
        return 2 if _norm(name) in _keepset else 1

    contained = set()
    for pair in info.get("container_pairs") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            inner, outer = _norm(pair[0]), _norm(pair[1])
            if inner not in seen or outer not in seen:
                continue
            if inner == outer:
                contained.add(outer)
                dropped.append("%s（空容器，单独做成贴纸没有纪念价值）" % pair[1])
                continue
            vi, vo = _value(inner), _value(outer)
            if vi > vo:
                loser, winner = outer, inner
            elif vo > vi:
                loser, winner = inner, outer
            else:
                # 势均力敌：丢里层，因为里层本来就画在外层身上
                loser, winner = inner, outer
            contained.add(loser)
            dropped.append("%s（已随 %s 一起画，单独出会重复）" % (loser, winner))

    # ② 同族折叠：G0 标注的 similar_pairs + 内置词库双保险
    explicit = set()
    for pair in info.get("similar_pairs") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            a, b = _norm(pair[0]), _norm(pair[1])
            order = [_norm(x) for x in objs]
            if a in order and b in order:
                loser = pair[1] if order.index(a) <= order.index(b) else pair[0]
                explicit.add(_norm(loser))
                dropped.append("%s（与同族物品轮廓雷同）" % loser)

    # ③ 排序：纪念物置顶 → 普通物品 → 「去字后只剩空色块」的低价值件垫底。
    #    垫底而不是删除：有更好的候选就轮不到它们；候选不够时它们仍是兜底来源。
    #    低价值判定优先于纪念物判定 —— G0 有时会把匾额/横幅标成纪念物，
    #    但合规要求必须去字，去完还是一块空色块，所以以「去字后还剩什么」为准。
    keep = [k for k in (_norm(_decompose(x)[0]) for x in info.get("keepsake_objects") or [])
            if k in seen]

    def _rank(o):
        if _text_dependent(o):
            return 2
        return 0 if _norm(o) in keep else 1

    objs.sort(key=_rank)

    picked, used_fam = [], set()
    for o in objs:
        k = _norm(o)
        if k in contained or k in explicit:
            continue
        fam = _family(o)
        if fam is not None and fam in used_fam:
            dropped.append("%s（与已选物品同族，轮廓雷同）" % o)
            continue
        picked.append(o)
        if fam is not None:
            used_fam.add(fam)
        if len(picked) >= n_obj:
            break

    # ④ 兜底：若过滤太狠导致数量不足，分层放宽。
    #    容器重复项（盘子）永远不回填 —— 单独出就是废件；
    #    同族项可以回填，缺枚数比轻微雷同更糟。
    if len(picked) < n_obj:
        for o in objs:
            if o not in picked and _norm(o) not in contained:
                picked.append(o)
                if len(picked) >= n_obj:
                    break
    # 仍不足才动容器项
    if len(picked) < n_obj:
        for o in objs:
            if o not in picked:
                picked.append(o)
                if len(picked) >= n_obj:
                    break
    # 把「降权后没被选上」的低价值件如实记进日志，方便复盘为什么没有它
    _pick = {_norm(o) for o in picked}
    for o in objs:
        if _text_dependent(o) and _norm(o) not in _pick:
            dropped.append("%s（主体价值依赖文字，按合规去字后只剩一块纯色空框，"
                           "已降权，仅在候选不足时才启用）" % o)
    return picked, dropped


def build_prompt(info, n, patches=None, figure_style="collage", exclude=None):
    night = info.get("lighting") == "night"
    n_obj, n_ppl = plan_mix(info, n)
    picked, _dropped = select_objects(info, n_obj)
    n_ppl = n - len(picked)
    thin = info.get("thin_parts") or ["cables", "wires", "thin lower poles of stands", "strings"]
    ip = info.get("ip_items") or ["any brand logo or wordmark"]
    cols, rows = GRID.get(n, (2, 3))
    parts = [
        HEAD, STYLE,
        PALETTE_NIGHT if night else PALETTE_DAY,
        COLOR_SPREAD,
    ]
    if n_ppl > 0:
        cols_fig = FIGURE_COLORS_NIGHT if night else FIGURE_COLORS_DAY
        parts.append((FIGURE_SILHOUETTE if figure_style == "silhouette"
                      else FIGURE_COLLAGE) % cols_fig)
    else:
        parts.append(NO_FIGURE)
    parts += [
        THICKNESS % ", ".join(thin),
        COMPLIANCE % ", ".join(ip),
        COMPOSITION % {"n": n, "nobj": len(picked), "nppl": n_ppl,
                       "objs": "; ".join('"%s"' % o for o in picked)},
    ]
    # 纪念物保护：只对本次真正入选的 keepsake 生效
    _pk = {_norm(o) for o in picked}
    keepsakes = [k for k in (_decompose(x)[0] for x in info.get("keepsake_objects") or [])
                 if _norm(k) in _pk]
    if keepsakes:
        parts.append(KEEPSAKE % ", ".join('"%s"' % k for k in keepsakes))
    if exclude:
        parts.append(EXCLUDE % ", ".join('"%s"' % e for e in exclude))
    parts += [
        LAYOUT % {"n": n, "cols": cols, "rows": rows},
        NEGATIVE + (", skin tones on figures, multicolour figures"
                    if figure_style == "silhouette" else
                    ", people as one flat single-colour blob with no clothing colour"),
    ]
    if patches:
        parts.append("CORRECTIONS - the previous attempt failed quality control. Fix these specific problems:\n" +
                     "\n".join("- " + p for p in patches))
    parts.append(STYLE_REMINDER)
    return "\n\n".join(parts)


# ── 主视觉场景图（卡纸打印图的左半部分） ──────────────────────────────────
# 和贴纸版共用 STYLE / PALETTE / FIGURE / COMPLIANCE，只换输出规格段，
# 这样同一单里「场景图」和「贴纸」出自同一套色板和笔触，拼到一张卡纸上才不出戏。
SCENE_OUTPUT = """OUTPUT - ONE SINGLE COMPLETE SCENE:
Produce ONE single complete scene composition that re-tells this photograph as one picture - NOT a sticker sheet, NOT separate cut-out elements, NOT a grid.
Keep the setting, the layout and the atmosphere of the original photo so the customer recognises the moment at a glance: the same subject in the same place, doing the same thing.
Fill the whole frame edge to edge with the scene. Balanced composition with one clear focal point.
Build the background out of large flat cut-paper shapes as well - walls, seating, windows, sky and ground are each their own piece of painted paper.
No white margin, no frame, no border, no caption, no numbering, no separate floating objects outside the scene."""

SCENE_NEGATIVE = """DO NOT: a sticker sheet, a grid of separate objects, isolated cut-outs on a white background, black outlines, black keylines, photorealism, 3D render, gloss, airbrush, neon glow, lens flare, bokeh, gradient mesh, facial features, readable text, watermarks, brand logos."""


def build_scene_prompt(info, figure_style="collage"):
    """主视觉场景图 prompt（模块 O1）。"""
    night = info.get("lighting") == "night"
    ip = info.get("ip_items") or ["any brand logo or wordmark"]
    parts = ["Reinterpret this photograph as one single illustrated scene in the following style. "
             "Keep the recognizable subjects, the setting and the composition of the original photo. "
             "Do NOT copy the photo's lighting, and do not copy its exact colours - MAP each real "
             "colour onto its nearest colour in the palette below. Never wash the whole picture into one single hue.",
             STYLE, PALETTE_NIGHT if night else PALETTE_DAY]
    if (info.get("people_count") or 0) > 0:
        cols_fig = FIGURE_COLORS_NIGHT if night else FIGURE_COLORS_DAY
        parts.append((FIGURE_SILHOUETTE if figure_style == "silhouette"
                      else FIGURE_COLLAGE) % cols_fig)
    else:
        parts.append(NO_FIGURE)
    parts += [COMPLIANCE % ", ".join(ip), SCENE_OUTPUT, SCENE_NEGATIVE, STYLE_REMINDER]
    return "\n\n".join(parts)

# ── 步骤 3：生成 ───────────────────────────────────────────────────────────

def generate(photo_small, prompt, outdir, tag):
    """图生图。实现在 providers.py，并在那里校验输出分辨率是否够印。"""
    dst = os.path.join(outdir, "round%s.png" % tag)
    try:
        return providers.generate_image(photo_small, prompt, dst)
    except providers.ProviderError as e:
        raise SystemExit("❌ %s" % e)

# ── 步骤 3.5：程序化重排（排版不靠模型） ──────────────────────────────────

def do_relayout(png, outdir, tag, gap, margin, dpi):
    """
    调 print-ready-doctor/relayout.py 把这一轮的出图重排到干净 A5 画布。
    返回 (重排后的图路径 或 None, 摘要dict, 日志)。
    失败不静默：返回 None，主流程会退回用原图继续质检并在报告里记一笔。
    """
    name = "round%s_relayout" % tag
    r = subprocess.run([sys.executable, RELAYOUT, os.path.abspath(png),
                        "--in-width", str(SHEET_WIDTH_MM),
                        "--sheet-width", str(SHEET_WIDTH_MM),
                        "--sheet-height", str(SHEET_HEIGHT_MM),
                        "--dpi", str(dpi), "--gap", str(gap), "--margin", str(margin),
                        "--outdir", os.path.abspath(outdir), "--name", name],
                       capture_output=True, text=True, cwd=os.path.dirname(RELAYOUT))
    log_txt = r.stdout + r.stderr
    dst = os.path.join(outdir, name + ".png")
    meta_p = os.path.join(outdir, name + ".json")
    if r.returncode != 0 or not os.path.exists(dst):
        return None, {}, log_txt
    meta = {}
    if os.path.exists(meta_p):
        try:
            with open(meta_p, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            # 不静默：重排图本身是好的，但报告里会缺邻距/边距实测值，要让人看见
            log("  ⚠️ 重排元数据 %s 读不出来（%s），报告中该轮实测值将缺失"
                % (os.path.basename(meta_p), e))
            meta = {}
    return dst, meta, log_txt


# ── 步骤 4：双重质检 ───────────────────────────────────────────────────────

def qc_quant(png):
    """量化体检：print-ready-doctor。这一关目视看不出来，必须跑。"""
    r = subprocess.run([sys.executable, DOCTOR, os.path.abspath(png),
                        "--sheet-width", str(SHEET_WIDTH_MM), "--report-only"],
                       capture_output=True, text=True, cwd=os.path.dirname(DOCTOR))
    txt = r.stdout + r.stderr
    res = {"raw": txt, "dpi": None, "must_fix": None, "n_elem": None, "n_cut": None, "min_gap": None}
    m = re.search(r"(\d+)\s*dpi", txt);                        res["dpi"] = int(m.group(1)) if m else None
    m = re.search(r"必须修\s*(\d+)\s*枚", txt);                 res["must_fix"] = int(m.group(1)) if m else None
    m = re.search(r"(\d+)\s*枚元素\s*→\s*刀线轮廓\s*(\d+)\s*个", txt)
    if m: res["n_elem"], res["n_cut"] = int(m.group(1)), int(m.group(2))
    gaps = [float(g) for g in re.findall(r"邻距\s*([\d.]+)mm", txt)]
    res["min_gap"] = min(gaps) if gaps else None
    fails = []
    if res["dpi"] is not None and res["dpi"] < 300:
        fails.append("有效分辨率仅 %d dpi，低于 300 dpi 印刷线" % res["dpi"])
    if res["must_fix"]:
        fails.append("量化体检必须修 %d 枚" % res["must_fix"])
    if res["n_elem"] and res["n_cut"] and res["n_elem"] != res["n_cut"]:
        fails.append("元素数 %d ≠ 刀线轮廓数 %d，判定粘连" % (res["n_elem"], res["n_cut"]))
    if res["min_gap"] is not None and res["min_gap"] < 3.0:
        fails.append("最小邻距仅 %.2fmm，全切会粘连（需 ≥3mm）" % res["min_gap"])
    res["fails"] = fails
    return res

VISUAL_TASK = """你是定制贴纸的印前质检员。这是一张贴纸排版稿，要求共 %(n)d 枚独立元素，其中至少 %(nobj)d 枚必须是【纯物品】贴纸（画面里只有单个物品，没有人、没有场景背景）。%(ppl)s
只输出一个 JSON 对象，不要解释文字：
{
  "n_elements": 整数,
  "n_object_only": 整数,          // 纯物品贴纸枚数（只有物品，无人、无场景）
  "object_names": ["..."],        // 每枚纯物品贴纸画的是什么
  "n_with_people": 整数,
  "invented_people": true/false,   // 若上文说明"不允许出现人物"，画面中是否仍出现了人物/剪影/人体部位
  "figures_ok": true/false,       // %(figrule)s
  "figures_note": "不合格时说明具体位置",
  "black_outline": true/false,    // 是否出现黑色描边勾线
  "accent_ratio_pct": 整数,       // 高饱和焦点色占画面面积百分比
  "warm_white_border": true/false,// 每枚元素外圈是否有可见暖白(米白)手切边，纯白则 false
  "matte_paper": true/false,      // 是否哑光水粉纸感（非光面塑料/3D/渐变）
  "thin_parts": ["..."],          // 仍然过于纤细、模切会断的结构。没有则空数组
  "duplicate_pairs": [["A","B"]], // 【重复检查】画面中任意两枚贴纸，如果画的是同一个物品、或同族物品（两把吉他、两个鼓、两个杯子）、
                                  // 或简化后轮廓几乎一样，就把这一对写进来。没有则空数组
  "objects_with_container": ["..."], // 【容器检查】哪些纯物品贴纸里除了物品本体，还画进了它下面的盘子/托盘/桌面/支架/底座。没有则空数组
  "missing_objects": ["..."],     // 【缺失检查】下面这份清单里，哪些物品在画面中【完全找不到对应贴纸】：%(expect)s
                                  // 只填确实没画的。名称照抄清单里的英文
  "readable_text_or_logo": true/false,
  "text_note": "如有请写出看到的文字或logo",
  "scenery_inside_object_stickers": true/false  // 纯物品贴纸里是否混进了背景场景
}"""

FIGRULE_SILHOUETTE = ("人物是否均为单一深色不透明剪影、完全无脸、无任何肤色。"
                     "有肤色或五官则 false")
FIGRULE_COLLAGE = ("人物是否【完全无脸】（无眼睛/眉毛/嘴/鼻子/眼镜）。"
                   "只要出现任何五官就是 false。"
                   "注意：人物有皮肤色块、有衣服颜色、由多块平涂色纸拼成，都是【正确】的，不要因此判 false")


def count_fails(d, n, n_obj, n_ppl):
    """
    枚数硬门禁。单独抽成函数是为了能离线回归测试（不花生图钱）。

    d = 目视质检返回的 JSON；n = 目标总枚数；n_obj = 纯物品枚数；n_ppl = 含人物枚数。

    历史缺陷（2026-08-30 · 02 演出现场实拍）：qc_visual 只判 `n_object_only >= n_obj`，
    模型漏画人物那一枚时总数只有 5 枚，而 5 >= 5 成立，于是判「双重质检全过」
    并直接交付了 5 枚的成品。少一枚是客户一眼能看出来的硬伤，必须是硬门禁。

    两种构成都要能过：有人的照片 = n_obj 物品 + 1 人物；无人的照片 = n 枚全物品
    （03 故宫就是纯物品 6 枚，合规，不能误拦）。
    """
    fails = []
    comp = ("%d 枚纯物品 + %d 枚含人物" % (n_obj, n_ppl) if n_ppl
            else "%d 枚纯物品（这张照片没有人，不允许出现人物）" % n_obj)
    tot = d.get("n_elements")
    if not isinstance(tot, int):
        fails.append("目视质检没给出总枚数（n_elements=%r），无法确认成品是 %d 枚 —— "
                     "按不合格处理：枚数未经确认的稿件不允许交付" % (tot, n))
    elif tot != n:
        fails.append("整版共 %d 枚独立元素，不等于目标 %d 枚（应为 %s）—— "
                     "必须重画成刚好 %d 枚：缺的补齐，多的合并或去掉"
                     % (tot, n, comp, n))
    if n_ppl > 0 and isinstance(d.get("n_with_people"), int) and d["n_with_people"] < 1:
        fails.append("照片里有人，但一枚含人物的元素都没有（应为 %s）—— "
                     "缺的正是人物那一枚，必须补画" % comp)
    return fails


def qc_visual(png, workdir, n, n_obj, tag, n_ppl=1, figure_style="collage", expected=None):
    expected = expected or []
    small = shrink(png, os.path.join(workdir, "qc%s.jpg" % tag), box=1400)
    txt = call_vision([small], VISUAL_TASK % {
        "n": n, "nobj": n_obj,
        "expect": "; ".join('"%s"' % e for e in expected) or "（未指定）",
        "figrule": FIGRULE_SILHOUETTE if figure_style == "silhouette" else FIGRULE_COLLAGE,
        "ppl": ("其中允许有 %d 枚含人物。" % n_ppl) if n_ppl else
               "这张照片原本没有人，因此画面中不允许出现任何人物、剪影或人体部位。"})
    d = grab_json(txt) or {}
    fails = []
    # 拿不到 JSON 绝不能当「没发现问题」：早期实现里 d={} 会让下面所有判定
    # 全部跳过，结果是视觉模型一抽风就直接判「全过」并交付。
    if not d:
        fails.append("目视质检没有返回可解析的 JSON（视觉模型响应异常），本轮判定无效 —— "
                     "按不合格处理并重试")
    else:
        fails += count_fails(d, n, n_obj, n_ppl)
    if d.get("n_object_only") is not None and d["n_object_only"] < n_obj:
        fails.append("纯物品贴纸仅 %d 枚，少于要求的 %d 枚 —— 元素过于单一，缺少可自由搭配的装饰件。"
                     "必须把其中几枚改成【单个物品、无人、无场景】的独立贴纸"
                     % (d["n_object_only"], n_obj))
    if n_ppl == 0 and (d.get("invented_people") or (d.get("n_with_people") or 0) > 0):
        fails.append("原照片没有人，但画面中凭空生成了人物/剪影 —— 必须全部替换为物品或建筑细节元素")
    if n_ppl > 0 and d.get("figures_ok") is False:
        # figures_note 经常是空串，直接拼进去会得到「不合格：。」这种句子，
        # 而这些文案是原样喂回模型当重试指令的，标点错乱会稀释指令
        _note = str(d.get("figures_note") or "").strip() or "视觉模型未说明具体位置"
        if figure_style == "silhouette":
            fails.append("人物剪影不合格：%s。人物必须是单一深色不透明无脸剪影，禁止任何肤色和五官"
                         % _note)
        else:
            fails.append("人物出现了五官：%s。人物的脸必须是一整块平涂的无五官色块 —— "
                         "去掉眼睛、眉毛、嘴、鼻子、眼镜，但保留皮肤色块和衣服配色"
                         % _note)
    if d.get("black_outline"): fails.append("出现黑色描边，必须完全去掉黑色勾线")
    ar = d.get("accent_ratio_pct")
    if isinstance(ar, int) and ar > 15:
        fails.append("焦点色占比约 %d%%，超过 15%% —— 大面积载体（桌布/墙面/天花板/背景）一律改用大地色" % ar)
    if d.get("warm_white_border") is False:
        fails.append("剪纸边缘不是暖白而是纯白 —— 必须是可见的米白/奶油白，否则印厂会当留白去掉")
    if d.get("matte_paper") is False: fails.append("质感偏光面/3D/渐变，必须回到哑光水粉纸感")
    if d.get("readable_text_or_logo"):
        fails.append("仍有可读文字或品牌标识（%s），必须全部替换为纯色块"
                     % (str(d.get("text_note") or "").strip() or "视觉模型未写明内容"))
    if d.get("scenery_inside_object_stickers"):
        fails.append("纯物品贴纸里混进了背景场景，物品贴纸必须只有物品本体")
    if d.get("thin_parts"):
        fails.append("以下结构仍过细，必须加粗（禁止删除物品本体）：%s" % ", ".join(d["thin_parts"]))
    # 重复 / 容器 / 缺失 —— 定向重试，不合格必须重出
    dup = [p for p in (d.get("duplicate_pairs") or [])
           if isinstance(p, (list, tuple)) and len(p) == 2]
    if dup:
        fails.append("出现重复元素：%s。同一张贴纸上不能有两枚画同一物品或同族物品（两把吉他、两个杯子）。"
                     "必须把其中一枚换成清单里【完全不同类别】的物品，两枚轮廓要一眼就能区分"
                     % "、".join("%s 与 %s" % (p[0], p[1]) for p in dup))
    # ⚠️ 容器判定必须区分两种情况，早期版本一刀切，把好看的图判废了：
    #   (a) 蛋糕连着自己那只小盘子 —— 参考稿里就是这样，比光秃秃一块蛋糕好看得多，放行；
    #   (b) 蛋糕带盘子，同时另一枚贴纸又是那只盘子 —— 这才是顾客投诉的「重复的盘子」，判废；
    #   (c) 物品下面拖着桌面/地面/舞台这类大面积承载物 —— 贴纸剪不出来，判废。
    _BIG_SURFACES = {"table", "tabletop", "desk", "counter", "floor", "ground",
                     "stage", "surface", "shelf", "wall"}
    with_c = [c for c in (d.get("objects_with_container") or []) if str(c).strip()]
    if with_c:
        big = [c for c in with_c if _words(c) & _BIG_SURFACES]
        if big:
            fails.append("以下贴纸把桌面/地面/舞台这类大面积承载物也画进去了：%s。"
                         "模切剪不出这种底座，必须让物品单独悬空呈现" % ", ".join(big))
        # 「容器又被单独画了一枚」这种真重复，已经在 select_objects 阶段就被
        # 堵死了（盘子根本不会进物品清单），这里不必再判一次。
        # 早期版本在这里补了一刀，结果视觉模型每轮都把「蛋糕连着自己的小盘子」
        # 报成容器问题，三轮全废 —— 判废的恰恰是参考稿里最好看的那版。
        # 真正的重复交给 duplicate_pairs 兜，这里只管大面积承载物。
    miss = [m for m in (d.get("missing_objects") or []) if str(m).strip()]
    if miss:
        fails.append("以下指定物品没有画出来：%s。这些是顾客照片里的纪念物，必须补画成独立贴纸；"
                     "若因为太细而被省略，请加粗放大后重画，不得用其它物品替代"
                     % ", ".join(miss))
    d["fails"] = fails
    return d

# ── 主流程 ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("photo")
    ap.add_argument("--outdir", default="forge_out")
    ap.add_argument("--elements", type=int, default=6)
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--no-relayout", action="store_true",
                    help="关掉程序化重排，用模型自己的排版（旧行为，仅用于对比）")
    ap.add_argument("--gap", type=float, default=8.0, help="重排后元素最小净距 mm")
    ap.add_argument("--margin", type=float, default=10.0, help="重排后四边最小留白 mm")
    ap.add_argument("--figure-style", choices=["collage", "silhouette"], default="collage",
                    help="人物画法：collage=分色剪纸拼贴（有肤色/衣服色，无脸，默认）；"
                         "silhouette=单色深色剪影")
    ap.add_argument("--dpi", type=int, default=400, help="重排后成品分辨率")
    ap.add_argument("--objects", default=None,
                    help="显式指定这一批要画的物品清单（分号分隔），跳过 G0 自动选物。"
                         "供 forge_a3.py 分批调度使用，保证 4 批之间不重复")
    ap.add_argument("--exclude", default=None,
                    help="其它批次已画的图案（分号分隔）。本批禁止重复，也禁止把它们"
                         "作为本批物品的一部分画进来（避免多枚都出现同一个甜点盘）")
    ap.add_argument("--allow-people", choices=["auto", "no"], default="auto",
                    help="no = 这一批不出人物（分批时只让其中一批出人物，避免 4 批都有人）")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    work = os.path.join(a.outdir, "_work"); os.makedirs(work, exist_ok=True)

    log("═" * 66)
    log("memory-sticker-forge  ·  %s  ·  目标 %d 枚元素" % (os.path.basename(a.photo), a.elements))
    log("provider: %s  |  人物画法: %s" % (providers.describe(), a.figure_style))
    log("═" * 66)

    # G0
    log("\n【G0】照片体检 + 场景解析")
    info, small = preflight(a.photo, work)
    n_obj, n_ppl = plan_mix(info, a.elements)
    log("  像素      : %s（短边 %dpx）" % (info.get("_size_px"), info.get("_short_edge_px") or 0))
    log("  场景     : %s" % info.get("scene"))
    log("  光线      : %s  → %s色板" % (info.get("lighting"), "夜场" if info.get("lighting") == "night" else "日间"))
    log("  人数      : %s" % info.get("people_count"))
    log("  可独立物品 : %s" % ", ".join(info.get("standalone_objects", [])))
    log("  纤细件     : %s" % (", ".join(info.get("thin_parts", [])) or "无"))
    log("  第三方 IP  : %s" % (", ".join(info.get("ip_items", [])) or "无"))

    # 分批调度：用外部指定的物品清单覆盖自动选物，并可强制本批不出人物
    if a.objects:
        forced = [o.strip() for o in a.objects.split(";") if o.strip()]
        info["standalone_objects"] = forced
        log("  ▸ 本批指定物品 : %s" % ", ".join(forced))
    if a.allow_people == "no":
        info["people_count"] = 0
        log("  ▸ 本批不出人物（人物额度已分配给其它批次）")
    n_obj, n_ppl = plan_mix(info, a.elements)

    picked, dropped = select_objects(info, n_obj)
    # ⚠️ 必须按【真正入选的枚数】重算配比：build_prompt 内部就是按 len(picked) 写
    #    prompt 的（n_ppl = n - len(picked)）。选品兜底后 picked 可能不等于 n_obj，
    #    不重算的话质检会拿着「5 物品 + 1 人物」去校验一张实际是「4 物品 + 2 人物」
    #    的版面，枚数门禁跟着一起错。
    n_obj, n_ppl = len(picked), a.elements - len(picked)
    log("  ▸ 本版选中 : %s" % ", ".join(picked))
    log("  ▸ 版面构成 : %d 枚纯物品 + %d 枚含人物 = %d 枚" % (n_obj, n_ppl, a.elements))
    if info.get("keepsake_objects"):
        log("  ▸ 纪念物   : %s（强制保留，不得省略）" % ", ".join(info["keepsake_objects"]))
    for dp in dropped:
        log("  ▸ 已剔除   : %s" % dp)
    if info.get("_rescued_from_thin"):
        log("  ▸ 已从纤细件中救回主体物品 : %s" % ", ".join(info["_rescued_from_thin"]))

    ok, blocks, warns = g0_gate(info, a.elements)
    for w in warns: log("  🟡 %s" % w)
    for b in blocks: log("  🔴 %s" % b)
    with open(os.path.join(a.outdir, "preflight.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    if not ok:
        log("\n❌ G0 拒稿：不建议接这张照片。（未消耗任何生成额度）")
        return 1
    log("  ✅ G0 通过")
    if a.preflight_only:
        log("\n（--preflight-only，未生成）")
        return 0

    patches, history = [], []
    for rnd in range(1, a.max_rounds + 1):
        log("\n【第 %d 轮】生成 → %s双重质检"
            % (rnd, "" if a.no_relayout else "程序化重排 → "))
        prompt = build_prompt(info, a.elements, patches, a.figure_style,
                              [e.strip() for e in a.exclude.split(';') if e.strip()] if a.exclude else None)
        with open(os.path.join(a.outdir, "prompt_round%d.txt" % rnd), "w",
                  encoding="utf-8") as f:
            f.write(prompt)
        png = generate(small, prompt, a.outdir, str(rnd))
        log("  出图 : %s" % os.path.basename(png))

        # 排版交给代码，不交给模型。重排失败才退回模型原图。
        sheet, rmeta = png, {}
        if not a.no_relayout:
            rp, rmeta, rlog = do_relayout(png, a.outdir, str(rnd), a.gap, a.margin, a.dpi)
            if rp:
                sheet = rp
                log("  重排 : %s → %s 枚，%s列×%s行，实测最小邻距 %.2fmm / 最小边距 %.2fmm"
                    % (os.path.basename(rp), rmeta.get("n_elements"),
                       rmeta.get("cols"), rmeta.get("rows"),
                       rmeta.get("min_gap_mm", 0), rmeta.get("min_margin_mm", 0)))
            else:
                log("  🔴 重排失败，退回模型原图继续质检：\n%s" % rlog[-500:])

        q = qc_quant(sheet)
        log("  量化 : %s dpi | 必须修 %s | 元素 %s/刀线 %s | 最小邻距 %s mm"
            % (q["dpi"], q["must_fix"], q["n_elem"], q["n_cut"], q["min_gap"]))
        v = qc_visual(sheet, work, a.elements, n_obj, str(rnd), n_ppl, a.figure_style,
                      expected=picked)
        log("  目视 : 共 %s 枚 | 纯物品 %s 枚 (%s)"
            % (v.get("n_elements"), v.get("n_object_only"), ", ".join(v.get("object_names", [])[:8])))

        fails = q["fails"] + v["fails"]
        history.append({"round": rnd, "png": os.path.basename(sheet),
                        "relayout": ({k: rmeta.get(k) for k in
                                      ("n_elements", "cols", "rows", "min_gap_mm",
                                       "min_margin_mm", "shrink_k")} if rmeta else None),
                        "quant": {k: q[k] for k in
                        ("dpi", "must_fix", "n_elem", "n_cut", "min_gap")}, "visual": v, "fails": fails})
        if not fails:
            log("  ✅ 双重质检全过")
            final = os.path.join(a.outdir, "FINAL.png"); shutil.copy(sheet, final)
            prod = os.path.join(a.outdir, "production")
            rp2 = subprocess.run([sys.executable, DOCTOR, os.path.abspath(final),
                                  "--sheet-width", str(SHEET_WIDTH_MM),
                                  "--outdir", os.path.abspath(prod)],
                                 capture_output=True, text=True, cwd=os.path.dirname(DOCTOR))
            write_report(a.outdir, info, history, rnd, True)
            log("\n🎉 交付：%s（第 %d 轮通过）" % (final, rnd))
            # 这一步以前不看返回码：刀线导出失败时照样打印「生产文件：cutline.svg」，
            # 直到把不存在的文件发给工厂才发现。现在失败就明说。
            cut = os.path.join(prod, "cutline.svg")
            if os.path.isfile(cut):
                log("   生产文件：%s" % cut)
            else:
                log("   🔴 刀线导出失败（doctor 退出码 %s），生产文件未生成，先别送厂：\n%s"
                    % (rp2.returncode, (rp2.stdout + rp2.stderr)[-500:]))
            log("   凑满 4 单后拼 A3：python3 ../print-ready-doctor/impose_a3.py "
                "单1/FINAL.png 单2/FINAL.png 单3/FINAL.png 单4/FINAL.png --outdir a3_out/")
            return 0

        log("  🔴 不合格 %d 项：" % len(fails))
        for f in fails: log("     · %s" % f)
        patches = fails

    log("\n❌ %d 轮仍未通过，不交付。见 qc_report.md" % a.max_rounds)
    write_report(a.outdir, info, history, a.max_rounds, False)
    return 2

def write_report(outdir, info, history, rounds, passed):
    L = ["# 质检报告\n",
         "**结果**：%s（共 %d 轮）\n" % ("✅ 通过并交付" if passed else "🔴 未通过，未交付", rounds),
         "## G0 照片体检\n",
         "| 项 | 值 |", "|---|---|",
         "| 场景 | %s |" % info.get("scene"),
         "| 光线判定 | %s |" % info.get("lighting"),
         "| 人数 | %s |" % info.get("people_count"),
         "| 可独立物品 | %s |" % ", ".join(info.get("standalone_objects", [])),
         "| 第三方 IP（已强制剔除） | %s |" % (", ".join(info.get("ip_items", [])) or "无"),
         "\n## 逐轮结果\n"]
    for h in history:
        L.append("### 第 %d 轮 · %s\n" % (h["round"], h["png"]))
        r = h.get("relayout")
        if r:
            L.append("- 程序化重排：%s 枚 → %s列×%s行，实测最小邻距 **%s mm**，最小边距 **%s mm**"
                     "（收缩系数 %s）"
                     % (r.get("n_elements"), r.get("cols"), r.get("rows"),
                        r.get("min_gap_mm"), r.get("min_margin_mm"), r.get("shrink_k")))
        else:
            L.append("- 程序化重排：未启用（--no-relayout）或执行失败，排版沿用模型出图")
        q = h["quant"]
        L.append("- 量化：%s dpi ｜ 必须修 %s ｜ 元素 %s / 刀线 %s ｜ 最小邻距 %s mm"
                 % (q["dpi"], q["must_fix"], q["n_elem"], q["n_cut"], q["min_gap"]))
        v = h["visual"]
        L.append("- 目视：共 %s 枚，纯物品 %s 枚（%s）"
                 % (v.get("n_elements"), v.get("n_object_only"), ", ".join(v.get("object_names", [])[:8])))
        if h["fails"]:
            L.append("- 🔴 不合格项：")
            L += ["  - %s" % f for f in h["fails"]]
        else:
            L.append("- ✅ 双重质检全过")
        L.append("")
    with open(os.path.join(outdir, "qc_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))

if __name__ == "__main__":
    sys.exit(main())
