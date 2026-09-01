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
    python3 forge.py <photo> --fresh               # 忽略断点，强制从 G0 重头跑

断点续跑（v3.5.0 · 2026-09-02）
--------------------------------
同一条命令重跑同一个 --outdir 时，默认【复用】已有产物，不重复烧额度：
    · preflight.json 存在且有效 → 直接复用 G0，不再调用视觉模型
    · roundN 的 prompt 与上次逐字节相同且产物在 → 复用生图/重排/质检结果
复用了什么会在日志里逐条打印，并在收尾汇总，不会让人误以为是重新跑的。
要强制从头跑用 --fresh（等价别名 --no-resume）。

退出码：0 = 交付合格稿；1 = G0 拒稿；2 = 达到重试上限仍不合格；
        3 = provider 自检/能力不达标；4 = provider 生图失败（已存断点，可续跑）
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time

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
  "ip_items": ["..."],                    // 【🔴 绝对不碰】所有品牌logo/字标/商标文字、赞助商字样、产品字标、大屏画面内容、海报图案、
                                          // 动漫或游戏形象、卡通吉祥物、玩偶/手办/盲盒形象、主题乐园（迪士尼/环球等）元素、
                                          // 景区吉祥物、景区文创商品上的原创设计图案。没有则空数组
  "modern_landmark_items": ["..."],       // 【现代地标建筑本体】受著作权保护的建筑作品：体育场馆、摩天楼、电视塔/观光塔、
                                          // 会展中心、歌剧院/音乐厅、机场航站楼、大型商场、任何有在世建筑师署名的现代建筑。
                                          // ⚠️ 不包含古建筑：城墙、古塔、飞檐、斗拱、石狮、牌楼、亭子、宫殿屋顶属公共领域，不要写进来。
                                          // 名称必须与 standalone_objects 里完全一致。没有则空数组
  "carried_items": ["..."],               // 【那天你带着的东西】随身物/消耗品/自然物：门票、票根、地图、水壶、背包、帽子、
                                          // 相机、鞋、伞、冰淇淋、饮料、食物、落叶、合影照片等。名称必须与 standalone_objects 一致
  "standalone_objects": ["...","..."],    // 至少10个、最多14个【能单独抠出来做贴纸的实体物品】，按"纪念价值"从高到低排（不是按体量）。
                                          // 必须是物品不是人；必须是画面里真实存在的；用英文，简短具体，如 "electric guitar","drum kit","birthday cake"
                                          // ⚠️ 数量下限是硬要求，且必须【跨类别】：随身物（背包/外套/帽子/鞋/手机/水壶/门票/伞/食物）、
                                          // 自然物（落叶/树/花/石头/草地）、建筑构件或现场设施（围墙/花格/栏杆/路灯/台阶/长椅/遮阳棚）
                                          // 三类都要有。只给同一族的 6 项（例如全是树和墙）会导致后续选品无物可选。
  "composite_parts": [["A","B"]],         // 【复合物体拆解】清单里凡是「由多个可独立成立的部件组成」或「靠支架/细杆撑起来」的整体 A，
                                          // 给出其中最具代表性、体量最厚实、能单独成立的那一个部件 B。
                                          // 如 [["drum kit","bass drum"],["lego set","lego brick"],["gachapon machine","capsule toy"]]。
                                          // 不确定就不要写。没有则空数组
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
0. ⭐【最优先】优先选「那天你带着的东西」：门票、票根、地图、水壶、背包、帽子、相机、伞、鞋、
   冰淇淋、饮料、食物、落叶、合影照片这类随身物 / 消耗品 / 自然物。它们比地标建筑更值得画，
   同时写进 carried_items。
   🟢 可以画：不受著作权保护的自然景观与古建筑本体 —— 山、树、湖、城墙、古塔轮廓、飞檐、石狮。
   🔴 不要选：卡通吉祥物、玩偶/手办/盲盒形象、主题乐园元素、文创商品上的原创设计、任何商标字标或 logo；
      现代地标建筑本体（体育场馆、摩天楼、电视塔、会展中心等）也不要选，它们是受著作权保护的建筑作品；
      这类东西一律写进 ip_items 或 modern_landmark_items，不要写进 standalone_objects。
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
   清单里出现 "kit"、"set"、"rig"、"stand"、"tripod"、"rack" 这类词，几乎都是选错了。
   ⚠️ 这条对【任何品类】都成立，不限于乐器：乐高套装、盲盒套组、扭蛋机、玩具组、模型套件、
   相机三脚架、行李推车……只要是「一堆部件 + 支撑结构」，就在 composite_parts 里给出单件。"""

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
#
# ⚠️ 单词级词表里【不要】放 board / boards / screen / display / notice / tablet
#    这类多义词（2026-08-30 复盘）：它们会误命中案板（cutting board）、
#    屏风（folding screen）、平板电脑（tablet），把好物件当空色块降权剔除。
#    这些词只有在确定语义的词组里才成立，一律放到下面的 TEXT_DEPENDENT_PHRASES。
TEXT_DEPENDENT_WORDS = {
    "sign", "signs", "signage", "signboard", "signpost", "banner", "plaque",
    "billboard", "poster", "nameplate", "placard",
    "noticeboard", "label", "tag", "menu",
    "certificate", "scoreboard", "marquee", "leaflet", "flyer", "brochure",
    "inscription",
}
# 词组级：整词组匹配（按词边界），只认「去字后确实只剩一块空色块」的说法。
TEXT_DEPENDENT_PHRASES = {
    "display board", "information board", "info board", "notice board",
    "message board", "bulletin board", "exhibition board", "exhibit board",
    "menu board", "sign board", "name board", "score board", "poster board",
    "announcement board", "direction board", "departure board",
    "display panel", "information panel", "interpretive panel",
    "display screen", "led screen", "led display", "video screen", "video wall",
    "big screen", "giant screen", "projection screen", "projector screen",
    "information sign", "notice sign", "stone tablet", "memorial tablet",
    "notice paper", "public notice",
}


def _words(name):
    return set(_norm(name).split())


# ── IP 合规三级边界（2026-09-01 客户收紧策略） ──────────────────────────────
# 🔴 绝对不碰：主题乐园（迪士尼/环球）、景区吉祥物、景区文创设计、商标字标、
#             卡通/玩偶/手办/盲盒形象 → 硬剔除，一枚都不出。
# 🟢 可以做  ：不受著作权保护的自然景观与古建筑本体（山/树/湖/城墙/古塔/飞檐/石狮）。
# ⭐ 最优先  ：「那天你带着的东西」—— 随身物、消耗品、自然物。
#
# ⚠️ 词库只是先验，不是判据全部。真正兜住新品类的是三条不依赖词库的通用层：
#    (a) G0 让视觉模型自己填 ip_items / modern_landmark_items（模型判断 > 词表）；
#    (b) 目视质检复检 banned_ip_or_landmark，漏网的当轮判废；
#    (c) 被判废的物体走 ConvergenceGuard 永久剔除并换候选，不原地重试。
HARD_BAN_WORDS = {
    # 卡通 / 玩偶 / 手办 / 盲盒形象
    "mascot", "mascots", "doll", "dolls", "plush", "plushie", "plushies",
    "plushy", "figurine", "figurines", "funko", "amigurumi",
    "cartoon", "anime", "manga", "chibi", "vtuber",
    # 商标字标 / 品牌名
    "logo", "logos", "wordmark", "wordmarks", "trademark", "brand", "branded",
    "sponsor", "livery", "liveries", "decal", "decals",
    # 授权商品 / 文创商品设计
    "merch", "merchandise",
}
HARD_BAN_PHRASES = {
    "theme park", "amusement park", "theme park castle", "fairytale castle",
    "fairy tale castle", "cinderella castle", "park mascot", "scenic mascot",
    "mascot costume", "cartoon character", "anime character", "game character",
    "character goods", "character mascot", "stuffed animal", "stuffed toy",
    "soft toy", "plush toy", "plush doll", "blind box", "blind box figure",
    "mystery box figure", "gashapon figure", "capsule toy figure",
    "action figure", "collectible figure", "resin figure", "bobblehead",
    "cultural creative product", "cultural product", "creative merchandise",
    "souvenir merchandise", "licensed merchandise", "official merchandise",
    "branded merchandise", "gift shop merchandise", "designer toy",
    "art toy", "brand logo", "brand name", "team logo", "sponsor logo",
    "sponsor board", "brand mascot", "ip character",
}
# 现代地标建筑本体：受著作权保护的建筑作品（多有在世建筑师署名）→ 剔除。
# 只放【确定语义】的词组和无歧义单词，避免误伤 "temple gate"、"bell tower" 这类古建。
MODERN_LANDMARK_WORDS = {
    "stadium", "stadiums", "arena", "arenas", "skyscraper", "skyscrapers",
    "gymnasium", "velodrome", "natatorium", "megamall",
}
MODERN_LANDMARK_PHRASES = {
    "bird nest stadium", "birds nest stadium", "bird s nest stadium",
    "national stadium", "olympic stadium", "olympic tower", "water cube",
    "national aquatics center", "aquatics center", "sports center",
    "sports centre", "sports arena", "sports complex",
    "convention center", "convention centre", "exhibition center",
    "exhibition centre", "exhibition hall", "opera house", "concert hall",
    "tv tower", "television tower", "observation tower", "observation deck",
    "observation wheel", "ferris wheel", "office tower", "office building",
    "high rise", "high rise building", "glass tower", "glass facade tower",
    "steel tower", "shopping mall", "shopping center", "shopping centre",
    "airport terminal", "terminal building", "railway station building",
    "train station building", "museum building", "library building",
    "art museum building", "modern building", "modern architecture",
    "landmark building", "landmark tower", "iconic building",
    "iconic skyscraper", "city skyline", "skyline",
}
# 古建筑 / 公共领域构件：命中这些词时，即使同时命中现代词也按「可以画」处理。
ANCIENT_ARCH_WORDS = {
    "ancient", "historic", "historical", "traditional", "imperial", "dynasty",
    "ming", "qing", "tang", "song",
    "pagoda", "temple", "shrine", "palace", "eave", "eaves", "dougong",
    "bracket", "hutong", "siheyuan", "pavilion", "archway", "paifang",
    "torii", "stele", "watchtower", "drum tower", "bell tower", "city wall",
    "battlement", "battlements", "crenellation", "stone lion", "lion",
    "ruins", "relic", "courtyard", "roof tile", "glazed tile", "moat",
}
# ⭐ 那天你带着的东西：随身物 / 消耗品 / 自然物 —— 排序里提到最高优先级档。
CARRY_WORDS = {
    # 票证纸品
    "ticket", "tickets", "stub", "stubs", "boarding", "pass", "map", "maps",
    "postcard", "postcards", "stamp", "stamps", "photo", "photos",
    "photograph", "polaroid", "receipt", "wristband", "lanyard",
    # 随身携带
    "backpack", "rucksack", "bag", "tote", "handbag", "purse", "pouch",
    "suitcase", "luggage", "hat", "cap", "beanie", "sunhat", "scarf",
    "glove", "gloves", "umbrella", "parasol", "camera", "phone",
    "smartphone", "sunglasses", "glasses", "wallet", "keys", "watch",
    "shoe", "shoes", "sneaker", "sneakers", "boot", "boots", "sandal",
    "sandals", "sock", "socks", "notebook", "sketchbook", "pen", "pencil",
    "bottle", "flask", "thermos", "tumbler", "canteen", "fan", "towel",
    "headphones", "earphones", "earbuds", "mask", "badge", "pin", "keyring",
    # 消耗品 / 吃喝
    "ice", "cream", "icecream", "popsicle", "gelato", "cone", "coffee",
    "tea", "boba", "soda", "juice", "drink", "snack", "bread", "sandwich",
    "cake", "candy", "fruit", "apple", "orange", "banana", "skewer",
    "noodles", "dumpling", "dumplings", "straw", "cup", "mug",
    # 自然物（捡得起来的那种）
    "leaf", "leaves", "petal", "petals", "pinecone", "acorn", "shell",
    "pebble", "feather", "twig", "flower", "blossom",
}
CARRY_PHRASES = {
    "ice cream", "ice cream cone", "water bottle", "coffee cup", "paper cup",
    "bubble tea", "milk tea", "ticket stub", "entry ticket", "admission ticket",
    "paper map", "tourist map", "guide map", "folding fan", "sun hat",
    "group photo", "group picture", "instant photo", "fallen leaf",
    "fallen leaves", "ginkgo leaf", "maple leaf", "picnic mat",
    "picnic blanket", "tote bag", "canvas bag", "shoulder bag",
}
# 「成套 / 支撑结构」中心词：通用兜底拆解用。词库未命中的新品类靠这里落地。
COMPOSITE_HEADS = {
    "kit", "kits", "set", "sets", "rig", "rigs", "stand", "stands",
    "tripod", "tripods", "rack", "racks", "mount", "mounts", "assembly",
    "assemblies", "system", "systems", "ensemble", "combo",
    "installation", "apparatus", "station", "cart", "trolley",
}
# ⚠️ 不要把 pile / stack / bunch / bundle 这类【集合量词】放进来（见 COLLECTIVE_HEADS）：
#    "leaf pile" 的中心词虽是量词，但它不是成套装备，去掉量词会打乱同族判定。

# ── G0 召回类别覆盖（2026-09-02 · v36 06 银杏事故）─────────────────────────
# 事故经过：同一张银杏照片，G0 有一次只召回 6 项且全是树/墙同族，⭐随身物档
# 只有 `ginkgo leaf` 一项；这一项一轮内撞上互斥规则被永久剔除后候选池当场见底，
# 最终交付是「4 物品 + 2 人物、银杏叶缺席」—— 技术指标全过，产品完全不合格。
# 根因不是选品逻辑，是 **G0 召回抖动**（第 3 次跑同一张照片时它给出了
# backpack / jacket / sneakers）。
#
# 所以给召回加两条下限：数量 ≥ G0_MIN_OBJECTS，且横跨 ≥ G0_MIN_CATEGORIES 个类别。
# 这两个词库只用于【判断召回够不够杂】，不参与选品排序，判错只会多问一次模型。
NATURE_WORDS = {
    "tree", "trees", "trunk", "trunks", "branch", "branches", "twig", "foliage",
    "bush", "bushes", "shrub", "shrubs", "hedge", "grass", "lawn", "moss",
    "plant", "plants", "bamboo", "pine", "maple", "ginkgo", "willow", "cherry",
    "flower", "flowers", "blossom", "petal", "petals", "leaf", "leaves",
    "stone", "rock", "boulder", "pebble", "sand", "soil", "water", "pond",
    "lake", "river", "stream", "mountain", "hill", "cloud", "clouds", "sky",
    "snow", "puddle", "root", "roots", "acorn", "pinecone", "feather", "shell",
}
# 建筑构件 / 现场设施：不区分古今（现代地标本体的剔除在选品层做，这里只数类别）
FACILITY_WORDS = {
    "wall", "walls", "lattice", "gate", "gateway", "door", "doorway", "window",
    "railing", "fence", "balustrade", "roof", "tile", "tiles", "column",
    "pillar", "arch", "archway", "step", "steps", "stair", "stairs",
    "staircase", "bridge", "path", "pavement", "paving", "curb", "bollard",
    "lamp", "lamppost", "lantern", "streetlight", "bench", "chair", "stool",
    "kiosk", "booth", "canopy", "tent", "awning", "umbrella", "planter",
    "pot", "bin", "statue", "sculpture", "monument", "pillar", "post",
    "building", "house", "hall", "tower", "pagoda", "pavilion", "temple",
    "gazebo", "stage", "platform", "barrier", "handrail", "grille", "screen",
}
G0_MIN_OBJECTS = 10        # 候选物品数下限：低于此值，撞一次互斥规则池子就可能见底
G0_MIN_CATEGORIES = 3      # 类别覆盖下限：随身物 / 自然物 / 建筑设施 / 其它
G0_MAX_ATTEMPTS = 3        # 单次 G0 调用的解析重试上限（缺陷 A）
G0_RECALL_ATTEMPTS = 2     # 召回不足时最多再补问几次（缺陷 06 银杏）


def _phrase_hit(name, phrases):
    """按词边界做整词组匹配（避免 "cutting board" 误命中 "board" 类判定）。"""
    padded = " %s " % _norm(name)
    return any((" %s " % p) in padded for p in phrases)


def _hard_banned(name):
    """🔴 绝对不碰：卡通吉祥物/玩偶/主题乐园/文创设计/商标字标 → 直接剔除。"""
    return bool(_words(name) & HARD_BAN_WORDS) or _phrase_hit(name, HARD_BAN_PHRASES)


def _ancient_arch(name):
    """🟢 古建筑本体 / 公共领域构件：可以画。"""
    return bool(_words(name) & ANCIENT_ARCH_WORDS) or _phrase_hit(
        name, {"city wall", "stone lion", "drum tower", "bell tower"})


def _modern_landmark(name):
    """现代地标建筑本体（受著作权保护的建筑作品）→ 剔除。

    与古建筑的区分点：命中古建词（ancient / pagoda / city wall / eaves…）就放行，
    所以 "ancient watchtower"、"pagoda tower" 不会被误杀，而 "national stadium"、
    "observation tower"、"glass tower" 会被拦下。
    """
    if _ancient_arch(name):
        return False
    return bool(_words(name) & MODERN_LANDMARK_WORDS) or _phrase_hit(
        name, MODERN_LANDMARK_PHRASES)


def _carry_item(name):
    """⭐「那天你带着的东西」：随身物 / 消耗品 / 自然物 → 最高优先级档。"""
    return bool(_words(name) & CARRY_WORDS) or _phrase_hit(name, CARRY_PHRASES)


def g0_category(name):
    """把一个候选物品归到 G0 召回类别（只用于判断召回够不够杂，不参与选品排序）。

    顺序有意为之：随身物优先（ginkgo leaf 既是叶子也是能捡起来带走的东西，
    它对本产品的价值在「带走」这一面），其次自然物，再次建筑设施。
    """
    if _carry_item(name):
        return "carry"
    if _words(name) & NATURE_WORDS:
        return "nature"
    if (_words(name) & FACILITY_WORDS) or _ancient_arch(name) or _modern_landmark(name):
        return "facility"
    return "other"


G0_CATEGORY_CN = {"carry": "随身物", "nature": "自然物",
                  "facility": "建筑/设施", "other": "其它"}


def g0_recall_report(objs):
    """返回 (是否达标, 数量, 类别集合, 一句话说明)。"""
    names = _strlist(objs)
    cats = {g0_category(o) for o in names}
    ok = len(names) >= G0_MIN_OBJECTS and len(cats) >= G0_MIN_CATEGORIES
    desc = "%d 项 / %d 类（%s）" % (
        len(names), len(cats),
        "、".join(G0_CATEGORY_CN.get(c, c) for c in sorted(cats)) or "无")
    return ok, len(names), cats, desc


def _ip_reason(name):
    """返回硬剔除原因；不该剔除时返回 None。词库层，模型层在 select_objects 里合并。"""
    if _hard_banned(name):
        return "🔴 IP 硬禁止（卡通吉祥物/玩偶/主题乐园/文创设计/商标字标）"
    if _modern_landmark(name):
        return "🔴 现代地标建筑本体（受著作权保护的建筑作品，古建筑本体才可画）"
    return None



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
    """这枚物体的主体价值是不是全在文字上（按合规去字后只剩一块纯色空框）。

    两级匹配：单词级词表（sign / plaque / poster…本身就等于「一块字」）+
    词组级词表（board / screen / display 这类多义词只在确定词组里才算）。
    词组按【词边界】匹配，所以 "cutting board"（案板）、"folding screen"（屏风）
    不会被误判 —— 这是 2026-08-30 复盘时收窄的。
    """
    if _words(name) & TEXT_DEPENDENT_WORDS:
        return True
    padded = " %s " % _norm(name)
    return any((" %s " % p) in padded for p in TEXT_DEPENDENT_PHRASES)


def preflight(photo, workdir):
    # 尺寸也必须按 EXIF 摆正后再取：Orientation=6 的竖拍照片，原始像素是
    # 3024x4032 记成 4032x3024，_size_px 会写反（短边不受影响，但报告会误导）。
    _w, _h = open_photo(photo).size
    small = shrink(photo, os.path.join(workdir, "src_small.jpg"))
    data = _g0_ask_with_retry(small)
    data = _g0_normalize(data)
    data = _g0_fill_recall(data, small)
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


# ── 缺陷 A（2026-09-02）：G0 返回截断 JSON 时整单直接崩 ─────────────────────
# 实测：v36 02 演出现场首跑，G0 返回的 JSON 在 `"mode` 处被截断，
# `preflight()` 里一句 `raise SystemExit` 就把整单结束了 —— 没有任何重试，
# 前面的自检、体检全部作废，人得手动重跑。
#
# 形状归一（_strlist / _pairs_from）挡的是「字段类型抖动」，挡不住「响应被砍断」：
# 那时候连 JSON 都不完整，没有任何字段可归一。
#
# 修法：把「调用 + 解析」包成有限次重试（G0_MAX_ATTEMPTS 次），并且重试时
# **改一下请求**而不是原样再问一遍 —— 原样重问对「输出长度超限」这个根因无效。
# 追加提示要求：只输出 JSON、值写短、可选字段允许直接给 []。
# 超过上限才失败，且报错必须明说是 G0 阶段失败（v36 那次报错看不出阶段）。
G0_JSON_ONLY_HINT = """

