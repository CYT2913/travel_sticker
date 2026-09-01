#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收敛保证回归测试（红队 R2）—— 锁死「有些订单永远交付不出来」这一类缺陷。

红队原话：CHANGELOG 里的 drum kit 案例，整套架子鼓同时触发「结构过细」和
「自带支架」两条互斥质检，4 轮全失败、不可能收敛。且 COMPOSITE_REPLACE(13 项)
与 SIMILAR_FAMILIES(17 族) 都是在生日/演出/银杏几张照片上长出来的，
遇到校园/宠物/盲盒/玩偶/乐高/手办等新品类必然漏判。

所以这里刻意【绕开所有词库】：用 "gachapon capsule rig"、"claw machine plush prize"
这类词库完全没有的新品类，验证产线仍然收敛。

不调用任何 AI 接口，不产生费用，可离线运行：
    python3 -m pytest tests/ -v
    python3 tests/test_convergence.py
"""
import os
import sys

sys.path.insert(0, os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge")))
import forge  # noqa: E402


# ── 0. 归因与标签 ─────────────────────────────────────────────────────────
def test_fail_tags_and_attribution():
    """失败文案要能被归类到规则，并归因到具体物体 —— 收敛判断的地基。"""
    thin = "以下结构仍过细，必须加粗（禁止删除物品本体）：gachapon rig thin poles"
    rig = ("以下元素画进了与主体无关的支撑结构（支架/立杆/托架/底座板/挂架）："
           "gachapon rig。模切剪不出这种结构")
    assert "too_thin" in forge.fail_tags(thin)
    assert "support_rig" in forge.fail_tags(rig)
    assert forge.objects_in_fail(thin, ["gachapon rig", "school badge"]) == ["gachapon rig"]
    # 全局项（色板/边框）不该归因到任何物体，否则会误剔无辜元素
    assert forge.fail_tags("焦点色占比约 22%，超过 15%") == set()


# ── 1. 一轮内撞上互斥规则 → 立刻换，不等第二轮 ──────────────────────────
def test_contradictory_rules_evict_immediately():
    """
    drum kit 案例的通用化版本：一枚元素同时被要求「加粗细杆」和「去掉支架」，
    这两条指令互斥，原地重画多少轮都过不了 —— 必须当轮剔除并换候选。
    """
    info = {
        # 词库里【一个都没有】的新品类
        "standalone_objects": ["gachapon capsule rig", "school badge", "blind box shelf",
                               "campus lanyard", "pet leash", "lego brick tower",
                               "enamel pin", "sketchbook"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0,
    }
    picked, _ = forge.select_objects(info, 6)
    guard = forge.ConvergenceGuard(info, 6)
    victim = picked[0]
    fails = [
        "以下结构仍过细，必须加粗（禁止删除物品本体）：%s" % victim,
        "以下元素画进了与主体无关的支撑结构（支架/立杆/托架/底座板/挂架）：%s" % victim,
    ]
    new_picked, logs, evicted = guard.after_round(picked, fails)
    assert evicted == [victim], "互斥规则应当轮剔除，实际 %s" % evicted
    assert forge._norm(victim) not in {forge._norm(p) for p in new_picked}
    assert len(new_picked) == 6, "换元素不能减少枚数：%s" % new_picked
    assert any("已替换为" in l for l in logs), logs
    assert any("互斥" in l for l in logs), logs


# ── 2. 连续 2 轮同一物体失败 → 永久剔除并换下一个候选 ────────────────────
def test_two_strikes_then_replaced():
    """同一物体连续 2 轮触发失败就换掉，日志要明确写「已替换为 Y」。"""
    info = {
        "standalone_objects": ["blind box figure stand", "campus lanyard", "school badge",
                               "pet leash", "enamel pin", "sketchbook", "canvas pouch"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0,
    }
    picked, _ = forge.select_objects(info, 5)
    guard = forge.ConvergenceGuard(info, 5)
    victim = picked[1]
    fail = "以下结构仍过细，必须加粗（禁止删除物品本体）：%s" % victim

    p1, logs1, ev1 = guard.after_round(picked, [fail])
    assert ev1 == [], "第 1 轮只记一次 strike，不该马上剔除：%s" % ev1
    p2, logs2, ev2 = guard.after_round(p1, [fail])
    assert ev2 == [victim], "连续第 2 轮必须剔除：%s" % ev2
    txt = " ".join(logs2)
    assert "连续 2 轮不收敛" in txt and "已替换为" in txt, txt
    assert len(p2) == 5


def test_strike_counter_resets_when_object_is_clean():
    """「连续」必须是真的连续：中间干净一轮就清零，不能秋后算账。"""
    info = {
        "standalone_objects": ["campus lanyard", "school badge", "pet leash",
                               "enamel pin", "sketchbook", "canvas pouch"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0,
    }
    picked, _ = forge.select_objects(info, 5)
    guard = forge.ConvergenceGuard(info, 5)
    v = picked[0]
    guard.after_round(picked, ["以下结构仍过细，必须加粗：%s" % v])
    # 这一轮该物体没问题，只有全局色板问题
    guard.after_round(picked, ["焦点色占比约 22%，超过 15%"])
    _p, _l, ev = guard.after_round(picked, ["以下结构仍过细，必须加粗：%s" % v])
    assert ev == [], "中间干净过一轮，不应算连续 2 轮：%s" % ev


# ── 3. 全局收敛保证：归因不到物体也不许空转 ──────────────────────────────
def test_global_guarantee_no_identical_group_across_rounds():
    """
    最关键的一条：只要候选池还有没试过的物体，就不允许「几轮跑完仍是同一组失败元素」。
    这里每轮只给一条【归因不到任何物体】的全局失败，产线也必须主动换元素。
    """
    info = {
        "standalone_objects": ["campus lanyard", "school badge", "pet leash",
                               "enamel pin", "sketchbook", "canvas pouch",
                               "paper cup", "acorn"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0,
    }
    picked, _ = forge.select_objects(info, 5)
    guard = forge.ConvergenceGuard(info, 5)
    seen = [tuple(sorted(forge._norm(p) for p in picked))]
    for _ in range(3):
        picked, _logs, _ev = guard.after_round(picked, ["剪纸边缘不是暖白而是纯白"])
        seen.append(tuple(sorted(forge._norm(p) for p in picked)))
    assert len(set(seen)) > 1, "4 轮下来元素组合一次都没变，等于空转：%s" % (seen,)
    assert len(picked) == 5


def test_pool_exhausted_is_reported_not_silently_shrinking():
    """候选池见底时不硬剔（枚数是硬约束），但必须在日志里说清楚。"""
    info = {
        "standalone_objects": ["campus lanyard", "school badge", "pet leash"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0,
    }
    picked, _ = forge.select_objects(info, 3)
    guard = forge.ConvergenceGuard(info, 3)
    v = picked[0]
    fails = ["以下结构仍过细，必须加粗：%s" % v,
             "以下元素画进了与主体无关的支撑结构（支架/立杆/托架/底座板/挂架）：%s" % v]
    new_picked, logs, ev = guard.after_round(picked, fails)
    assert ev == [] and len(new_picked) == 3, "池子见底不该减枚数：%s" % new_picked
    assert any("候选池已用尽" in l for l in logs), logs


# ── 4. 词库未覆盖的新品类，端到端仍能收敛 ────────────────────────────────
def test_new_category_converges_end_to_end():
    """
    综合场景：一张「盲盒展台 + 扭蛋机 + 校徽 + 乐高套装 + 毛绒玩偶」的照片。
    - 玩偶 / 盲盒属 IP 硬禁止，必须一枚都不出；
    - 扭蛋机、乐高套装是词库完全没有的复合体，靠通用规则 + 视觉模型拆解处理；
    - 4 轮之内必须收敛出 6 枚合法元素（不是 4 轮全败）。
    """
    info = {
        "standalone_objects": ["gachapon machine rig", "lego set", "plush toy",
                               "blind box figure", "school badge", "campus lanyard",
                               "water bottle", "paper map", "acorn", "sketchbook"],
        "keepsake_objects": ["school badge"],
        "container_pairs": [], "similar_pairs": [], "people_count": 0,
    }
    picked, dropped = forge.select_objects(info, 6)
    assert "plush toy" not in picked and "blind box figure" not in picked, \
        "玩偶/盲盒属 IP 硬禁止：%s" % picked
    assert any("IP 硬禁止" in d for d in dropped), dropped
    assert len(picked) == 6

    guard = forge.ConvergenceGuard(info, 6)
    rounds = 0
    for _ in range(4):
        rounds += 1
        # 每轮都对当前第一枚同时报「过细 + 支架」（最恶劣的互斥情形）
        bad = picked[0]
        fails = ["以下结构仍过细，必须加粗：%s" % bad,
                 "以下元素画进了与主体无关的支撑结构（支架/立杆）：%s" % bad]
        visual = {"composite_elements": [["gachapon machine rig", "capsule toy"]]}
        picked, logs, ev = guard.after_round(picked, fails, visual)
        if not ev and not logs:
            break
        assert len(picked) == 6, "第 %d 轮后枚数掉了：%s" % (rounds, picked)
    assert len(guard.banned) >= 2, "连撞 4 轮却没换过元素：%s" % guard.banned
    # 收敛的判据：最后一组元素里没有任何一枚是被判定不可收敛的
    assert not ({forge._norm(b) for b in guard.banned} &
                {forge._norm(p) for p in picked}), \
        "被剔除的物体又回到候选里：%s / %s" % (guard.banned, picked)


def test_model_composite_split_replaces_wordlist():
    """
    视觉模型说「这枚由多个部件组成，代表部件是 X」时，下一轮必须真的改画 X。
    这条就是「不再靠往 COMPOSITE_REPLACE 里加词」的兜底路径。
    """
    info = {
        "standalone_objects": ["claw machine prize tower", "campus lanyard",
                               "school badge", "sketchbook", "acorn", "paper cup"],
        "keepsake_objects": [], "container_pairs": [], "similar_pairs": [],
        "people_count": 0,
    }
    picked, _ = forge.select_objects(info, 6)
    assert "claw machine prize tower" in picked
    guard = forge.ConvergenceGuard(info, 6)
    visual = {"composite_elements": [["claw machine prize tower", "capsule toy"]]}
    new_picked, logs, _ev = guard.after_round(
        picked, ["以下元素是由多个部件组成的复合体，必须改画成括号里那一个单件："
                 "claw machine prize tower（改画 capsule toy）"], visual)
    assert "capsule toy" in new_picked, "模型给的单件没有生效：%s" % new_picked
    assert any("视觉模型判定，非词库" in l for l in logs), logs


def test_generic_composite_head_rule_without_wordlist():
    """通用中心词规则：词库没有 "lego set"，也要拆成单件。"""
    for whole, part in (("lego set", "lego"), ("model kit", "model"),
                        ("luggage cart", "luggage"), ("display mount", "display")):
        got, changed = forge._decompose(whole)
        assert changed and forge._norm(got) == part, "%s → %s（期望 %s）" % (whole, got, part)


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
