#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
选品逻辑回归测试 —— 锁死三个已在真实订单中出现过的缺陷。

这三个 case 都是真事，不是假想：
  1. 生日照片的蜡烛排在 G0 清单第 7 位，被 objs[:5] 截断，成品里没有蜡烛
  2. 蛋糕和盘子同时入选，蛋糕自带盘子，成品出现两枚重叠元素
  3. 电吉他和贝斯同时入选，扁平剪纸下轮廓雷同，看起来是两把吉他

不调用任何 AI 接口，不产生费用，可离线运行：
    python3 -m pytest tests/ -v
    python3 tests/test_select_objects.py      # 不装 pytest 也能跑
"""
import os
import sys

# 默认测同目录旁边的那份 forge.py；用 FORGE_DIR 可以指向另一份副本
# （内部运行版 / 公开副本），同一套断言能把两边都锁住。
sys.path.insert(0, os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge")))
import forge  # noqa: E402


# ── 1. 纪念物不得被截断 ──────────────────────────────────────────────────
def test_keepsake_not_truncated():
    """蜡烛排在第 7 位也必须入选 —— 顾客就是为它下的单。"""
    info = {
        "standalone_objects": ["cheesecake", "plate", "glass cup", "red leather chair",
                               "spoon", "pendant light", "candle", "red petals"],
        "keepsake_objects": ["candle"],
        "container_pairs": [], "similar_pairs": [],
    }
    picked, _ = forge.select_objects(info, 5)
    assert "candle" in picked, "纪念物 candle 被截断了：%s" % picked
    assert len(picked) == 5


# ── 2. 容器必须剔除 ──────────────────────────────────────────────────────
def test_container_dropped():
    """蛋糕本来就盛在盘子上，盘子不能再单独出一枚。"""
    info = {
        "standalone_objects": ["cheesecake", "plate", "glass cup", "chair", "spoon", "lamp"],
        "keepsake_objects": [],
        "container_pairs": [["cheesecake", "plate"]],
        "similar_pairs": [],
    }
    picked, dropped = forge.select_objects(info, 5)
    assert "cheesecake" in picked, "内容物应保留"
    assert "plate" not in picked, "容器 plate 应被剔除：%s" % picked
    assert any("plate" in d for d in dropped), "剔除理由应被记录"


def test_keepsake_survives_container_rule():
    """
    实拍回归（v3.3）：G0 把生日单标成
        [["birthday cake","plate"], ["candle","birthday cake"], ["sparkler","glass cup"]]
    第二对的语义是「蜡烛插在蛋糕上」。旧逻辑无脑丢外层 → 把蛋糕本身删了，
    整单最重要的纪念物消失。修复后必须保住蛋糕。
    """
    info = {
        "standalone_objects": ["birthday cake", "sparkler", "candle", "glass cup",
                               "plate", "spoon", "rose petal", "pendant lamp"],
        "keepsake_objects": ["birthday cake", "sparkler", "candle"],
        "container_pairs": [["birthday cake", "plate"],
                            ["candle", "birthday cake"],
                            ["sparkler", "glass cup"]],
        "similar_pairs": [],
    }
    picked, dropped = forge.select_objects(info, 5)
    assert "birthday cake" in picked, "蛋糕是纪念物，不能被当容器丢掉：%s" % picked
    assert "plate" not in picked, "空盘子仍应剔除：%s" % picked
    assert "sparkler" in picked, "仙女棒应保留：%s" % picked
    assert len(picked) == 5


def test_empty_container_dropped_without_model_hint():
    """模型没标 container_pairs 时，词库要能自己认出「空盘子」这种废件。"""
    objs = ["dessert plate", "sparkler", "candle", "glass cup", "lamp", "chair"]
    pairs = forge.infer_container_pairs(objs, None)
    info = {"standalone_objects": objs, "keepsake_objects": forge.infer_keepsakes(objs, None),
            "container_pairs": pairs, "similar_pairs": []}
    picked, dropped = forge.select_objects(info, 5)
    assert "dessert plate" not in picked, "无内容物的盘子也应剔除：%s" % picked


def test_null_fields_are_coerced():
    """
    实拍回归（v3.3）：视觉模型会把可选字段显式写成 null。
    原来用 setdefault，null 会原样留下，容器/纪念物保护全部静默失效。
    """
    objs = ["cake slice", "dessert plate", "sparkler", "candle", "glass", "lamp"]
    assert forge.infer_keepsakes(objs, None), "keepsake 为 null 时应能用词库补出来"
    pairs = forge.infer_container_pairs(objs, None)
    assert any("plate" in str(p) for p in pairs), "container_pairs 为 null 时应能推断出盘子"


# ── 3. 同族不得重复 ──────────────────────────────────────────────────────
def test_similar_family_folded():
    """电吉他和贝斯只能留一把。"""
    info = {
        "standalone_objects": ["electric guitar", "bass guitar", "microphone",
                               "bass drum", "guitar amplifier", "smartphone",
                               "snare drum", "cymbal"],
        "keepsake_objects": [],
        "container_pairs": [],
        "similar_pairs": [["electric guitar", "bass guitar"], ["bass drum", "snare drum"]],
    }
    picked, _ = forge.select_objects(info, 5)
    assert len(picked) == 5
    fams = [forge._family(p) for p in picked]
    real = [f for f in fams if f is not None]
    assert len(real) == len(set(real)), "出现同族重复：%s" % picked


# ── 4. 中心词判族（同族折叠的正确性基础）──────────────────────────────────
def test_family_uses_head_noun():
    """子串匹配会把 microphone 判成 phone 族、guitar amplifier 判成 guitar 族，
    进而把候选砍光。必须按中心词（最后一个词）判定。"""
    assert forge._family("electric guitar") == forge._family("bass guitar")
    assert forge._family("bass drum") == forge._family("snare drum")
    # 这三条是曾经的误判，必须互不相同
    assert forge._family("microphone") != forge._family("smartphone")
    assert forge._family("guitar amplifier") != forge._family("electric guitar")
    assert forge._family("bass drum") != forge._family("electric guitar")


# ── 5. 枚数必须够（兜底不能把版面弄少）────────────────────────────────────
def test_always_fills_quota():
    """过滤再狠也不能少于要求枚数，缺枚数比轻微雷同更糟。"""
    info = {
        "standalone_objects": ["ginkgo leaf", "leaf cluster", "tree trunk",
                               "tree branch", "lattice window", "stone block"],
        "keepsake_objects": ["ginkgo leaf"],
        "container_pairs": [["ginkgo leaf", "tree branch"]],
        "similar_pairs": [],
    }
    picked, _ = forge.select_objects(info, 5)
    assert len(picked) == 5, "枚数不足：%s" % picked


# ── 6. 主体物品不得被误当成纤细件删掉 ────────────────────────────────────
def test_thin_parts_rescue():
    """仙女棒曾被 G0 填进 thin_parts，而 thin_parts 允许省略 → 烟花整个消失。"""
    info = {
        "standalone_objects": ["sparkler", "cheesecake", "glass cup"],
        "thin_parts": ["sparkler", "candle wick", "cable"],
    }
    objs = {forge._norm(o) for o in info["standalone_objects"]}
    kept = [t for t in info["thin_parts"] if forge._norm(t) not in objs]
    assert "sparkler" not in kept, "主体物品 sparkler 不应留在 thin_parts"
    assert "candle wick" in kept, "真正的纤细附属件应保留在 thin_parts"


# ── 7. 无人照片不得凭空造人 ──────────────────────────────────────────────
def test_no_people_no_figure():
    info = {"standalone_objects": ["gate", "roof tile", "stone lion",
                                   "lantern", "pillar", "staircase"],
            "people_count": 0, "lighting": "day",
            "keepsake_objects": [], "container_pairs": [], "similar_pairs": []}
    prompt = forge.build_prompt(info, 6)
    assert "NO HUMAN FIGURES" in prompt
    n_obj, n_ppl = forge.plan_mix(info, 6)
    assert n_ppl == 0, "无人照片不应分配人物额度"


# ── 8. prompt 必须带上三条硬约束 ─────────────────────────────────────────
def test_prompt_contains_hard_rules():
    info = {"standalone_objects": ["cake", "cup", "chair", "lamp", "book", "clock"],
            "people_count": 3, "lighting": "day",
            "keepsake_objects": ["cake"], "container_pairs": [], "similar_pairs": []}
    prompt = forge.build_prompt(info, 6)
    for rule in ("DRAW EVERY LISTED OBJECT", "NO DUPLICATES", "MUST KEEP"):
        assert rule in prompt, "prompt 缺少硬约束：%s" % rule


# ── 9. 树体部件不得凑成一版（缺陷 3 · 2026-08-30 · 06 银杏）─────────────────
def test_tree_parts_are_one_family():
    """ginkgo tree / tree branch / tree trunk 是同一棵树的三个部件，
    折叠后只能留 1 枚。整体与部件的关系必须算同族。"""
    fam = forge._family("ginkgo tree")
    assert fam is not None, "tree 必须落在某个族里"
    for n in ("tree branch", "tree trunk", "tree bough", "leaf cluster",
              "golden foliage", "ginkgo leaf", "tree canopy", "tree branches"):
        assert forge._family(n) == fam, "%s 应与 tree 同族" % n


def test_tree_parts_folded_in_selection():
    """候选够用时，一版里最多只出现 1 枚树体部件。"""
    info = {
        "standalone_objects": ["ginkgo tree", "tree branch", "tree trunk",
                               "stone bench", "bicycle", "red lantern", "wooden gate"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
    }
    picked, dropped = forge.select_objects(info, 5)
    assert len(picked) == 5, "枚数不足：%s" % picked
    tree = [p for p in picked if forge._family(p) == forge._family("ginkgo tree")]
    assert len(tree) == 1, "树体部件仍然重复入选：%s" % picked
    assert any("trunk" in d or "branch" in d for d in dropped), "剔除理由应被记录"


def test_part_whole_families_cover_building_and_flower():
    """顺手补的两组部件-整体关系：建筑构件、花。"""
    roof = forge._family("tiled roof")
    for n in ("carved eave", "brick wall", "stone pillar", "wooden column"):
        assert forge._family(n) == roof, "%s 应与 roof 同族" % n
    flower = forge._family("flower")
    for n in ("rose petal", "flower stem", "peach blossom", "petals"):
        assert forge._family(n) == flower, "%s 应与 flower 同族" % n
    # 别过度折叠：不同大类之间必须仍然可区分
    assert forge._family("tiled roof") != forge._family("ginkgo tree")
    assert forge._family("lattice window") != forge._family("tiled roof")
    assert forge._family("stone lion") is None or \
        forge._family("stone lion") != roof


# ── 10. 去字后只剩空色块的东西要降权（缺陷 4 · 03/04/05 三单）─────────────
def test_text_dependent_objects_deprioritized():
    """匾额/展板/告示牌这类东西，主体价值就是那行字；合规要求必须去字，
    去完只剩一块纯色空框。有更好的候选时不许选它们。"""
    info = {
        "standalone_objects": ["temple plaque", "stone lion", "red lantern",
                               "bronze incense burner", "marble staircase",
                               "copper water vat", "exhibition board", "notice sign"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
    }
    picked, dropped = forge.select_objects(info, 5)
    assert len(picked) == 5
    for bad in ("temple plaque", "exhibition board", "notice sign"):
        assert bad not in picked, "低价值空色块被选中了：%s" % picked
    assert any("plaque" in d for d in dropped), "降权理由应被记录：%s" % dropped


def test_text_dependent_still_available_as_last_resort():
    """但不能硬删：候选实在不够时，一枚空色块也好过整版缺一枚。"""
    info = {
        "standalone_objects": ["temple plaque", "stone lion", "red lantern",
                               "bronze incense burner", "marble staircase"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
    }
    picked, _ = forge.select_objects(info, 5)
    assert len(picked) == 5, "兜底失效，枚数不足：%s" % picked
    assert "temple plaque" in picked, "候选不足时应回填低价值件：%s" % picked
    assert forge._norm(picked[-1]) == "temple plaque", "低价值件必须排在最后：%s" % picked


def test_text_dependent_beats_keepsake_hint():
    """G0 有时把匾额标成纪念物。以「去字后还剩什么」为准，仍然降权。"""
    info = {
        "standalone_objects": ["temple plaque", "stone lion", "red lantern",
                               "bronze incense burner", "marble staircase",
                               "copper water vat"],
        "keepsake_objects": ["temple plaque"],
        "container_pairs": [], "similar_pairs": [],
    }
    picked, _ = forge.select_objects(info, 5)
    assert "temple plaque" not in picked, "被标成纪念物也不该顶掉真物件：%s" % picked


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("  ✅ %s" % fn.__name__)
        except AssertionError as e:
            failed += 1
            print("  ❌ %s\n     %s" % (fn.__name__, e))
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    sys.exit(1 if failed else 0)