────────────────────────────────────────────────────────────
⚠️ 重试提示（上一次的返回无法解析：被截断 / 不是 JSON / 空返回）
────────────────────────────────────────────────────────────
这一次请严格遵守，否则本单无法处理：
1. **只输出一个 JSON 对象**：第一个字符是 `{`，最后一个字符是 `}`。
   不要 markdown 代码块，不要任何解释文字、前言或结语。
2. **把值写短**：每个字符串控制在 20 个字以内，不要在 JSON 里写注释。
3. **宁可少写可选字段也必须保证 JSON 完整闭合**：
   `composite_parts` / `similar_pairs` / `container_pairs` / `thin_parts`
   如果不确定，直接给 `[]`。
4. 必须给出的字段只有这些：`scene`、`lighting`、`people_count`、`has_minor`、
   `ip_items`、`modern_landmark_items`、`carried_items`、`standalone_objects`、
   `keepsake_objects`、`reject_reason`。
"""


def g0_parse_problem(txt, data):
    """G0 返回不可用时给出【可恢复错误】的具体类型；可用时返回 None。

    抽成独立函数是为了能离线回归测试（造一段截断 JSON 喂进来即可）。
    """
    s = txt if isinstance(txt, str) else ("" if txt is None else str(txt))
    if not s.strip():
        return "视觉模型返回空内容"
    if data is None:
        tail = s[-160:].replace("\n", " ")
        if s.count("{") > s.count("}"):
            return "JSON 未闭合（疑似被截断，%d 个 { 对 %d 个 }），末尾：…%s" % (
                s.count("{"), s.count("}"), tail)
        return "返回内容里找不到可解析的 JSON，末尾：…%s" % tail
    if not isinstance(data, dict):
        return "解析出来的不是 JSON 对象（是 %s）" % type(data).__name__
    if not _strlist(data.get("standalone_objects")) and not data.get("reject_reason"):
        return "JSON 可解析但 standalone_objects 为空且未给拒稿原因（疑似截断在字段中途）"
    return None


def _g0_ask_with_retry(small, extra_task="", stage="G0"):
    """调一次 G0 并在【可恢复错误】上有限次重试。超上限抛 SystemExit。"""
    problems, txt = [], ""
    for attempt in range(1, G0_MAX_ATTEMPTS + 1):
        task = PREFLIGHT_TASK + extra_task
        if attempt > 1:
            task += G0_JSON_ONLY_HINT
        txt = call_vision([small], task)
        data = grab_json(txt if isinstance(txt, str) else str(txt or ""))
        problem = g0_parse_problem(txt, data)
        if problem is None:
            if attempt > 1:
                log("  ✅ %s 第 %d 次调用解析成功（前 %d 次不可用，已重试）"
                    % (stage, attempt, attempt - 1))
            return data
        problems.append(problem)
        log("  🟡 %s 第 %d/%d 次调用返回不可用：%s"
            % (stage, attempt, G0_MAX_ATTEMPTS, problem))
        if attempt < G0_MAX_ATTEMPTS:
            log("     ↻ 重试并要求模型只输出 JSON、缩短字段值")
    raise SystemExit(
        "❌ 【%s 阶段失败】视觉模型连续 %d 次未返回可解析 JSON，本单停止"
        "（未消耗任何生图额度）。\n   逐次原因：\n%s\n"
        "   最后一次原始返回（前 600 字）：\n%s"
        % (stage, G0_MAX_ATTEMPTS,
           "\n".join("     %d) %s" % (i + 1, p) for i, p in enumerate(problems)),
           (txt if isinstance(txt, str) else str(txt or ""))[:600]))


def _g0_normalize(data):
    """G0 出口的字段清洗：null 兜底 → 形状归一 → 词库补全。"""
    # ⚠️ 不能用 setdefault：视觉模型经常把可选字段显式写成 null（实拍 run_regress
    #    的 keepsake_objects / container_pairs 全是 null），setdefault 只在「键不存在」
    #    时生效，null 会原样留下，后面 `for x in None` 直接把整套保护逻辑跳过。
    #    这就是生日单出现「空盘子单独成为一枚贴纸」的真正原因。
    for _k, _dv in (("ip_items", []), ("thin_parts", []), ("standalone_objects", []),
                    ("keepsake_objects", []), ("container_pairs", []),
                    ("similar_pairs", []), ("people_count", 0)):
        if data.get(_k) is None:
            data[_k] = _dv
    # 形状归一（2026-09-01）：上面只解决了 null，没解决【类型不对】。
    # 视觉模型对同一个字段可能回 ["a","b"]、"a, b"、[{"name":"a"}] 三种形状，
    # 后面这些字段全都直接进 for / join / set 推导，一旦形状不对就抛 TypeError，
    # 而抛点在 G0 之后 —— 表现是「这一单直接崩，交付不出来」。
    # 所以在【边界】统一过一遍 _strlist，后续代码可以放心假设是 [str]。
    for _k in ("ip_items", "thin_parts", "standalone_objects", "keepsake_objects",
               "modern_landmark_items", "carried_items", "text_dependent",
               "color_palette"):
        if _k in data:
            data[_k] = _strlist(data.get(_k))
    # composite_parts 是成对结构，用 _pairs_from 归一成 [[整体, 单件]]
    if data.get("composite_parts") is not None:
        data["composite_parts"] = [[a, b] for a, b in _pairs_from(data["composite_parts"])]
    # 模型漏标时用内置词库补全，不依赖模型自觉
    data["container_pairs"] = infer_container_pairs(
        data["standalone_objects"], data["container_pairs"])
    data["keepsake_objects"] = infer_keepsakes(
        data["standalone_objects"], data["keepsake_objects"])
    return data


G0_RECALL_HINT = """

