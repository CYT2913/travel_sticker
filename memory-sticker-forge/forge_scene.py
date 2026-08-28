#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forge_scene.py —— 生成「卡纸打印图」左半边的主视觉场景图

────────────────────────────────────────────────────────────────────────
这个脚本为什么单独存在
────────────────────────────────────────────────────────────────────────
一单产品其实有两件东西要印：

    A5 贴纸版（forge.py）      → 不干胶 + 模切，撕下来贴手账
    卡纸打印图（本脚本 + make_memory_card.py） → 厚卡纸，整张收藏

贴纸版是「六枚孤立元素躺在纯白底上」，卡纸图是「一整幅场景 + 旁边几枚贴纸样」。
两者的出图要求正好相反：贴纸版要求彼此绝不相连、要留大白边；场景图要求满幅、
要有背景。塞进同一个 prompt 只会互相打架，所以拆成两次生成。

关键点是 **色板必须同源**：本脚本直接复用 forge.py 里的 STYLE / PALETTE /
FIGURE / COMPLIANCE 段，只替换输出规格段（模块 O1）。这样场景图和贴纸拼到
同一张卡纸上时，笔触和颜色是一套，不会一半暖棕一半灰绿。

────────────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────────────
    # 复用已有单子的 G0 结果（推荐，省一次视觉调用，也保证两边理解一致）
    python3 forge_scene.py 照片.jpg --preflight run/p5/preflight.json --outdir run/p5

    # 没有 G0 结果时自己跑一次
    python3 forge_scene.py 照片.jpg --outdir run/p5

产出：<outdir>/scene.png + <outdir>/scene_prompt.txt
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import forge          # noqa: E402  复用 G0、prompt 模块、体检门禁
import providers      # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="生成卡纸打印图用的主视觉场景图")
    ap.add_argument("photo", help="客户原照片")
    ap.add_argument("--outdir", default="scene_out")
    ap.add_argument("--preflight", help="复用已有的 preflight.json（推荐）")
    ap.add_argument("--figure", choices=["collage", "silhouette"], default="collage")
    ap.add_argument("--aspect", default="4:5",
                    help="场景图长宽比，默认 4:5（竖版，贴合卡纸左栏）")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    work = os.path.join(args.outdir, "_work")
    os.makedirs(work, exist_ok=True)

    if args.preflight and os.path.exists(args.preflight):
        info = json.load(open(args.preflight))
        small = forge.shrink(args.photo, os.path.join(work, "src_small.jpg"))
        forge.log("· 复用已有 G0：%s" % args.preflight)
    else:
        forge.log("· 跑 G0 照片体检 …")
        info, small = forge.preflight(args.photo, work)

    # 场景图同样受分辨率门禁约束：卡纸是实体印刷品，糊了就是废品
    ok, blocks, warns = forge.g0_gate(info, 6)
    for w in warns:
        forge.log("  ⚠️ %s" % w)
    if not ok:
        raise SystemExit("❌ G0 未通过：\n" + "\n".join("  · " + b for b in blocks))

    prompt = forge.build_scene_prompt(info, figure_style=args.figure)
    open(os.path.join(args.outdir, "scene_prompt.txt"), "w").write(prompt)

    # 场景图是满幅构图，不走 2:3 贴纸版比例
    os.environ["FORGE_ASPECT"] = args.aspect
    dst = os.path.join(args.outdir, "scene.png")
    forge.log("· 生成主视觉场景图（%s）…" % args.aspect)
    try:
        providers.generate_image(small, prompt, dst)
    except providers.ProviderError as e:
        raise SystemExit("❌ %s" % e)

    from PIL import Image
    w, h = Image.open(dst).size
    forge.log("✅ 场景图：%s  %dx%d" % (dst, w, h))
    forge.log("   下一步：python3 ../print-ready-doctor/make_memory_card.py "
              "--scene %s --stickers <FINAL.png> --outdir <交付目录>" % dst)


if __name__ == "__main__":
    main()
