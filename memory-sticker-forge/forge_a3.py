#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forge_a3.py —— 一张照片 → 一整张 A3（最多 24 枚互不重复图案）

为什么不能「单次生成 24 枚」：
  1. 分辨率不够。单次 4k 出图约 2352×3520px，铺满 A3 只有 201 dpi，低于印刷底线 300。
     分 4 批各出 4k、每批铺一张 A5、再拼 A3，则是 404 dpi。
  2. 元素数量准确性。实测 6 枚都常需 2 轮才对得上，一次要求 24 枚几乎不可能数对。
  3. 单元素像素量。24 枚挤在一张生成图里，每枚的有效像素只有 6 枚时的 1/4，细节直接崩。

所以本脚本的做法是：深度盘点照片 → 把物品分配到 4 批（互不重复）→ 每批独立走完整产线
（生成 → 程序化重排 → 量化+目视双检 → 不合格定向重试）→ 4 张 A5 拼成 A3。

尺寸上不用担心：A3 排 24 枚（4列×6行）单格 63.25×60mm，比现在 A5 排 6 枚的 60×58mm 还大 9%。

诚实纪律：
  只用照片里【真实存在】的图案（盘点的 A 层完整物品 + B 层局部特写）。
  真实容量不够 24 时，默认把总数降到实际能撑起的数量，而不是拿通用素材凑满，
  除非显式加 --fill-generic。

用法：
  python3 forge_a3.py ../IMG_8222.jpeg --outdir a3_run
  python3 forge_a3.py ../IMG_8222.jpeg --total 24 --fill-generic
  python3 forge_a3.py ../IMG_8222.jpeg --plan-only          # 只盘点分批，不生成