────────────────────────────────────────────────────────────
⚠️ 补充召回（上一次 standalone_objects 只给出 %d 项：%s）
────────────────────────────────────────────────────────────
候选太少或类别太单一，后续选品一旦撞上互斥规则就会无物可选，
最终会漏掉这张照片的主角。请在保留上面已给出的物品的基础上，
**再补充一些不同类别的物品，尤其是人物随身携带的东西**：
· ⭐ 人物随身携带 / 身上穿戴的东西（最重要，请优先找）：背包、单肩包、手提袋、
  外套、大衣、围巾、帽子、鞋、手机、相机、水壶、门票、地图、伞、
  手里拿着的食物或饮料 —— 画面里只要看得见就写进来，并同时写进 carried_items；
· 自然物：落叶、叶簇、树、花、草地、石头；
· 建筑构件 / 现场设施：围墙、花格、栏杆、路灯、台阶、长椅、遮阳棚、指示柱。
要求 `standalone_objects` **至少 %d 项**，且横跨【随身物 / 自然物 / 建筑或设施】
三个类别（缺哪类补哪类，当前缺：%s）。
仍然只输出一个 JSON 对象，不要解释文字。
"""


def _g0_merge(base, extra):
    """把补问回来的 G0 结果并进已有结果：清单取并集（保序去重），标量保留首答。"""
    for key in ("standalone_objects", "carried_items", "keepsake_objects",
                "ip_items", "modern_landmark_items", "thin_parts"):
        old = _strlist(base.get(key))
        seen = {_norm(x) for x in old}
        for x in _strlist(extra.get(key)):
            if _norm(x) and _norm(x) not in seen:
                seen.add(_norm(x))
                old.append(x)
        base[key] = old
    for key in ("container_pairs", "similar_pairs"):
        pairs = [p for p in (base.get(key) or [])
                 if isinstance(p, (list, tuple)) and len(p) == 2]
        have = {(_norm(p[0]), _norm(p[1])) for p in pairs}
        for p in (extra.get(key) or []):
            if isinstance(p, (list, tuple)) and len(p) == 2 and \
                    (_norm(p[0]), _norm(p[1])) not in have:
                have.add((_norm(p[0]), _norm(p[1])))
                pairs.append([p[0], p[1]])
        base[key] = pairs
    # composite_parts 经 _g0_normalize 后一定是 [[整体, 单件]]，这里仍按 pairs 处理
    cp_pairs = [p for p in (base.get("composite_parts") or [])
                if isinstance(p, (list, tuple)) and len(p) == 2]
    have_cp = {(_norm(p[0]), _norm(p[1])) for p in cp_pairs}
    for p in (extra.get("composite_parts") or []):
        if isinstance(p, (list, tuple)) and len(p) == 2 and \
                (_norm(p[0]), _norm(p[1])) not in have_cp:
            have_cp.add((_norm(p[0]), _norm(p[1])))
            cp_pairs.append([p[0], p[1]])
    base["composite_parts"] = cp_pairs
    # 拒稿原因只要有一方给了就必须保留（补问时模型可能忘了写）
    if not base.get("reject_reason") and extra.get("reject_reason"):
        base["reject_reason"] = extra["reject_reason"]
    return base


def _g0_fill_recall(data, small):
    """召回下限保证（06 银杏事故）：数量不足或类别单一时补问，仍不足只警告不阻断。"""
    ok, n, cats, desc = g0_recall_report(data.get("standalone_objects"))
    if ok:
        log("  召回      : %s ✅ 达标（下限 %d 项 / %d 类）"
            % (desc, G0_MIN_OBJECTS, G0_MIN_CATEGORIES))
        return data
    for attempt in range(1, G0_RECALL_ATTEMPTS + 1):
        missing = [G0_CATEGORY_CN[c] for c in ("carry", "nature", "facility")
                   if c not in cats]
        log("  🟡 G0 召回不足：%s（下限 %d 项 / %d 类）→ 第 %d/%d 次补问"
            % (desc, G0_MIN_OBJECTS, G0_MIN_CATEGORIES, attempt, G0_RECALL_ATTEMPTS))
        hint = G0_RECALL_HINT % (n, ", ".join(_strlist(data.get("standalone_objects"))[:14]),
                                 G0_MIN_OBJECTS, "、".join(missing) or "（类别够了，数量不够）")
        try:
            extra = _g0_normalize(_g0_ask_with_retry(small, hint, stage="G0 补充召回"))
        except SystemExit as e:
            # 补问失败不能把整单拖死：首答本身是可用的，退化成「候选池偏薄」继续跑
            log("  🟡 补问失败（%s），沿用首次召回结果继续" % str(e)[:120])
            break
        before = n
        data = _g0_merge(data, extra)
        ok, n, cats, desc = g0_recall_report(data.get("standalone_objects"))
        log("  ▸ 补问后召回 : %d → %s" % (before, desc))
        if ok:
            log("  召回      : %s ✅ 达标（经 %d 次补问）" % (desc, attempt))
            return data
    log("  🟡 G0 召回仍未达下限：%s —— 这张照片可能确实只有这些可拆元素。"
        "继续跑，但候选池偏薄，撞互斥规则时更容易凑不满枚数（见 qc_report.md）" % desc)
    data["_recall_warning"] = desc
    return data


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

# 2026-09-01 客户 IP 策略：写进 prompt 的硬边界。代码侧已在选品阶段剔除，
# 但生图模型会「顺手」把地标补进画面（05 鸟巢实拍：清单里没有体育场，模型自己加了），
# 所以 prompt 里必须再声明一次。
IP_POLICY = """IP AND TRADEMARK POLICY - HARD LIMITS, NEVER DRAW THESE:
- No theme-park elements of any kind (no Disney, no Universal, no park castle, no park ride branding), no scenic-area mascots, no cultural-merchandise designs from any gift shop.
- No cartoon characters, no mascot suits, no dolls, plush toys, figurines, blind-box or collectible figures.
- No brand logos, wordmarks, brand names, trademarks, sponsor boards or product lettering. No readable text at all.
- No MODERN LANDMARK BUILDINGS as a subject: no stadium, arena, skyscraper, TV or observation tower, convention centre, opera house, glass office tower, shopping mall or city skyline. Modern buildings are copyrighted architectural works.
ALLOWED instead: plain natural scenery and ANCIENT architecture as a form - mountains, trees, a lake, a city wall, a pagoda silhouette, an upturned eave, a stone lion, roof tiles.
PREFERRED above all: the things the customer was carrying that day - the ticket, the paper map, the water bottle, the backpack, the hat, the ice cream, a fallen leaf, the photo they took."""

# 「自带支架/底座」的通用约束：不枚举 drum kit / mic stand（枚举永远漏），
# 而是描述这个类别的共同特征，让模型自己判断。质检侧有对应的通用复检。
NO_SUPPORT_RIG = """NO SUPPORT STRUCTURE - GENERAL RULE: every element is the object ITSELF and nothing else. If the real object sits on, hangs from or is held up by something that is not part of it - a stand, a tripod, a pole, a bracket, a rack, a mount, a pedestal, a base plate, a hook, a shelf, a table or the floor - drop that supporting part entirely and draw the object as one compact chunky shape floating alone. Never draw an assembly of several parts joined by thin rods. If an object only makes sense as a whole set, draw just the single thickest, most recognisable piece of that set."""

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


def _strlist(blob):
    """把模型返回的「一串名字」归一成 [str]，任何形状都不许把产线弄崩。

    视觉模型不是稳定 API：要 ["a","b"] 时它可能回 "a, b"、[{"name":"a"}]、
    甚至 [["a","支架"]]。原来这些字段直接进 ", ".join(...)，遇到 dict 就
    TypeError —— 一次格式抖动 = 这一单交付不出来。宁可名字取得糙一点。
    """
    if blob in (None, "", [], {}):
        return []
    if isinstance(blob, str):
        parts = re.split(r"[,;、；]", blob)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(blob, dict):
        blob = list(blob.values())
    if not isinstance(blob, (list, tuple, set)):
        return [str(blob).strip()]
    out = []
    for x in blob:
        if isinstance(x, str):
            s = x.strip()
        elif isinstance(x, dict):
            s = next((str(x[k]).strip() for k in ("name", "element", "object",
                                                  "label", "名称")
                      if isinstance(x.get(k), str)), "")
            if not s and len(x) == 1:
                s = str(list(x.values())[0]).strip()
        elif isinstance(x, (list, tuple)) and x:
            s = str(x[0]).strip()
        else:
            s = str(x).strip()
        if s:
            out.append(s)
    return out


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

def _pairs_from(blob):
    """把模型给的「整体→单件」映射归一成 [(whole, part)]，容忍各种返回形状。

    为什么要这么宽松（2026-09-01 离线冒烟实测踩到）：prompt 里要的是
    `[["A","B"]]`，但视觉模型完全可能回 `{"A": "B"}`、
    `[{"whole":"A","part":"B"}]` 或者 `["A -> B"]`。原来的实现直接
    `list + list`，模型回 dict 时抛 TypeError，**整条产线在 G0 之后当场崩掉**
    —— 一个格式抖动就让订单交付不出来，比漏判严重得多。
    解析不了的形状一律忽略（返回空），让上层退回词库和通用中心词规则，
    这是「失败可收敛」而不是「失败即崩」。
    """
    out = []
    if not blob:
        return out
    if isinstance(blob, dict):
        return [(k, v) for k, v in blob.items() if isinstance(v, str)]
    if isinstance(blob, str):
        blob = [blob]
    if not isinstance(blob, (list, tuple)):
        return out
    for item in blob:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((item[0], item[1]))
        elif isinstance(item, dict):
            # 键名不固定，取「第一个像整体的」和「第一个像单件的」
            whole = next((item[k] for k in ("whole", "object", "composite",
                                            "name", "from", "整体")
                          if isinstance(item.get(k), str)), None)
            part = next((item[k] for k in ("part", "representative_part",
                                          "replacement", "to", "单件")
                         if isinstance(item.get(k), str)), None)
            if whole is None and part is None and len(item) == 1:
                (whole, part), = item.items()
            if isinstance(whole, str) and isinstance(part, str):
                out.append((whole, part))
        elif isinstance(item, str):
            for sep in ("->", "→", "=>", ":", "：", "|"):
                if sep in item:
                    a, b = item.split(sep, 1)
                    out.append((a, b))
                    break
    return [(a, b) for a, b in out if isinstance(a, str) and isinstance(b, str)]


def composite_map(info):
    """把 G0 / 目视质检给出的 composite_parts 归一成 {整体: 单件}。

    这是「不靠词库」的那一层：COMPOSITE_REPLACE 只覆盖生日/演出那几张照片长出来的
    13 项，遇到 lego set / blind box / gachapon machine 之类新品类必然漏判。
    模型每轮都会被问「这个物体是否由多个可独立成立的部件组成，若是给出最有代表性的
    那一个」，答案就落在这里，优先级排在通用中心词规则之前、词库之后。
    """
    out = {}
    for whole, part in (_pairs_from(info.get("composite_parts"))
                        + _pairs_from(info.get("_composite_parts"))):
        whole, part = _norm(whole), str(part).strip()
        if whole and part and _norm(part) != whole:
            out[whole] = part
    return out


def _decompose(name, model_map=None):
    """把成套装备换成单件；返回 (替换后名称, 是否发生替换)。

    三层，依次尝试 —— 词库是先验，模型和通用规则负责新品类：
      ① COMPOSITE_REPLACE 词库（最准，实拍验证过的 13 项）
      ② 视觉模型给出的 composite_parts（G0 或目视质检问出来的）
      ③ 通用中心词规则：中心词是「成套/支撑结构」类词（kit/set/rig/stand/…）就去掉它，
         lego set → lego、model kit → model、luggage cart → luggage。
         宁可名字变粗糙，也不要把一整套带支架的东西丢给生图模型。
    """
    n = _norm(name)
    if n in COMPOSITE_REPLACE:
        return COMPOSITE_REPLACE[n], True
    for k, v in COMPOSITE_REPLACE.items():
        if n.endswith(" " + k) or n == k:
            return v, True
    if model_map and n in model_map:
        return model_map[n], True
    words = n.split()
    if len(words) > 1 and words[-1] in COMPOSITE_HEADS:
        return " ".join(words[:-1]), True
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


# 集合名词：这类词做中心词时不代表物品本身，只是「一堆」的量词。
# 实拍漏判（2026-08-30 · v35）：leaf pile 的中心词是 pile，落不进树体族，
# 结果「单叶 + 叶堆」可能同版共存。真正决定剪纸轮廓的是被数的那个东西，
# 所以遇到这种结构要先用前一个词（leaf）判族，判不出来再退回用集合名词本身。
# 注意 "pile of leaves" 这种写法不需要特殊处理 —— 它的最后一个词已经是 leaves。
COLLECTIVE_HEADS = {
    "pile", "piles", "heap", "heaps", "stack", "stacks", "cluster", "clusters",
    "bunch", "bunches", "bundle", "bundles", "clump", "clumps", "mound",
    "mounds", "pair", "pairs", "bouquet", "bouquets", "row", "rows",
    "group", "groups", "collection", "collections",
}


def _family(name):
    """
    用【中心词】判定所属物品族，而不是子串匹配。
    英文复合名词的中心词在最后：electric guitar→guitar，bass drum→drum，
    guitar amplifier→amplifier，microphone→microphone。
    子串匹配会把 microphone 误判成 phone、guitar amplifier 误判成 guitar、
    bass drum 误判成 bass(guitar)，导致候选被过度砍光。

    例外：中心词是集合名词（leaf pile / flower bunch / stone stack）时，
    先用它前面那个被数的词判族，见 COLLECTIVE_HEADS。
    """
    words = _norm(name).split()
    if not words:
        return None
    heads = [words[-1]]
    if len(words) > 1 and words[-1] in COLLECTIVE_HEADS:
        heads.insert(0, words[-2])          # 优先用「被数的那个东西」判族
    for head in heads:
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


def _rank_ctx(info):
    """选品排序需要的两份上下文：纪念物集合 + G0 标注的随身物集合。"""
    cmap = composite_map(info)
    keep = {_norm(_decompose(x, cmap)[0]) for x in info.get("keepsake_objects") or []}
    carried = {_norm(x) for x in info.get("carried_items") or []}
    return keep, carried


def _rank_of(name, keep=(), carried=()):
    """选品优先级档（越小越先选）：

      0  ⭐ 随身物 / 消耗品 / 自然物 —— 「那天你带着的东西」，零 IP 风险且最有记忆点
      1  纪念物（G0 keepsake）
      2  普通物品
      3  古建筑本体（公共领域，可画，但不如随身物；现代地标已在入池阶段剔除）
      4  文字依赖件（去字后只剩空色块，垫底，仅候选不足时启用）
    """
    if _text_dependent(name):
        return 4
    if _carry_item(name) or _norm(name) in set(carried):
        return 0
    if _norm(name) in set(keep):
        return 1
    if _ancient_arch(name):
        return 3
    return 2


def select_objects(info, n_obj, banned=None):
    """
    从 G0 清单里挑出 n_obj 个【互不重复、互不包含、IP 合规】的物品。

    取代旧的 objs[:n_obj] —— 旧写法有三个致命缺陷，已在实拍中全部复现：
      1. 纪念物被截断：生日照的 "candle" 排第 7，直接被切掉；
      2. 容器重复：cheesecake 与 plate 同时入选，蛋糕自带盘子 → 两枚重叠；
      3. 同族重复：electric guitar 与 bass guitar 同时入选 → 看起来是两把吉他。

    banned = 已被判定「不可收敛 / 不合规」的物体名集合（ConvergenceGuard 传入），
    永久剔除，回填层也不会把它们放回来 —— 否则换元素等于没换。
    返回 (picked, dropped_log)
    """
    banned = {_norm(b) for b in (banned or [])}
    cmap = composite_map(info)
    objs, seen, dropped = [], set(), []
    # G0 自己标出来的违规项也并进硬剔除集合（模型判断 > 词表，覆盖词库没有的新品类）
    model_flag = {}
    for x in info.get("ip_items") or []:
        model_flag[_norm(x)] = "🔴 IP 硬禁止（G0 标注为第三方 IP / 商标 / 吉祥物 / 文创设计）"
    for x in info.get("modern_landmark_items") or []:
        model_flag[_norm(x)] = "🔴 现代地标建筑本体（G0 标注为受著作权保护的建筑作品）"
    for o in info.get("standalone_objects") or []:
        o2, changed = _decompose(o, cmap)
        if changed:
            dropped.append("%s → 改用单件 %s（整套装备带支架细杆，模切做不了）" % (o, o2))
        k = _norm(o2)
        if not k or k in seen:
            continue
        reason = _ip_reason(o2) or model_flag.get(k) or model_flag.get(_norm(o))
        if reason:
            dropped.append("%s（%s，一枚都不出）" % (o2, reason))
            continue
        if k in banned:
            continue
        seen.add(k); objs.append(o2)


    # ① 容器剔除：A 盛放在 B 上时，两者只留一个。
    #
    # ⚠️ 这里不能无脑「丢外层」。实拍踩过的坑：G0 把生日单标成
    #        [["birthday cake","plate"], ["candle","birthday cake"], ["sparkler","glass cup"]]
    #    第二对的语义是「蜡烛插在蛋糕上」，无脑丢外层就把【蛋糕本身】删了 ——
    #    整单最重要的纪念物没了，比留一个空盘子还糟。
    #    所以要比「价值」：容器词最低，纪念物最高；两个都是纪念物时丢里层
    #    （蜡烛本来就画在蛋糕上，蛋糕带蜡烛才是那枚经典图案）。
    _keepset = {_norm(_decompose(x, cmap)[0]) for x in info.get("keepsake_objects") or []}

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

    # ③ 排序：⭐随身物置顶 → 纪念物 → 普通物品 → 古建筑本体 → 「去字后只剩空色块」的低价值件垫底。
    #    为什么随身物在最前（2026-09-01 客户 IP 策略）：「那天你带着的东西」（门票/地图/水壶/
    #    背包/帽子/冰淇淋/落叶/合影）既没有任何 IP 风险，又比地标建筑更能唤起当天的记忆。
    #    古建筑本体（城墙/古塔/飞檐/石狮）属公共领域可以画，但只排在普通物品之后 ——
    #    现代地标建筑本体已在入池阶段硬剔除，根本走不到排序。
    #    垫底而不是删除：有更好的候选就轮不到它们；候选不够时它们仍是兜底来源。
    #    低价值判定优先于其它判定 —— G0 有时会把匾额/横幅标成纪念物，
    #    但合规要求必须去字，去完还是一块空色块，所以以「去字后还剩什么」为准。
    keep = [k for k in (_norm(_decompose(x, cmap)[0]) for x in info.get("keepsake_objects") or [])
            if k in seen]
    carried = {_norm(x) for x in info.get("carried_items") or []}
    objs.sort(key=lambda o: _rank_of(o, keep, carried))

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
    #
    # ⚠️ 缺陷（2026-08-30 · v35 06 银杏实拍，已修）：这里原来只排除容器项，
    #    【没有过同族检查】，于是刚在 ③ 里被剔掉的同族件又被原样放回来 ——
    #    日志明明打了「已剔除 tree trunk（与已选物品同族）」，最终却选中
    #    ginkgo leaf + ginkgo tree + tree trunk 三枚树体部件。去重白做了。
    #
    # 现在分三档，档与档之间严格递进，枚数硬约束仍然保住：
    #    档 1  非容器 + 不同族   —— 正常情况到这里就够了
    #    档 2  非容器 + 允许同族 —— 只有「所有剩余候选都同族、否则凑不满」时才走，
    #                              且必须在日志里明确警告，事后好定位
    #    档 3  连容器项也用上   —— 最后的手段（盘子单独出是废件，能不用就不用）
    def _backfill(pool, allow_same_family):
        for o in pool:
            if len(picked) >= n_obj:
                return
            if _norm(o) in {_norm(p) for p in picked}:
                continue
            fam = _family(o)
            same_fam = fam is not None and fam in used_fam
            if same_fam and not allow_same_family:
                continue
            if same_fam:
                dropped.append("⚠️ 因候选不足，回填了同族元素 %s（与已选物品同族，"
                               "轮廓可能雷同；枚数是硬约束，缺枚比轻微雷同更糟）" % o)
            picked.append(o)
            if fam is not None:
                used_fam.add(fam)

    non_container = [o for o in objs if _norm(o) not in contained]
    if len(picked) < n_obj:
        _backfill(non_container, False)
    if len(picked) < n_obj:
        _backfill(non_container, True)
    # 仍不足才动容器项
    if len(picked) < n_obj:
        _backfill(objs, True)
    # 把「降权后没被选上」的低价值件如实记进日志，方便复盘为什么没有它
    _pick = {_norm(o) for o in picked}
    for o in objs:
        if _text_dependent(o) and _norm(o) not in _pick:
            dropped.append("%s（主体价值依赖文字，按合规去字后只剩一块纯色空框，"
                           "已降权，仅在候选不足时才启用）" % o)
    return picked, dropped


def build_prompt(info, n, patches=None, figure_style="collage", exclude=None, banned=None):
    night = info.get("lighting") == "night"
    n_obj, n_ppl = plan_mix(info, n)
    picked, _dropped = select_objects(info, n_obj, banned)
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
        IP_POLICY,
        NO_SUPPORT_RIG,
        COMPOSITION % {"n": n, "nobj": len(picked), "nppl": n_ppl,
                       "objs": "; ".join('"%s"' % o for o in picked)},
    ]
    # 纪念物保护：只对本次真正入选的 keepsake 生效
    _pk = {_norm(o) for o in picked}
    keepsakes = [k for k in (_decompose(x, composite_map(info))[0]
                             for x in info.get("keepsake_objects") or [])
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

# v36 实跑暴露的缺陷：IP_POLICY 排在 SCENE_OUTPUT 之前，而 SCENE_OUTPUT 里写着
# 「keep the setting … the same subject in the same place」，两段直接打架，
# 后出现的那段赢 —— 05 鸟巢的场景图把受著作权保护的钢结构编织外立面原样画了出来。
# 贴纸版没这个问题（它靠 select_objects() 在选品层就把地标硬剔了），场景图没有选品层，
# 只能靠 prompt。所以把地标禁令挪到【最后】，并且点名替换方案，不留解释空间。
SCENE_LANDMARK_OVERRIDE = """MODERN LANDMARK OVERRIDE - THIS RULE OUTRANKS EVERY "KEEP THE SETTING" INSTRUCTION ABOVE:
The photograph contains a copyrighted modern landmark building: %s.
You must NOT reproduce that building, not even in simplified, stylised, partial or background form.
Specifically forbidden: its overall silhouette, its structural pattern, its lattice / woven / mesh / diagrid / exoskeleton facade, its curved shell, its distinctive roof profile - anything by which a viewer could name the building.
Instead, rebuild the same place WITHOUT it: keep the open plaza or ground, the paving, the sky, the trees and shrubs, the street lamps, the plain white event canopies, the fence and the people, and let plain generic low trees or an ordinary treeline fill the space where the building used to be.
An anonymous, unremarkable skyline is required. If in doubt, leave that area as empty sky."""


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
    parts += [COMPLIANCE % ", ".join(ip), IP_POLICY, SCENE_OUTPUT, SCENE_NEGATIVE, STYLE_REMINDER]
    # 地标禁令必须排在 SCENE_OUTPUT / STYLE_REMINDER 之后，否则会被「保持原场景」压过去
    lm = _strlist(info.get("modern_landmark_items"))
    if lm:
        parts.append(SCENE_LANDMARK_OVERRIDE % ", ".join(lm))
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
  "elements_with_support_rig": ["..."], // 【通用支架检查·不看清单只看画面】逐枚判断：这枚元素里是否包含【与主体无关的支撑结构】
                                  // —— 支架、三脚架、立杆、挂架、托架、底座板、吊钩、货架、桌面。
                                  // 只要有，就把这枚元素的名字写进来。没有则空数组。
                                  // ⚠️ 物体自身固有的部分不算（杯子的把手、相机的镜头、蛋糕自带的小盘子都不算）
  "composite_elements": [["A","B"]], // 【通用复合体检查】逐枚判断：这枚元素 A 是否由【多个可独立成立的部件】组成
                                  // （一整套器材、一堆零件、机器+产出物）。若是，B 填其中最有代表性、
                                  // 最厚实、能单独成立的那一个部件。不是复合体就不要写。没有则空数组
  "banned_ip_or_landmark": ["..."], // 【IP 合规复检】画面中是否出现：卡通吉祥物/玩偶/手办/盲盒形象、主题乐园元素、
                                  // 文创商品原创设计、商标字标或品牌名，或【现代地标建筑本体】
                                  // （体育场馆/摩天楼/电视塔/会展中心/歌剧院/商场/城市天际线）。
                                  // 有就写出来。⚠️ 古建筑本体（城墙/古塔/飞檐/石狮/亭子）和自然景观不算违规，不要写
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
    _thin = _strlist(d.get("thin_parts"))
    if _thin:
        fails.append("以下结构仍过细，必须加粗（禁止删除物品本体）：%s" % ", ".join(_thin))
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
    with_c = _strlist(d.get("objects_with_container"))
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
    # 通用支架复检（不依赖词库）：词表永远追不上新品类（drum kit / mic stand / 扭蛋机 /
    # 乐高展台…），所以直接问模型「这枚元素里有没有与主体无关的支撑结构」。
    rig = _strlist(d.get("elements_with_support_rig"))
    if rig:
        fails.append("以下元素画进了与主体无关的支撑结构（支架/立杆/托架/底座板/挂架）：%s。"
                     "模切剪不出这种结构，必须只画物体本体，让它单独悬空呈现"
                     % ", ".join(rig))
    # 通用复合体复检：问出「最有代表性的那个部件」，下一轮直接改画该部件，
    # 不再靠 COMPOSITE_REPLACE 词库覆盖。成对结果会被主流程回填进 info["_composite_parts"]。
    comp = [(a.strip(), b.strip()) for a, b in _pairs_from(d.get("composite_elements"))
            if a.strip() and b.strip()]
    if comp:
        fails.append("以下元素是由多个部件组成的复合体，贴纸做不了，必须改画成括号里那一个单件：%s"
                     % "、".join("%s（改画 %s）" % (p[0], p[1]) for p in comp))
    # IP 合规复检：选品阶段已硬剔除，但生图模型会自己把地标/吉祥物补进画面
    bad_ip = _strlist(d.get("banned_ip_or_landmark"))
    if bad_ip:
        fails.append("出现 IP 合规禁止内容：%s。卡通吉祥物/玩偶/主题乐园元素/文创设计/商标字标、"
                     "以及现代地标建筑本体（体育场馆/摩天楼/电视塔/商场/天际线）一律不许画；"
                     "改画随身物品（门票/地图/水壶/背包/帽子/食物/落叶）或自然景观、古建筑构件"
                     % ", ".join(bad_ip))
    miss = _strlist(d.get("missing_objects"))
    if miss:
        fails.append("以下指定物品没有画出来：%s。这些是顾客照片里的纪念物，必须补画成独立贴纸；"
                     "若因为太细而被省略，请加粗放大后重画，不得用其它物品替代"
                     % ", ".join(miss))
    d["fails"] = fails
    return d

# ── 收敛保证（R2）─────────────────────────────────────────────────────────
# 红队 R2：CHANGELOG 的 drum kit 案例里，整套架子鼓同时触发「结构过细」和
# 「自带支架」两条【互斥】质检 —— 一条要求加粗保留，另一条要求整体去掉。
# 原实现只会把两条 fail 原样喂回模型再画一次，4 轮全败，永远交付不出来。
#
# 结论：靠继续堆词库不可能收敛，必须让【失败可收敛】成为结构性保证：
#   同一物体撞墙两次 → 永久剔除并换候选；一轮内撞上互斥规则 → 立刻换，不等第二轮；
#   谁都归因不出来但整组连续失败 → 主动换掉优先级最低那枚，绝不空转。
FAIL_TAGS = (
    ("too_thin", ("仍过细", "结构过细", "必须加粗", "太细")),
    ("support_rig", ("支撑结构", "大面积承载物", "支架", "底座")),
    ("composite", ("复合体", "改用单件", "改画成括号里")),
    ("ip", ("IP 合规禁止内容", "可读文字或品牌标识", "品牌标识")),
    ("duplicate", ("重复元素", "轮廓雷同")),
    ("missing", ("没有画出来",)),
)
# 互斥规则对：同一物体同时命中其中一对，就说明这枚元素本身不可能同时满足两条规则，
# 原地重试是纯粹的浪费（这正是 drum kit 4 轮全败的成因）。
CONTRADICTORY_TAG_PAIRS = (
    {"too_thin", "support_rig"},   # 「加粗这些细杆」vs「这些细杆整个去掉」
    {"too_thin", "composite"},     # 「加粗保留」vs「整体换成单件」
    {"missing", "ip"},             # 「必须补画这枚」vs「这枚一律不许画」
    {"missing", "support_rig"},    # 「必须补画」vs「它只剩支架可画」
)


def fail_tags(text):
    """把一条质检不合格文案归类成规则标签集合。用于判断互斥与连续失败。"""
    t = str(text)
    return {tag for tag, keys in FAIL_TAGS if any(k in t for k in keys)}


def objects_in_fail(text, names):
    """这条 fail 涉及哪些候选物体。

    质检文案里嵌的是英文物体名（视觉模型原样回填），所以先整名匹配，
    再退回用【中心词】匹配（模型常把 "bass drum" 写成 "drum"）。
    中心词要求 ≥4 字符，避免 "cup"/"bag" 这类短词在长文案里乱命中。
    """
    low = " %s " % _norm(text)
    hit = []
    for nm in names:
        k = _norm(nm)
        if not k:
            continue
        if (" %s " % k) in low or k in low:
            hit.append(nm); continue
        head = k.split()[-1]
        if len(head) >= 4 and ((" %s " % head) in low or (" %ss " % head) in low):
            hit.append(nm)
    return hit


def candidate_pool(info, banned=None):
    """全部【合法且未被剔除】的候选物体（已拆解、已过 IP 硬禁止、已去重）。

    收敛判断要用它：只有池子里还有没试过的物体，换元素才有意义。
    """
    banned = {_norm(b) for b in (banned or [])}
    cmap = composite_map(info)
    flags = {_norm(x) for x in _strlist(info.get("ip_items"))}
    flags |= {_norm(x) for x in _strlist(info.get("modern_landmark_items"))}
    out, seen = [], set()
    for o in info.get("standalone_objects") or []:
        o2, _ = _decompose(o, cmap)
        k = _norm(o2)
        if not k or k in seen or k in banned:
            continue
        if _ip_reason(o2) or k in flags or _norm(o) in flags:
            continue
        seen.add(k); out.append(o2)
    return out


class ConvergenceGuard:
    """让「失败」一定收敛的看门人。

    用法（main 里每轮质检之后调一次）：
        picked, logs, evicted = guard.after_round(picked, fails, visual_json)

    三条规则：
      R-a 同一物体连续 strikes(默认 2) 轮触发质检失败 → 永久剔除，换下一个候选；
      R-b 同一物体在一轮内同时触发互斥规则 → 立即剔除，不等第二轮；
      R-c 一轮下来归因不到任何物体、但同一组元素已连续失败 → 换掉优先级最低那枚。
    保证：只要候选池还有没试过的物体，就绝不会出现「max-rounds 跑完仍是同一组失败元素」。
    只有池子真的见底时才会保持原组重试（此时换也没得换，日志会明说）。
    """

    def __init__(self, info, n_obj, strikes=2):
        self.info = info
        self.n_obj = n_obj
        self.strikes = strikes
        self.banned = []          # 有序，报告里按剔除顺序展示
        self.strike = {}          # _norm(name) → 连续触发失败的轮数
        self.combo_fails = 0      # 同一组元素连续失败的轮数
        self.last_combo = None
        self.events = []          # [{round, object, reason, replaced_by}]
        self.round = 0

    # 内部：优先级最低的那一枚（文字依赖件 > 古建筑 > 普通 > 纪念物 > 随身物）
    def _lowest(self, picked):
        keep, carried = _rank_ctx(self.info)
        ranked = sorted(picked, key=lambda o: (-_rank_of(o, keep, carried),
                                               -picked.index(o)))
        return ranked[0] if ranked else None

    def _has_spare(self, picked):
        cur = {_norm(p) for p in picked}
        return any(_norm(c) not in cur for c in candidate_pool(self.info, self.banned))

    def after_round(self, picked, fails, visual=None):
        """返回 (新的 picked, 日志行, 本轮被剔除的物体名)。"""
        self.round += 1
        logs = []
        if not fails:
            return picked, logs, []

        # ① 视觉模型这一轮给出的复合体拆解直接吃进 info：下一轮 _decompose 就会用上，
        #    这是「不靠词库」处理新品类的关键一步（lego set → lego brick 等）。
        map_changed = False
        for whole, part in _pairs_from((visual or {}).get("composite_elements")):
            if str(whole).strip() and str(part).strip():
                self.info.setdefault("_composite_parts", []).append([whole, part])
                map_changed = True
                logs.append("复合体拆解（视觉模型判定，非词库）：%s → 下一轮改画单件 %s"
                            % (whole, part))

        # ② 归因：每条 fail 属于哪几条规则、涉及哪些物体
        tagmap = {}
        for f in fails:
            tags = fail_tags(f)
            if not tags:
                continue                      # 全局项（色板/边框/枚数）不归因到物体
            for o in objects_in_fail(f, picked):
                tagmap.setdefault(o, set()).update(tags)

        evict = []
        for o in picked:
            k = _norm(o)
            tags = tagmap.get(o) or set()
            if not tags:
                self.strike.pop(k, None)      # 「连续」的语义：本轮没事就清零
                continue
            clash = next((pair for pair in CONTRADICTORY_TAG_PAIRS if pair <= tags), None)
            self.strike[k] = self.strike.get(k, 0) + 1
            if clash:
                evict.append((o, "一轮内同时触发互斥规则（%s），原地重试不可能收敛"
                                 % " + ".join(sorted(clash))))
            elif self.strike[k] >= self.strikes:
                evict.append((o, "连续 %d 轮不收敛（%s）"
                                 % (self.strike[k], "、".join(sorted(tags)))))

        # ③ 全局收敛兜底：这一组已经连续失败，却归因不到任何物体 → 也要换，不许空转
        combo = tuple(sorted(_norm(o) for o in picked))
        self.combo_fails = self.combo_fails + 1 if combo == self.last_combo else 1
        self.last_combo = combo
        if not evict and self.combo_fails >= self.strikes:
            v = self._lowest(picked)
            if v:
                evict.append((v, "同一组元素已连续 %d 轮不合格且无法归因到具体物体，"
                                 "主动换元素避免空转" % self.combo_fails))

        if not evict:
            # 没有要剔除的，但模型给了新的复合体拆解 → 元素名会变（lego set → lego brick），
            # 必须把 picked 重算一遍，否则 main 手里的清单和 prompt 里的不一致
            if map_changed:
                new_picked, _d = select_objects(self.info, self.n_obj, self.banned)
                if [_norm(x) for x in new_picked] != [_norm(x) for x in picked]:
                    return new_picked, logs, []
            return picked, logs, []

        # ④ 池子见底时不做剔除：换不出替补还硬剔，只会让整版少一枚（枚数是硬约束）
        if not self._has_spare(picked):
            logs.append("⚠️ 候选池已用尽（%d 个候选全试过或全被剔除），"
                        "无法换元素，只能在现有元素上继续重试：%s"
                        % (len(candidate_pool(self.info)),
                           "；".join("%s —— %s" % (o, why) for o, why in evict)))
            return picked, logs, []

        before = {_norm(p) for p in picked}
        for o, _why in evict:
            self.banned.append(o)
            self.strike.pop(_norm(o), None)
        new_picked, _dropped = select_objects(self.info, self.n_obj, self.banned)
        added = [p for p in new_picked if _norm(p) not in before]
        for i, (o, why) in enumerate(evict):
            rep = added[i] if i < len(added) else None
            if rep:
                logs.append("物体 %s %s，已替换为 %s" % (o, why, rep))
            else:
                logs.append("物体 %s %s，已永久剔除（本轮没有同级替补可换）" % (o, why))
            self.events.append({"round": self.round, "object": o,
                                "reason": why, "replaced_by": rep})
        self.combo_fails = 0
        self.last_combo = tuple(sorted(_norm(o) for o in new_picked))
        return new_picked, logs, [o for o, _ in evict]

    def drop_stale_patches(self, fails, evicted):
        """被换掉的物体对应的重试指令要一起丢掉，否则会让模型去修一枚已经不存在的元素。"""
        if not evicted:
            return list(fails)
        return [f for f in fails if not objects_in_fail(f, evicted)]


# ── 断点续跑（缺陷 B · 2026-09-02）──────────────────────────────────────────
# 实测代价：v36 那一轮 01 生日跑了 5 次才成功，其中 2 次是 provider `400 Client Error`
# 打断的 —— 每次都从 G0 重头开始，已经成功的 G0 调用和已经跑完的前几轮生图全部作废。
# 一轮生图约 1 元 token + 数分钟，白烧的是真钱和真时间。
#
# 断点的粒度选「轮」而不是「单」：
#   · G0 结果（preflight.json）—— 只要文件有效就复用，不再调视觉模型
#   · 每一轮的生图 / 重排 / 双重质检结果 —— 以【该轮 prompt 的 sha256】为键，
#     prompt 一个字节不同就必须重跑（prompt 变了产物就不再对应，复用即造假）
#
# 为什么不按「文件存在」就复用：round2.png 存在不代表它是当前这组元素画出来的。
# ConvergenceGuard 换元素后 prompt 会变，那时候旧图必须作废。所以键是 prompt 摘要。
RESUME_FILE = "resume_state.json"


def prompt_sig(text):
    """prompt 的内容摘要。取前 16 位十六进制，够用且日志里能看。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def load_resume(outdir):
    """读断点。读不出来就当没有断点（宁可多跑一轮，不能拿坏数据当产物）。"""
    p = os.path.join(outdir, RESUME_FILE)
    if not os.path.isfile(p):
        return {"rounds": {}}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("rounds"), dict):
            return d
    except Exception as e:
        log("  🟡 断点文件 %s 读不出来（%s），本次按无断点处理" % (RESUME_FILE, e))
    return {"rounds": {}}


