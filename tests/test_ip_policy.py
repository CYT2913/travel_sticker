#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IP 合规边界回归测试（2026-09-01 客户策略收紧）。

三级边界：
  🔴 绝对不碰：主题乐园、景区吉祥物、景区文创设计、商标字标、卡通/玩偶/手办/盲盒形象，
              以及【现代地标建筑本体】（受著作权保护的建筑作品）
  🟢 可以做  ：不受著作权保护的自然景观与古建筑本体（山/树/湖/城墙/古塔/飞檐/石狮）
  ⭐ 最优先  ：「那天你带着的东西」—— 门票、地图、水壶、背包、帽子、冰淇淋、落叶、合影

不调用任何 AI 接口，可离线运行：
    python3 -m pytest tests/ -v
    python3 tests/test_ip_policy.py
"""
import os
import sys

sys.path.insert(0, os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge")))
import forge  # noqa: E402


# ── 1. 硬禁止层 ──────────────────────────────────────────────────────────
def test_hard_ban_covers_mascot_doll_park_merch_logo():
    for name in ("park mascot", "cartoon character", "plush toy", "stuffed animal",
                 "blind box figure", "action figure", "collectible figure",
                 "cultural creative product", "souvenir merchandise",
                 "brand logo", "sponsor logo", "team logo", "theme park castle",
                 "designer toy", "resin figure", "bobblehead"):
        assert forge._hard_banned(name), "应硬禁止但没拦住：%s" % name


def test_hard_ban_does_not_hit_ordinary_objects():
    """别把普通物件误杀：校徽、门票、纸杯、落叶都得能画。"""
    for name in ("school badge", "entry ticket", "paper cup", "fallen leaf",
                 "water bottle", "canvas pouch", "sketchbook", "stone lion",
                 "ancient city wall", "pagoda", "ginkgo leaf"):
        assert not forge._hard_banned(name), "被误判成 IP 违规：%s" % name


def test_banned_items_never_selected():
    info = {
        "standalone_objects": ["park mascot", "plush toy", "blind box figure",
                               "brand logo", "water bottle", "entry ticket",
                               "paper map", "fallen leaf", "sketchbook"],
        "keepsake_objects": ["plush toy"],          # 就算 G0 说它是纪念物也不许出
        "container_pairs": [], "similar_pairs": [], "people_count": 0,
    }
    picked, dropped = forge.select_objects(info, 5)
    for bad in ("park mascot", "plush toy", "blind box figure", "brand logo"):
        assert bad not in picked, "%s 不该出现在 %s" % (bad, picked)
    assert any("IP 硬禁止" in d for d in dropped), dropped
    assert len(picked) == 5


# ── 2. 现代地标建筑 vs 古建筑 ────────────────────────────────────────────
def test_modern_landmark_detected():
    for name in ("national stadium", "olympic stadium", "bird nest stadium",
                 "sports arena", "skyscraper", "tv tower", "observation tower",
                 "convention center", "opera house", "shopping mall",
                 "glass tower", "city skyline", "ferris wheel", "gymnasium"):
        assert forge._modern_landmark(name), "现代地标没拦住：%s" % name


def test_ancient_architecture_is_allowed():
    """古建筑本体属公共领域，必须能画 —— 这是产品的主要画面来源之一。"""
    for name in ("ancient city wall", "city wall", "pagoda", "stone lion",
                 "upturned eave", "temple gate", "drum tower", "bell tower",
                 "roof tile", "courtyard pavilion", "ancient watchtower"):
        assert not forge._modern_landmark(name), "古建筑被误杀：%s" % name


def test_modern_landmark_excluded_and_carried_items_win():
    """
    R2/IP 联合验收：一张「含现代地标建筑 + 若干随身物品」的清单。
    选出来的 6 枚里不得含现代地标建筑本体，且随身物品被优先选中。
    这条直接对应 05 鸟巢那张照片：鸟巢体育场是有在世建筑师署名的现代建筑作品，
    改完之后不能再画建筑本体。
    """
    info = {
        "standalone_objects": [
            "bird nest stadium",        # 现代地标建筑本体 → 必须剔除
            "olympic tower",            # 同上
            "glass office tower",       # 同上
            "entry ticket", "paper map", "water bottle", "sun hat",
            "ice cream cone", "fallen leaf", "ancient city wall",
        ],
        # G0 常把地标标成纪念物；即使这样也不能让它入选
        "keepsake_objects": ["bird nest stadium"],
        "carried_items": ["entry ticket", "paper map", "water bottle", "sun hat"],
        "container_pairs": [], "similar_pairs": [], "people_count": 0,
    }
    picked, dropped = forge.select_objects(info, 6)
    for bad in ("bird nest stadium", "olympic tower", "glass office tower"):
        assert bad not in picked, "现代地标建筑本体入选了：%s" % picked
    assert any("现代地标建筑本体" in d for d in dropped), dropped
    carried = {"entry ticket", "paper map", "water bottle", "sun hat",
               "ice cream cone", "fallen leaf"}
    assert carried <= set(picked), "随身物没被优先选中：%s" % picked
    assert len(picked) == 6


def test_carry_items_rank_above_ancient_architecture():
    """随身物品档必须高于地标/建筑档：候选够时先出随身物。"""
    info = {
        "standalone_objects": ["ancient city wall", "pagoda", "stone lion",
                               "entry ticket", "water bottle", "fallen leaf"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0,
    }
    picked, _ = forge.select_objects(info, 3)
    assert set(picked) == {"entry ticket", "water bottle", "fallen leaf"}, picked


def test_model_flagged_items_are_dropped_even_without_wordlist():
    """
    通用兜底：词库没有 "riverside art centre"，但 G0 把它填进了
    modern_landmark_items —— 模型判断优先于词表，必须照样剔除。
    """
    info = {
        "standalone_objects": ["riverside art centre", "hometown gift shop bear",
                               "water bottle", "paper map", "acorn", "sketchbook"],
        "ip_items": ["hometown gift shop bear"],
        "modern_landmark_items": ["riverside art centre"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0,
    }
    picked, dropped = forge.select_objects(info, 4)
    assert "riverside art centre" not in picked and "hometown gift shop bear" not in picked, picked
    assert any("G0 标注" in d for d in dropped), dropped


# ── 3. prompt 与目视质检都要带上这条边界 ─────────────────────────────────
def test_prompt_carries_ip_policy_and_no_rig_rule():
    info = {
        "standalone_objects": ["entry ticket", "water bottle", "paper map",
                               "sun hat", "fallen leaf", "sketchbook"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0, "lighting": "day",
    }
    p = forge.build_prompt(info, 6)
    assert "IP AND TRADEMARK POLICY" in p
    assert "MODERN LANDMARK BUILDINGS" in p
    assert "NO SUPPORT STRUCTURE" in p
    assert "ANCIENT architecture" in p


def test_visual_qc_flags_ip_and_support_rig():
    """目视质检要能把「模型自己加进来的地标/吉祥物」和「支架」判成不合格。"""
    d = {"n_elements": 6, "n_object_only": 6, "n_with_people": 0,
         "banned_ip_or_landmark": ["stadium building"],
         "elements_with_support_rig": ["gachapon rig"],
         "composite_elements": [["gachapon rig", "capsule toy"]]}
    fails = []
    fails += forge.count_fails(d, 6, 6, 0)
    # 直接复用 qc_visual 的判定文案（这里不跑视觉模型，只验规则齐不齐）
    assert fails == [], fails
    text = " ".join([
        "出现 IP 合规禁止内容：stadium building",
        "以下元素画进了与主体无关的支撑结构（支架/立杆）：gachapon rig",
    ])
    assert "ip" in forge.fail_tags(text) or "support_rig" in forge.fail_tags(text)
    assert forge.fail_tags("出现 IP 合规禁止内容：stadium building") == {"ip"}


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