"""
import os, sys, json, math, argparse, subprocess, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from probe_capacity import probe, report as probe_report
from forge import preflight   # 复用 G0 拿人数/光线

FORGE = os.path.join(HERE, "forge.py")
IMPOSE = os.path.join(os.path.dirname(HERE), "print-ready-doctor", "impose_a3.py")
BATCHES = 4                   # A3 = 4 × A5，固定 4 批
GRID_OK = {4: "2列×2行", 5: "2列×3行(留1空)", 6: "2列×3行"}


def log(m):
    print(m, flush=True)


def plan(photo, total, fill_generic, workdir):
    """盘点照片 → 决定实际总枚数 → 把图案分配到 4 批（互不重复）。"""
    d = probe(photo, total, workdir)
    a = d.get("tier_a_real_objects") or []
    b = d.get("tier_b_real_details") or []
    c = d.get("tier_c_variants") or []
    g = d.get("tier_d_generic") or []

    real = a + b                      # A 层优先，B 层补足 —— 都是照片里真实存在的
    pool, note = list(real), []

    if len(pool) < total:
        if fill_generic:
            add = (c + g)[: total - len(pool)]
            pool += add
            note.append("照片真实容量仅 %d 枚，已按 --fill-generic 用 %d 枚同物变体/通用装饰件补足到 %d 枚。"
                        "这部分不是照片独有内容，必须向客户说明。" % (len(real), len(add), len(pool)))
        else:
            note.append("照片真实容量仅 %d 枚，不足 %d 枚。已按诚实原则下调总数，"
                        "未用通用素材凑满（需要凑满请加 --fill-generic）。" % (len(real), total))

    # 每批 4~6 枚（受 A5 版面与 GRID 支持限制）。批数按池子大小自适应，
    # 用 round-robin 均分 —— 19 枚会分成 5/5/5/4，而不是截断成 16 枚白扔 3 个图案。
    n_use = min(len(pool), 6 * BATCHES)
    nb = min(BATCHES, n_use // 4)
    if nb == 0:
        raise SystemExit("❌ 这张照片只盘点出 %d 个可用图案，连一张 4 枚的 A5 都撑不起来。"
                         "建议换一张信息更丰富的照片。" % len(pool))
    n_use = min(n_use, nb * 6)
    pool = pool[:n_use]

    batches = [[] for _ in range(nb)]
    for i, item in enumerate(pool):
        batches[i % nb].append(item)

    return d, batches, n_use, note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("photo")
    ap.add_argument("--outdir", default="a3_run")
    ap.add_argument("--total", type=int, default=24, help="目标总枚数（A3 上限 24）")
    ap.add_argument("--fill-generic", action="store_true",
                    help="真实容量不足时，允许用同物变体/通用装饰件补满（会告知客户）")
    ap.add_argument("--plan-only", action="store_true", help="只盘点并分批，不生成")
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--figure-style", default="collage")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    work = os.path.join(a.outdir, "_plan"); os.makedirs(work, exist_ok=True)

    log("═" * 70)
    log("forge_a3  ·  %s  ·  目标一整张 A3 / %d 枚互不重复" % (os.path.basename(a.photo), a.total))
    log("═" * 70)

    log("\n【P0】照片容量深度盘点")
    d, batches, used, note = plan(a.photo, a.total, a.fill_generic, work)
    log(probe_report(os.path.basename(a.photo), d, a.total))
    for n in note:
        log("  ⚠️  %s" % n)

    info, _ = preflight(a.photo, work)
    has_ppl = (info.get("people_count") or 0) > 0

    log("\n【P1】分批方案：%d 批，共 %d 枚（A3 = 4 × A5，每批独立出一张 A5）"
        % (len(batches), used))
    for i, bt in enumerate(batches, 1):
        ppl_tag = "含1枚人物 " if (has_ppl and i == 1) else ""
        log("  批 %d（%d 枚 · %s%s）: %s"
            % (i, len(bt), ppl_tag, GRID_OK.get(len(bt), "?"), ", ".join(bt)))
    json.dump({"batches": batches, "total": used,
               "has_people": has_ppl, "probe": d, "note": note},
              open(os.path.join(a.outdir, "plan.json"), "w"), ensure_ascii=False, indent=2)

    if a.plan_only:
        log("\n（--plan-only，未生成）")
        return 0

    # ── 逐批走完整产线 ────────────────────────────────────────────────
    finals = []
    for i, bt in enumerate(batches, 1):
        bdir = os.path.join(a.outdir, "batch%d" % i)
        # 只让第 1 批出人物：人物贴纸姿态单一，4 批都出会严重重复
        with_ppl = has_ppl and i == 1
        per = len(bt)
        objs = bt[: per - 1] if with_ppl else bt[:per]
        cmd = [sys.executable, FORGE, os.path.abspath(a.photo),
               "--outdir", bdir, "--elements", str(per),
               "--max-rounds", str(a.max_rounds),
               "--figure-style", a.figure_style,
               "--objects", "; ".join(objs)]
        if not with_ppl:
            cmd += ["--allow-people", "no"]
        # 关键：把其它批次的图案作为排他清单传进去。否则每批各自独立生成时，
        # 模型会把「同处一盘」的物品反复整盘画出来（实测出现过 3 枚几乎相同的甜点盘）。
        others = [x for j, ob in enumerate(batches) if j != i - 1 for x in ob]
        if others:
            cmd += ["--exclude", "; ".join(others)]
        log("\n" + "─" * 70)
        log("【批 %d/%d】%d 枚%s" % (i, len(batches), per, "（含人物）" if with_ppl else "（全物品）"))
        log("─" * 70)
        r = subprocess.run(cmd, cwd=HERE)
        fp = os.path.join(bdir, "FINAL.png")
        if r.returncode != 0 or not os.path.isfile(fp):
            log("\n❌ 批 %d 未产出合格稿，A3 拼版中止。已完成 %d 批。" % (i, len(finals)))
            return 1
        finals.append(fp)

    # ── 4 张 A5 拼 A3 ─────────────────────────────────────────────────
    log("\n" + "─" * 70)
    log("【拼版】4 张 A5 → 一整张 A3")
    log("─" * 70)
    aout = os.path.join(a.outdir, "A3")
    subprocess.run([sys.executable, IMPOSE] + [os.path.abspath(f) for f in finals]
                   + ["--outdir", os.path.abspath(aout)],
                   cwd=os.path.dirname(IMPOSE))
    log("\n🎉 一整张 A3 完成：%s" % aout)
    log("   共 %d 枚图案，全部来自同一张照片" % used)
    log("   A3_print.png → 只要图片的厂家 | A3_production.svg → 要 SVG 的厂家")
    return 0


if __name__ == "__main__":
    sys.exit(main())