def save_resume(outdir, state):
    p = os.path.join(outdir, RESUME_FILE)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:                                  # 存不下也不该弄崩这一单
        log("  🟡 断点写入失败（%s），续跑能力本次不可用" % e)


def record_round(state, rnd, prompt, png, sheet, rmeta, q, v, outdir):
    """把这一轮的产物与质检结论写进断点。路径一律存相对 outdir 的文件名。"""
    q2 = {k: val for k, val in (q or {}).items() if k != "raw"}   # raw 是几十 KB 的报告全文
    state.setdefault("rounds", {})[str(rnd)] = {
        "prompt_sig": prompt_sig(prompt),
        "png": os.path.basename(png) if png else None,
        "sheet": os.path.basename(sheet) if sheet else None,
        "relayout": rmeta or {},
        "quant": q2,
        "visual": v,
    }
    save_resume(outdir, state)
    return state


def round_cache(state, rnd, prompt, outdir):
    """这一轮能不能直接复用？返回 (记录 or None, 说明)。

    四个条件全部满足才复用：断点里有这一轮 / prompt 摘要一致 /
    出图与重排图两个文件都还在 / 量化与目视结论都记全了。
    任何一条不满足就重跑 —— 复用一个说不清来源的产物比多花一轮更糟。
    """
    rec = (state.get("rounds") or {}).get(str(rnd))
    if not isinstance(rec, dict):
        return None, "无断点记录"
    if rec.get("prompt_sig") != prompt_sig(prompt):
        return None, "prompt 已变化（换过元素或改过参数），旧产物作废"
    for key in ("png", "sheet"):
        name = rec.get(key)
        if not name or not os.path.isfile(os.path.join(outdir, name)):
            return None, "产物文件 %s 已不在" % (name or key)
    if not isinstance(rec.get("quant"), dict) or not isinstance(rec.get("visual"), dict):
        return None, "断点里缺质检结论"
    return rec, "prompt 未变且产物齐全"


