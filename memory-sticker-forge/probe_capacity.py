#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_capacity.py —— 一张照片到底能榨出多少个「不同图案」？

这是回答「一张照片能不能填满 A3 的 24 枚」的前置探测。
它不生成图，只让视觉模型对照片做一次深度盘点，并把候选图案按【来源可信度】分级：

  tier A  真实独立物品      —— 照片里确实存在、能整体抠出来的实体物品
  tier B  真实局部特写      —— 照片里存在，但只能取局部（花纹、把手、屋檐一角）
  tier C  同物变体          —— 同一物品的另一种状态/角度，照片里没有，但合理可推
  tier D  通用装饰件        —— 与这张照片无关的手账通用素材（色块/星点/飘带/边框）

A + B 是「这张照片真正的信息量」，C 会语义重复，D 严格来说不是「你的照片」。
把这四层分开报数，才能诚实回答 24 枚里有多少是真的。

用法：
  python3 probe_capacity.py ../IMG_8222.jpeg --target 24
"""
import os, sys, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from forge import call_vision, grab_json, shrink

TASK = """你是定制贴纸产品的元素规划师。客户只给了这一张照片，希望尽可能多地拆出【互不重复】的贴纸图案，目标 %(target)d 枚。

请对这张照片做一次彻底盘点，把所有候选图案按来源可信度分成四层。只输出一个 JSON 对象，不要解释文字。

{
  "scene": "一句话场景描述",
  "tier_a_real_objects": ["..."],      // 照片里【确实存在】、轮廓完整、能整体抠出来做贴纸的实体物品。用英文，简短具体。有多少列多少，不要凑数，不要重复
  "tier_b_real_details": ["..."],      // 照片里确实存在，但只能取【局部/细节】的图案，如盘子花纹、杯身纹样、屋檐一角、栏杆一段。用英文
  "tier_c_variants": ["..."],          // 同一物品的【另一种状态或角度】，照片里看不到但合理可推，如"蛋糕切开一半"、"蜡烛熄灭"。用英文，并在括号里注明来源物品
  "tier_d_generic": ["..."],           // 与这张照片无关的手账通用装饰件，如色块、星点、飘带、边框、小箭头。用英文
  "honest_max_distinct": 整数,          // 你认为这张照片能撑起的【真正互不重复】的贴纸枚数（只算 A+B，不含 C/D）
  "why": "一句话说明为什么是这个数字，信息量瓶颈在哪"
}

判定纪律：
- tier_a 只放你在图里真的看得见的完整物品。看不清、被遮挡大半、纯背景墙面都不算。
- 【禁止列纯纹理和光效】不要出现 texture、bokeh、blur、light spots、gradient、shadow 这类东西。
  每一项都必须是【有明确轮廓、撕下来贴到本子上能一眼认出是什么】的具体物件或图案。
  反例（不许出现）："red linen texture"、"warm bokeh dots"、"wall surface"、"soft shadow"。
- 背景严重虚焦、只能猜出大概形状的东西不要放进 tier_a，可放 tier_b 但要确认轮廓仍可辨。
- 【同一物理对象只能出现一次】不要把一个东西的整体和它的部件分开列成两项（如 "plate" 和
  "plate rim pattern" 只能留一个）。也不要把【同处一个容器里的东西】拆成多项（如照片里蛋糕、
  冰淇淋、蜡烛都在同一个盘子上，则最多只留其中最有辨识度的一项），否则做出来会是几枚几乎
  相同的贴纸。宁可总数少，也不要语义重复。
- 不要把同一个物品用不同说法重复列（如 "cake" 和 "birthday cake" 只能算一个）。
- 不要为了凑够 %(target)d 个而把 tier_c / tier_d 塞进 tier_a。诚实比凑数重要。
- honest_max_distinct 要保守。如果这张照片只够 7 枚，就写 7。"""


def probe(photo, target, workdir):
    os.makedirs(workdir, exist_ok=True)
    small = shrink(photo, os.path.join(workdir, "probe_small.jpg"))
    txt = call_vision([small], TASK % {"target": target})
    d = grab_json(txt)
    if not d:
        raise SystemExit("❌ 盘点失败，未返回可解析 JSON：\n" + txt[:600])
    return d


def report(name, d, target):
    a = d.get("tier_a_real_objects") or []
    b = d.get("tier_b_real_details") or []
    c = d.get("tier_c_variants") or []
    g = d.get("tier_d_generic") or []
    real = len(a) + len(b)
    L = []
    L.append("=" * 66)
    L.append("照片容量盘点 · %s" % name)
    L.append("=" * 66)
    L.append("场景 : %s" % d.get("scene", ""))
    L.append("")
    L.append("A 真实独立物品 (%d) : %s" % (len(a), ", ".join(a) or "无"))
    L.append("B 真实局部特写 (%d) : %s" % (len(b), ", ".join(b) or "无"))
    L.append("C 同物变体     (%d) : %s" % (len(c), ", ".join(c) or "无"))
    L.append("D 通用装饰件   (%d) : %s" % (len(g), ", ".join(g) or "无"))
    L.append("")
    L.append("照片真实信息量 A+B = %d 枚" % real)
    L.append("模型自评可撑起的互不重复枚数 = %s 枚" % d.get("honest_max_distinct"))
    L.append("瓶颈 : %s" % d.get("why", ""))
    L.append("")
    gap = target - real
    if gap <= 0:
        L.append("→ 目标 %d 枚：照片信息量【足够】，可全部来自照片本身。" % target)
    else:
        L.append("→ 目标 %d 枚：照片只能真实供给 %d 枚，缺 %d 枚。" % (target, real, gap))
        L.append("  这 %d 枚只能来自 C（同物变体，会语义重复）或 D（通用装饰件，与照片无关）。" % gap)
        L.append("  必须向客户说明，否则等于拿通用素材充当「你的照片专属」。")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("photos", nargs="+")
    ap.add_argument("--target", type=int, default=24)
    ap.add_argument("--workdir", default="probe_out")
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)
    for p in a.photos:
        name = os.path.basename(p)
        d = probe(p, a.target, a.workdir)
        txt = report(name, d, a.target)
        print(txt + "\n")
        base = os.path.splitext(name)[0]
        with open(os.path.join(a.workdir, base + "_probe.json"), "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        with open(os.path.join(a.workdir, base + "_probe.txt"), "w") as f:
            f.write(txt + "\n")
