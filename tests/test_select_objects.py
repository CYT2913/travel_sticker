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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "memory-sticker-forge"))
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