def cached_preflight(outdir):
    """已有的 preflight.json 能不能复用？返回 (info or None, 说明)。"""
    p = os.path.join(outdir, "preflight.json")
    if not os.path.isfile(p):
        return None, "无 preflight.json"
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, "preflight.json 不是合法 JSON（%s）" % e
    if not isinstance(data, dict):
        return None, "preflight.json 不是 JSON 对象"
    if not _strlist(data.get("standalone_objects")):
        return None, "preflight.json 里 standalone_objects 为空（G0 那次本来就没成功）"
    if not data.get("_short_edge_px"):
        return None, "preflight.json 缺 _short_edge_px（旧版本产物，分辨率门禁会失效）"
    return data, "%d 个候选物品" % len(_strlist(data.get("standalone_objects")))


def _log_reused(reused):
    """把「本次复用了哪些已有产物」汇总打印。

    必须打：续跑时如果日志和全新跑一模一样，人会误以为这些图是这次重新生成的，
    进而误判「产线稳定」或「问题已复现」。v36 复盘时这类误判发生过。
    """
    if not reused:
        return
    log("\n【续跑汇总】本次复用了以下已有产物，未重新生成：")
    for r in reused:
        log("   ♻️ %s" % r)
    log("   （要强制全部重新生成：同一条命令加 --fresh）")


def resolve_preflight(photo, outdir, work, fresh=False):
    """G0 入口：能复用就复用，否则真跑一次。返回 (info, small, 复用说明 or None)。

    抽成函数而不是写在 main() 里，是为了能离线回归测试
    「preflight.json 存在时不重复调用视觉模型」「--fresh 能强制重跑」这两条。
    """
    info, why = cached_preflight(outdir)
    if info is not None and not fresh:
        small = shrink(photo, os.path.join(work, "src_small.jpg"))
        note = "preflight.json（G0 结果，%s）—— 本次未调用视觉模型" % why
        log("  ♻️ 复用已有 G0：%s" % note)
        log("     要强制重跑 G0 请加 --fresh")
        return info, small, note
    if fresh and os.path.isfile(os.path.join(outdir, "preflight.json")):
        log("  --fresh：忽略已有 preflight.json，重新调用视觉模型跑 G0")
    elif info is None and os.path.isfile(os.path.join(outdir, "preflight.json")):
        log("  🟡 已有 preflight.json 不可用（%s），重新跑 G0" % why)
    info, small = preflight(photo, work)
    return info, small, None


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
    # 默认值取 providers.TARGET_DPI（那边由「mm ÷ 25.4 × dpi」公式推导，见 R3 注释），
    # 这里不再各写一个 400 —— 两处魔数早晚会不一致。
    ap.add_argument("--dpi", type=int, default=providers.TARGET_DPI,
                    help="重排后成品分辨率，默认 %d（provider 达不到时自动降级到 %d 并标注）"
                         % (providers.TARGET_DPI, providers.FALLBACK_DPI))
    ap.add_argument("--skip-selftest", action="store_true",
                    help="跳过跑图前的 provider 自检（不建议；自检不花钱）")
    ap.add_argument("--selftest-offline", action="store_true",
                    help="自检时跳过联网检查（内网/代理环境）")
    ap.add_argument("--strikes", type=int, default=2,
                    help="同一物体连续几轮触发质检失败就永久剔除并换候选（收敛保证，默认 2）")
    ap.add_argument("--objects", default=None,
                    help="显式指定这一批要画的物品清单（分号分隔），跳过 G0 自动选物。"
                         "供 forge_a3.py 分批调度使用，保证 4 批之间不重复")
    ap.add_argument("--exclude", default=None,
                    help="其它批次已画的图案（分号分隔）。本批禁止重复，也禁止把它们"
                         "作为本批物品的一部分画进来（避免多枚都出现同一个甜点盘）")
    ap.add_argument("--allow-people", choices=["auto", "no"], default="auto",
                    help="no = 这一批不出人物（分批时只让其中一批出人物，避免 4 批都有人）")
    ap.add_argument("--fresh", "--no-resume", dest="fresh", action="store_true",
                    help="忽略 --outdir 里已有的断点（preflight.json / 各轮产物），"
                         "强制从 G0 重头跑。默认行为是自动续跑并在日志里说明复用了什么")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    work = os.path.join(a.outdir, "_work"); os.makedirs(work, exist_ok=True)

    # 断点：默认续跑，--fresh 时整份丢掉重来（不删文件，生图会覆盖同名产物）
    state = {"rounds": {}} if a.fresh else load_resume(a.outdir)
    reused = []          # 本次复用了哪些已有产物，收尾时汇总打印

    log("═" * 66)
    log("memory-sticker-forge  ·  %s  ·  目标 %d 枚元素" % (os.path.basename(a.photo), a.elements))
    log("provider: %s  |  人物画法: %s" % (providers.describe(), a.figure_style))
    log("续跑    : %s" % ("关（--fresh，强制从 G0 重头跑）" if a.fresh else
                          "开（复用已有产物；要从头跑加 --fresh）"))
    log("═" * 66)

    # ── R3/R1：跑图前的启动期自检（不消耗任何生图额度）────────────────────
    # ① 能力/余量表：各 provider 像素能力 vs 400/300dpi 需求，一眼看到还有多少余量
    # ② 链路自检 fail-fast：缺 key / base_url 不通 / 模型 ID 对不上 / 请求体不合法
    #    → 直接退出，不要跑到第 3 轮才 404
    log("\n【自检】provider 能力与链路（不花钱）")
    log(providers.capability_table(a.dpi))
    if a.preflight_only or a.skip_selftest:
        log("  （已跳过链路自检：%s）"
            % ("--preflight-only 不会生图" if a.preflight_only else "--skip-selftest"))
    else:
        code = providers.print_selftest(
            offline=True if a.selftest_offline else None, requested_dpi=a.dpi)
        if code != 0:
            log("\n❌ provider 自检不通过，已在生图前停止（未消耗任何生成额度）。")
            return 3

    # 分辨率能力探测 → 显式降级：达不到 400dpi 就按 300dpi 交付并全程标注，
    # 绝不静默插值假装 400dpi（那是唯一一类「所有自动检查全过、实物不能用」的失败）
    plan = providers.dpi_plan(requested_dpi=a.dpi)
    dpi = plan.get("dpi") or a.dpi
    if plan.get("dpi") is None:
        log("  🔴 %s" % plan.get("reason"))
        if not a.preflight_only:
            return 3
    elif plan.get("degraded"):
        log("  🟡 分辨率降级：本单按 %d dpi 交付（请求 %d dpi）。原因：%s"
            % (dpi, a.dpi, plan.get("reason")))
        log("     ⚠️ 300dpi 对不干胶模切可接受，但交付说明与对客沟通里必须写明本单为 %d dpi。" % dpi)
    else:
        log("  ✅ 分辨率：本单按 %d dpi 交付" % dpi)

    # G0
    log("\n【G0】照片体检 + 场景解析")
    info, small, g0_reused = resolve_preflight(a.photo, a.outdir, work, fresh=a.fresh)
    if g0_reused:
        reused.append(g0_reused)
    n_obj, n_ppl = plan_mix(info, a.elements)
    log("  像素      : %s（短边 %dpx）" % (info.get("_size_px"), info.get("_short_edge_px") or 0))
    log("  场景     : %s" % info.get("scene"))
    log("  光线      : %s  → %s色板" % (info.get("lighting"), "夜场" if info.get("lighting") == "night" else "日间"))
    log("  人数      : %s" % info.get("people_count"))
    log("  可独立物品 : %s" % ", ".join(info.get("standalone_objects", [])))
    log("  纤细件     : %s" % (", ".join(info.get("thin_parts", [])) or "无"))
    log("  第三方 IP  : %s" % (", ".join(info.get("ip_items", [])) or "无"))
    log("  现代地标    : %s（受著作权保护的建筑作品，本体一律不画）"
        % (", ".join(info.get("modern_landmark_items") or []) or "无"))
    log("  ⭐随身物    : %s" % (", ".join(info.get("carried_items") or []) or "（G0 未标，按词库判定）"))
    _rok, _rn, _rcats, _rdesc = g0_recall_report(info.get("standalone_objects"))
    log("  候选池     : %s %s（下限 %d 项 / %d 类，不足会自动补问 G0）"
        % (_rdesc, "✅" if _rok else "🟡 偏薄", G0_MIN_OBJECTS, G0_MIN_CATEGORIES))

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
        # 回填告警不是「剔除」，得单独一个前缀，否则日志自相矛盾、事后很难看懂
        if dp.startswith("⚠️"):
            log("  ▸ 选品告警 : %s" % dp)
        else:
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
        _log_reused(reused)
        return 0

    patches, history = [], []
    guard = ConvergenceGuard(info, n_obj, strikes=max(1, a.strikes))
    for rnd in range(1, a.max_rounds + 1):
        log("\n【第 %d 轮】生成 → %s双重质检"
            % (rnd, "" if a.no_relayout else "程序化重排 → "))
        prompt = build_prompt(info, a.elements, patches, a.figure_style,
                              [e.strip() for e in a.exclude.split(';') if e.strip()] if a.exclude else None,
                              banned=guard.banned)
        with open(os.path.join(a.outdir, "prompt_round%d.txt" % rnd), "w",
                  encoding="utf-8") as f:
            f.write(prompt)

        # ── 断点续跑：这一轮的产物能复用就不重复烧额度（缺陷 B）──────────
        cache, why = round_cache(state, rnd, prompt, a.outdir)
        if cache:
            png = os.path.join(a.outdir, cache["png"])
            sheet = os.path.join(a.outdir, cache["sheet"])
            rmeta = cache.get("relayout") or {}
            q = dict(cache["quant"]); q.setdefault("fails", [])
            q["raw"] = "（复用断点，本轮未重跑量化质检）"
            v = cache["visual"]
            note = ("round%d 的生图 + 重排 + 双重质检结果（%s / %s）"
                    % (rnd, os.path.basename(png), os.path.basename(sheet)))
            reused.append(note)
            log("  ♻️ 复用第 %d 轮已有产物（%s）：%s + %s"
                % (rnd, why, os.path.basename(png), os.path.basename(sheet)))
            log("     未重复生图、未重复质检。要强制重跑请加 --fresh")
            log("  量化 : %s dpi | 必须修 %s | 元素 %s/刀线 %s | 最小邻距 %s mm（复用）"
                % (q.get("dpi"), q.get("must_fix"), q.get("n_elem"),
                   q.get("n_cut"), q.get("min_gap")))
            log("  目视 : 共 %s 枚 | 纯物品 %s 枚 (%s)（复用）"
                % (v.get("n_elements"), v.get("n_object_only"),
                   ", ".join(_strlist(v.get("object_names"))[:8])))
        else:
            if (state.get("rounds") or {}).get(str(rnd)):
                log("  ▸ 第 %d 轮不可复用（%s），重新生成" % (rnd, why))
            try:
                png = generate(small, prompt, a.outdir, str(rnd))
            except SystemExit as e:
                # provider 已在内部按 2/4/8 秒指数退避重试过；到这里说明真的没恢复。
                # 关键是【不要丢掉断点】：G0 与前面几轮的产物都已落盘，重跑同一条
                # 命令就能续上，不用再从 G0 烧一遍（v36 01 生日白烧 2 次就是这么来的）。
                log("\n%s" % e)
                log("\n❌ 第 %d 轮生图失败，本单中止。" % rnd)
                log("   ♻️ 断点已保存：G0 结果与第 1~%d 轮产物都在 %s，"
                    "直接重跑同一条命令即可续跑（会自动复用，不重复烧额度）；"
                    "要从头跑请加 --fresh" % (rnd - 1, a.outdir))
                _log_reused(reused)
                save_resume(a.outdir, state)
                if history:
                    write_report(a.outdir, info, history, rnd - 1, False, dpi=dpi, guard=guard)
                return 4
            log("  出图 : %s" % os.path.basename(png))

            # 排版交给代码，不交给模型。重排失败才退回模型原图。
            sheet, rmeta = png, {}
            if not a.no_relayout:
                rp, rmeta, rlog = do_relayout(png, a.outdir, str(rnd), a.gap, a.margin, dpi)
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
                % (v.get("n_elements"), v.get("n_object_only"), ", ".join(_strlist(v.get("object_names"))[:8])))
            # 质检结论已经拿到，立刻落断点：下一轮若被 provider 打断，这一轮不必重跑
            record_round(state, rnd, prompt, png, sheet, rmeta, q, v, a.outdir)

        fails = q["fails"] + v["fails"]
        history.append({"round": rnd, "png": os.path.basename(sheet),
                        "relayout": ({k: rmeta.get(k) for k in
                                      ("n_elements", "cols", "rows", "min_gap_mm",
                                       "min_margin_mm", "shrink_k")} if rmeta else None),
                        "quant": {k: q.get(k) for k in
                        ("dpi", "must_fix", "n_elem", "n_cut", "min_gap")}, "visual": v, "fails": fails})
        if not fails:
            log("  ✅ 双重质检全过")
            final = os.path.join(a.outdir, "FINAL.png"); shutil.copy(sheet, final)
            prod = os.path.join(a.outdir, "production")
            rp2 = subprocess.run([sys.executable, DOCTOR, os.path.abspath(final),
                                  "--sheet-width", str(SHEET_WIDTH_MM),
                                  "--outdir", os.path.abspath(prod)],
                                 capture_output=True, text=True, cwd=os.path.dirname(DOCTOR))
            write_report(a.outdir, info, history, rnd, True, dpi=dpi, guard=guard)
            log("\n🎉 交付：%s（第 %d 轮通过，%d dpi%s）"
                % (final, rnd, dpi, "，⚠️ 已降级，交付说明须标注" if plan.get("degraded") else ""))
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
            _log_reused(reused)
            return 0

        log("  🔴 不合格 %d 项：" % len(fails))
        for f in fails: log("     · %s" % f)

        # ── 收敛保证：不允许拿着同一组失败元素把 max-rounds 跑光（红队 R2）──
        new_picked, glogs, evicted = guard.after_round(picked, fails, v)
        for gl in glogs:
            log("  ♻️ 收敛 : %s" % gl)
        patches = guard.drop_stale_patches(fails, evicted) if evicted else fails
        if [_norm(x) for x in new_picked] != [_norm(x) for x in picked]:
            picked = new_picked
            n_obj, n_ppl = len(picked), a.elements - len(picked)
            guard.n_obj = n_obj
            log("  ♻️ 本版改为 : %s" % ", ".join(picked))
            log("  ♻️ 版面构成 : %d 枚纯物品 + %d 枚含人物 = %d 枚" % (n_obj, n_ppl, a.elements))
        history[-1]["picked"] = list(picked)
        history[-1]["evicted"] = evicted

    log("\n❌ %d 轮仍未通过，不交付。见 qc_report.md" % a.max_rounds)
    write_report(a.outdir, info, history, a.max_rounds, False, dpi=dpi, guard=guard)
    _log_reused(reused)
    return 2

def write_report(outdir, info, history, rounds, passed, dpi=None, guard=None):
    L = ["# 质检报告\n",
         "**结果**：%s（共 %d 轮）\n" % ("✅ 通过并交付" if passed else "🔴 未通过，未交付", rounds)]
    if dpi:
        L.append("**交付分辨率**：%d dpi%s\n"
                 % (dpi, "" if dpi >= providers.TARGET_DPI else
                    "（⚠️ 已从 %ddpi 显式降级：provider 像素能力或尺寸配置不足。"
                    "%ddpi 对不干胶模切可接受，但必须让模切店和客户知道）"
                    % (providers.TARGET_DPI, dpi)))
    L += ["## G0 照片体检\n",
          "| 项 | 值 |", "|---|---|",
          "| 场景 | %s |" % info.get("scene"),
          "| 光线判定 | %s |" % info.get("lighting"),
          "| 人数 | %s |" % info.get("people_count"),
          "| 可独立物品 | %s |" % ", ".join(info.get("standalone_objects", [])),
          "| 第三方 IP（已强制剔除） | %s |" % (", ".join(info.get("ip_items", [])) or "无"),
          "| 现代地标建筑（已强制剔除） | %s |" % (", ".join(info.get("modern_landmark_items") or []) or "无"),
          "| ⭐随身物（最高优先级） | %s |" % (", ".join(info.get("carried_items") or []) or "按词库判定"),
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
                 % (v.get("n_elements"), v.get("n_object_only"), ", ".join(_strlist(v.get("object_names"))[:8])))
        if h["fails"]:
            L.append("- 🔴 不合格项：")
            L += ["  - %s" % f for f in h["fails"]]
        else:
            L.append("- ✅ 双重质检全过")
        if h.get("evicted"):
            L.append("- ♻️ 本轮换元素：剔除 %s" % ", ".join(h["evicted"]))
        if h.get("picked"):
            L.append("- 下一轮元素清单：%s" % ", ".join(h["picked"]))
        L.append("")
    if guard is not None and (guard.events or guard.banned):
        L.append("## 收敛过程（不可收敛检测）\n")
        L.append("| 轮次 | 被永久剔除的物体 | 原因 | 替换为 |")
        L.append("|---|---|---|---|")
        for e in guard.events:
            L.append("| %d | %s | %s | %s |"
                     % (e["round"], e["object"], e["reason"], e.get("replaced_by") or "（无替补）"))
        L.append("")
        L.append("剩余候选池：%s\n" % (", ".join(candidate_pool(info, guard.banned)) or "已用尽"))
    with open(os.path.join(outdir, "qc_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))

if __name__ == "__main__":
    sys.exit(main())
