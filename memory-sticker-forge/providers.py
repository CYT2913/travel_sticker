#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
providers.py —— 外部 AI 能力适配层

【这个文件存在的唯一目的】
forge.py 的产线逻辑（照片体检、prompt 组装、配比规划、质检、重试）是纯业务代码，
和用哪家模型无关。换任何一家生图 / 视觉服务，都不需要改动 forge.py。
（本副本不含运行方的内部 provider 实现，只保留 cmd / volcengine / openai / gemini。）

把它们收敛到这一个文件后，迁移工作量 = 实现下面两个函数，其余代码一行不用改。

────────────────────────────────────────────────────────────────────────
需要实现的两个能力（接口契约）
────────────────────────────────────────────────────────────────────────
1) generate_image(photo_path, prompt, out_png) -> out_png
   图生图（image-to-image），不是文生图。
   必须把原照片作为输入，模型要保留照片里的可辨识主体，再做风格重绘。
   ★ 硬要求：输出短边 ≥ 2300px（A5 竖版 @400dpi = 2331×3307）。
      低于这个值不会报错，但会让成品糊掉 —— 详见文末「分辨率陷阱」。

2) analyze_images(paths, task) -> str
   多图视觉理解。task 里会要求模型只返回一个 JSON 对象。
   返回原始文本即可，上层负责抽 JSON。

────────────────────────────────────────────────────────────────────────
切换方式
────────────────────────────────────────────────────────────────────────
    export FORGE_PROVIDER=cmd         # 接你自己的 CLI（见下方 cmd 段）
    export FORGE_PROVIDER=volcengine  # 推荐的对外方案（Seedream 4.0 支持 4K）
    export FORGE_PROVIDER=openai      # ✅ gpt-image-2 起可用，见文末
    export FORGE_PROVIDER=gemini      # ⚠️ 分辨率不达标，见文末

────────────────────────────────────────────────────────────────────────
自检入口（R1：无法真调用时把风险降到最低）
────────────────────────────────────────────────────────────────────────
    python3 providers.py --capabilities   # 打印各 provider 像素能力 vs 400/300dpi 需求表
    python3 providers.py --selftest       # 不消耗生图额度的链路自检（key/网络/模型/请求体）
    python3 providers.py --selftest --offline   # 跳过联网检查（内网/代理环境）

forge.py 正式跑图前会自动跑一次 --selftest（fail-fast），不通过直接退出，
不会跑到一半才 404。用 --skip-selftest 可关掉。

────────────────────────────────────────────────────────────────────────
⚠️ 分辨率陷阱（迁移时最容易踩，且不会报错）
────────────────────────────────────────────────────────────────────────
尺寸【全部由公式推导】，不写魔数（见下面 px_at / min_short_edge_px）：

    短边像素 = 148mm ÷ 25.4 × dpi     →  400dpi: 2331px   300dpi: 1748px
    长边像素 = 210mm ÷ 25.4 × dpi     →  400dpi: 3307px   300dpi: 2480px

    火山 Seedream 4.0（总像素上限约 462 万）  → 只能 300dpi ⚠️
    OpenAI gpt-image-1 最大 1536×1024        → ❌ 差 1.5 倍以上
    OpenAI gpt-image-2 最长边 3840px          → ✅ 400dpi 可达（2336×3312）
    Gemini 2.5 Flash Image 约 1024~2048      → ❌ 不达标

达不到 400dpi 时【显式降级到 300dpi 并标注】，不静默插值成假 400dpi：
见 dpi_plan() / capability_table()。300dpi 对不干胶模切可接受，但必须让人知道。

为什么它不会报错（这一点很反直觉，务必看懂）：
relayout 是按「输入图代表 148mm 宽」来换算的，所以低分辨率图不会让贴纸变小，
而是被【插值放大】到 400dpi 的目标画布。结果是：
    · 成品物理尺寸正常
    · print_ready_doctor 按画布算，照样报 400 dpi ✅
    · 邻距、边距、刀线数全部合格 ✅
    · 但水粉纸纹和剪纸边缘已经糊掉了，印出来才发现
这是唯一一类「所有自动检查都通过、实物却不能用」的失败模式。

因此门禁必须卡在【输入端】：
    · providers.generate_image() 校验模型输出短边 ≥ MIN_SHORT_EDGE_PX（推导值，见下）
    · relayout.py --min-input-dpi 300 校验输入图有效 dpi
两道都不要关掉。分辨率不够时正确做法是换模型或显式降级，不是偷偷插值。
"""
import argparse
import base64
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

PROVIDER = os.environ.get("FORGE_PROVIDER", "openai").strip().lower()

# ══════════════════════════════════════════════════════════════════════════
# 瞬时错误重试（2026-09-02 · v36 实跑暴露）
# ══════════════════════════════════════════════════════════════════════════
# v36 一轮交付里 `400 Client Error` 打断了 6 次生图（01 白烧 2 次、06 连撞 3 次），
# 而重跑同一条命令往往就过了 —— 说明这类错误里有相当比例是【瞬时】的：网关抖动、
# 上游排队、审核服务超时。旧实现一次 400 就把异常抛到 forge.py，整单从 G0 重来。
#
# 所以在 provider 出口做有限次指数退避重试。三条边界必须守住：
#   ① 只重试【瞬时特征】的错误（见 TRANSIENT_MARKERS）。key 错、模型 ID 不存在、
#      内容被拒、分辨率不达标这些重试一万次也是同样结果，必须立刻失败，
#      否则等于把 3 倍时间和 3 倍额度烧在必然失败的请求上。
#   ② 次数有上限（默认 3 次），间隔 2/4/8 秒。生图是花钱的，不允许无限重试。
#   ③ 每次重试前先清掉可能存在的半成品文件，绝不把上一次的残留当本次产物。
GEN_MAX_ATTEMPTS = max(1, int(os.environ.get("FORGE_GEN_MAX_ATTEMPTS", "3")))
GEN_BACKOFF_SEC = (2, 4, 8)

# 判定「瞬时」的特征串（大小写不敏感，子串匹配）。
# 400 放进来是实测结论而不是理论推断：v36 六次 400 里重跑即过的占多数。
TRANSIENT_MARKERS = (
    "400 client error", "bad request",
    "bad gateway", "service unavailable", "gateway time-out", "gateway timeout",
    "timeout", "timed out", "connection reset", "connection aborted",
    "connection error", "remote end closed", "temporarily", "try again",
    "rate limit", "too many requests", "server error", "internal error",
    # _claim_generated 的「产物无法唯一确定」自己就写着「请重试本轮生成」
    "请重试本轮生成", "没有产出有效图片",
)
# HTTP 状态码单独用词边界匹配：写成裸子串会被 "1503px"、"2352x3520" 这类
# 尺寸数字误命中，那会把「分辨率不达标」这种必然失败的错误也拖去重试 3 次。
TRANSIENT_STATUS_RE = re.compile(r"(?<!\d)(408|409|425|429|500|502|503|504)(?!\d)")

# 明确【不该重试】的特征串。优先级高于 TRANSIENT_MARKERS：
# 很多厂商把「key 无效」「模型不存在」「内容被拒」也塞在 400 里返回，
# 那种 400 重试三次只是白等 14 秒。
PERMANENT_MARKERS = (
    "api key", "apikey", "unauthorized", "401", "403", "invalid_api_key",
    "authentication", "permission", "model not found", "invalid model",
    "does not exist", "content policy", "content_policy", "safety",
    "moderation", "被拒", "违规", "短边不足", "未知的 provider",
    "insufficient", "quota exceeded", "balance",
)


def _is_transient(msg):
    """这条错误值不值得重试。判不准时一律按【不重试】处理（宁可少烧额度）。"""
    m = (msg or "").lower()
    if any(p in m for p in PERMANENT_MARKERS):
        return False
    if any(t in m for t in TRANSIENT_MARKERS):
        return True
    return bool(TRANSIENT_STATUS_RE.search(m))


def _backoff_sec(attempt):
    """第 attempt 次失败后要等几秒（attempt 从 1 开始）。"""
    return GEN_BACKOFF_SEC[min(max(attempt, 1) - 1, len(GEN_BACKOFF_SEC) - 1)]

# ══════════════════════════════════════════════════════════════════════════
# 尺寸推导（R3：不写魔数）
# ══════════════════════════════════════════════════════════════════════════
# 这里所有像素值都是【算出来的】。以前写死 1748 有两个后果：
#   ① 改成品尺寸（比如出 A6 档）时必须手改常量，改漏就静默降质；
#   ② 门禁与目标 dpi 的关系藏在注释里，红队一眼看不出还剩多少余量。
#
#   短边像素 = 短边 mm ÷ 25.4 × dpi     长边像素 = 长边 mm ÷ 25.4 × dpi
#
# 容差 SHORT_EDGE_TOL：生图厂商的可选尺寸是【量化】的（gpt-image-2 要求宽高是
# 16 的倍数，火山只给固定档位），所以允许比理论值低 0.5%：
#       门禁 = floor(round(148/25.4×300) × (1 − 0.5%)) = floor(1748 × 0.995) = 1739px
# 0.5% 对应最低有效分辨率 298.5dpi —— 印刷与模切都感知不到，但把 gpt-image-2
# a5-300 档（短边 1760px）的余量从 12px 抬到 21px，任何一方微调默认尺寸不会立刻停摆。
# ⚠️ 容差只用来吸收「量化误差」，不是用来放行低分辨率模型：真低分辨率的模型
#    差的是 30% 以上，容差挡不住它们，dpi_plan() 会直接判定不达标。
MM_PER_INCH = 25.4
SHEET_W_MM = 148.0                # A5 竖版短边
SHEET_H_MM = 210.0                # A5 竖版长边
TARGET_DPI = 400                  # 目标分辨率
FALLBACK_DPI = 300                # 显式降级线：不干胶模切可接受的下限
SHORT_EDGE_TOL = 0.005            # 0.5% 量化容差，见上


def px_at(mm, dpi):
    """毫米 → 像素。四舍五入到最近整数，这样算出来的就是业界惯用值
    （A5 300dpi = 1748×2480、400dpi = 2331×3307），不会因为向上取整多出 1px
    让「和印厂对不上数」这种低级误会发生。"""
    return int(math.floor(float(mm) / MM_PER_INCH * float(dpi) + 0.5))


def sheet_px(dpi):
    """整版 A5 竖版在给定 dpi 下的 (宽, 高) 像素。"""
    return px_at(SHEET_W_MM, dpi), px_at(SHEET_H_MM, dpi)


def min_short_edge_px(dpi=FALLBACK_DPI, tol=SHORT_EDGE_TOL):
    """生图输出短边硬门禁 = 该 dpi 的理论短边 × (1 − 容差)。"""
    return int(math.floor(px_at(SHEET_W_MM, dpi) * (1.0 - tol)))


def effective_dpi(short_edge_px):
    """把短边像素换算回有效 dpi（报告与降级判定都用它，不猜）。"""
    return float(short_edge_px) * MM_PER_INCH / SHEET_W_MM


TARGET_W_PX, TARGET_H_PX = sheet_px(TARGET_DPI)        # 2331 × 3307
FALLBACK_W_PX, FALLBACK_H_PX = sheet_px(FALLBACK_DPI)  # 1748 × 2480
MIN_SHORT_EDGE_PX = min_short_edge_px()                # 1739px ≈ 298.5dpi，见上方公式

# 每个 provider 必需的环境变量。单一事实来源 —— tools/selftest_provider.py
# 直接读这里，不再各自维护一份（两份不一致过一次）。各 provider 实现段会
# 往这里补自己那一项（运行版与公开副本各自的那个 provider 只存在于对应副本里）。
REQUIRED_ENV = {
    "volcengine": ["ARK_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY"],
}

# provider 私有自检钩子：() -> [(level, title, howto)]。各 provider 实现段自行注册，
# selftest() 只管调用，因此公开副本少一个内部 provider 时这里天然为空，不会报错。
_PROVIDER_SELFTEST = {}

# ══════════════════════════════════════════════════════════════════════════
# 像素能力表（R3.3：启动期自检把余量摆出来）
# ══════════════════════════════════════════════════════════════════════════
# tested 字段就是「实测状态」，不要美化：False = 代码写完但一次真实调用都没跑过。
PROVIDER_CAPS = {
    "openai": {
        "label": "OpenAI gpt-image-2",
        "max_pixels": 8_290_000,
        "max_long_edge": 3840,
        "multiple_of": 16,
        "endpoint": "https://api.openai.com/v1",
        "models_api": True,
        "tested": False,
        "note": "宽高须为 16 的倍数；>2560px 官方标为实验性",
    },
    "volcengine": {
        "label": "火山方舟 Seedream 4.0",
        "max_pixels": 4_620_000,
        "max_long_edge": None,
        "multiple_of": None,
        "endpoint": "https://ark.cn-beijing.volces.com/api/v3",
        "models_api": False,
        "tested": False,
        "note": "总像素上限约 462 万 → 只能 300dpi；模型 ID 带日期后缀，可能过期",
    },
    "gemini": {
        "label": "Gemini 2.5 Flash Image",
        "max_pixels": 2048 * 2048,
        "max_long_edge": 2048,
        "multiple_of": None,
        "endpoint": "https://generativelanguage.googleapis.com",
        "models_api": True,
        "tested": False,
        "note": "分辨率不达标，仅适合做视觉理解",
    },
}


def _round_up_to(v, mult):
    if not mult:
        return int(v)
    return int(math.ceil(float(v) / mult) * mult)


def fits_dpi(caps, dpi):
    """
    这个 provider 能不能出该 dpi 的整版图。
    返回 dict：可行性 + 实际要请求的像素 + 余量（余量是给人看的，别删）。
    """
    w, h = sheet_px(dpi)
    mult = (caps or {}).get("multiple_of")
    w, h = _round_up_to(w, mult), _round_up_to(h, mult)
    px = w * h
    max_px = (caps or {}).get("max_pixels")
    max_long = (caps or {}).get("max_long_edge")
    reasons = []
    if max_px and px > max_px:
        reasons.append("总像素 %.2fM > 上限 %.2fM" % (px / 1e6, max_px / 1e6))
    if max_long and max(w, h) > max_long:
        reasons.append("最长边 %d > 上限 %d" % (max(w, h), max_long))
    return {
        "dpi": dpi, "w": w, "h": h, "pixels": px, "ok": not reasons,
        "why_not": "；".join(reasons),
        "short_edge_headroom_px": w - min_short_edge_px(),
        "pixel_headroom": (max_px - px) if max_px else None,
    }


def configured_output_px(provider):
    """
    这个 provider 【当前配置下实际会请求】的输出像素，未知返回 None。

    为什么要单独看一层「配置」而不是只看「能力」：gpt-image-2 的能力够 400dpi，
    但本项目默认档位是 a5-300 —— 如果只看能力就会报「本单 400dpi」，
    交付说明写 400dpi、实物却是 300dpi 插值上去的。那正是 R3 要堵的静默降级。
    """
    name = (provider or "").strip().lower()
    try:
        if name == "openai":
            v = _oai_size()
            if "x" in v:
                w, h = (int(x) for x in v.split("x"))
                return w, h
            return None
        if name == "volcengine":
            v = os.environ.get("ARK_IMAGE_SIZE", "").strip() or _ark_default_size()
            w, h = (int(x) for x in v.split("x"))
            return w, h
        if name == "cmd":
            return None                # 外部命令的输出尺寸无从得知，交给输出端门禁
    except Exception:
        return None
    return None


def _dpi_bucket(short_edge_px, requested_dpi=TARGET_DPI):
    """短边像素落在哪个交付档：requested / FALLBACK / 都不够(None)。"""
    for dpi in (requested_dpi, FALLBACK_DPI):
        if short_edge_px >= min_short_edge_px(dpi):
            return dpi
    return None


def dpi_plan(provider=None, requested_dpi=TARGET_DPI):
    """
    这一单实际能跑到多少 dpi。达不到 requested 就【显式降级】到 FALLBACK_DPI 并
    要求在日志和交付说明里标注，绝不静默插值假装 400dpi —— 那是唯一一类
    「所有自动检查全过、实物不能用」的失败。

    两层判定，任一层不够就降级：
      ① 能力层：provider 的总像素 / 最长边上限能不能装下该 dpi 的整版（fits_dpi）
      ② 配置层：当前环境变量实际会请求的尺寸够不够（configured_output_px）
    """
    name = (provider or _pick_name("image")).strip().lower()
    caps = PROVIDER_CAPS.get(name)
    cfg = configured_output_px(name)
    notes = []

    # ① 能力层
    if caps is None:
        cap_dpi = requested_dpi
        notes.append("provider %s 的像素能力未登记，能力层按请求值放行，"
                     "真实分辨率由输出端门禁校验" % name)
    else:
        top, low = fits_dpi(caps, requested_dpi), fits_dpi(caps, FALLBACK_DPI)
        if top["ok"]:
            cap_dpi = requested_dpi
            notes.append("%s 能力可达 %ddpi（%dx%d）"
                         % (caps["label"], requested_dpi, top["w"], top["h"]))
        elif low["ok"]:
            cap_dpi = FALLBACK_DPI
            notes.append("%s 能力做不到 %ddpi（%s），能力上限 %ddpi"
                         % (caps["label"], requested_dpi, top["why_not"], FALLBACK_DPI))
        else:
            return {"provider": name, "dpi": None, "w": None, "h": None,
                    "degraded": True, "known": True, "configured_px": cfg,
                    "reason": "%s 连 %ddpi 都做不到（%s）—— 不能用它出印刷稿，请换 provider"
                              % (caps["label"], FALLBACK_DPI, low["why_not"])}

    # ② 配置层
    cfg_dpi = cap_dpi
    if cfg:
        short = min(cfg)
        cfg_dpi = _dpi_bucket(short, requested_dpi)
        if cfg_dpi is None:
            return {"provider": name, "dpi": None, "w": cfg[0], "h": cfg[1],
                    "degraded": True, "known": caps is not None, "configured_px": cfg,
                    "reason": "当前配置的输出尺寸 %dx%d 有效分辨率仅 %.0fdpi，"
                              "连 %ddpi 底线都不到（门禁 %dpx）—— 请调大尺寸环境变量"
                              % (cfg[0], cfg[1], effective_dpi(short),
                                 FALLBACK_DPI, min_short_edge_px())}
        notes.append("当前配置输出 %dx%d（有效 %.0fdpi）"
                     % (cfg[0], cfg[1], effective_dpi(short)))

    dpi = min(cap_dpi, cfg_dpi)
    w, h = (cfg if cfg else sheet_px(dpi))
    degraded = dpi < requested_dpi
    if degraded:
        notes.append("→ 本单按 %ddpi 交付（已降级，**必须在日志与交付说明里标注本单为 %ddpi**）"
                     % (dpi, dpi))
        if caps is not None and fits_dpi(caps, requested_dpi)["ok"]:
            notes.append("提示：该 provider 能力其实够 %ddpi，是尺寸配置压低的 —— "
                         "openai 设 OPENAI_IMAGE_SIZE=a5-400 / 火山设 ARK_IMAGE_SIZE=%dx%d 即可"
                         % (requested_dpi, TARGET_W_PX, TARGET_H_PX))
    else:
        notes.append("→ 本单按 %ddpi 交付" % dpi)
    return {"provider": name, "dpi": dpi, "w": w, "h": h,
            "degraded": degraded, "known": caps is not None, "configured_px": cfg,
            "reason": "；".join(notes)}



def _disp_w(s):
    """终端显示宽度：CJK 字符占 2 列。表格不对齐会让人看漏一行。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, width):
    s = str(s)
    return s + " " * max(0, width - _disp_w(s))


def capability_table(requested_dpi=TARGET_DPI):
    """
    启动期打印的能力/余量表。一眼看到还剩多少余量，别再靠翻注释。

    「300档短边余量」这一列就是红队 R3 指出的那个数：gpt-image-2 的 a5-300 档
    短边 1760px 对门禁的余量。写死 1748 时它只有 12px，现在门禁按公式带 0.5%
    量化容差推导出 1739px，余量 21px，任何一方微调默认尺寸不会立刻停摆。
    """
    L = []
    L.append("A5 竖版 %g×%gmm ｜ 短边像素 = 短边mm ÷ 25.4 × dpi（四舍五入）"
             % (SHEET_W_MM, SHEET_H_MM))
    L.append("  需求：%ddpi → %d×%dpx（%.2fM）  ｜  %ddpi → %d×%dpx（%.2fM）"
             % (TARGET_DPI, TARGET_W_PX, TARGET_H_PX,
                TARGET_W_PX * TARGET_H_PX / 1e6,
                FALLBACK_DPI, FALLBACK_W_PX, FALLBACK_H_PX,
                FALLBACK_W_PX * FALLBACK_H_PX / 1e6))
    L.append("  输出端门禁：短边 ≥ %dpx（= %ddpi 理论值 %dpx × (1−%.1f%%) 量化容差，"
             "≈ %.1fdpi）"
             % (MIN_SHORT_EDGE_PX, FALLBACK_DPI, FALLBACK_W_PX,
                SHORT_EDGE_TOL * 100, effective_dpi(MIN_SHORT_EDGE_PX)))
    L.append("")
    cols = (30, 8, 8, 14, 14, 10)
    head = ("provider", "400dpi", "300dpi", "300档短边余量", "当前配置", "实测状态")
    L.append("  " + "".join(_pad(h, w) for h, w in zip(head, cols)))
    for name in sorted(set(_GEN)):
        caps = PROVIDER_CAPS.get(name)
        cfg = configured_output_px(name)
        cfg_s = ("%dx%d" % cfg) if cfg else "未知"
        if caps is None:
            row = (name, "?", "?", "—", cfg_s, "能力未登记")
        else:
            hi, lo = fits_dpi(caps, TARGET_DPI), fits_dpi(caps, FALLBACK_DPI)
            head_px = (("%+dpx" % lo["short_edge_headroom_px"]) if lo["ok"] else "—")
            row = (caps["label"], "✅" if hi["ok"] else "❌", "✅" if lo["ok"] else "❌",
                   head_px, cfg_s,
                   "✅ 已实测" if caps.get("tested") else "⚠️ 未实测")
        L.append("  " + "".join(_pad(v, w) for v, w in zip(row, cols)))
    plan = dpi_plan(requested_dpi=requested_dpi)
    L.append("")
    L.append("  当前生效：%s" % plan["reason"])
    return "\n".join(L)




class ProviderError(RuntimeError):
    pass


# ══════════════════════════════════════════════════════════════════════════
# Provider: cmd —— 接任意外部命令行工具（公开副本的默认「自带实现」出口）
#
#   如果你已经有一套自己的生图 / 视觉命令（本地模型、私有网关、任意 CLI），
#   不用写 Python，直接用环境变量把命令模板告诉这里即可。
#
#   环境变量：
#       FORGE_GEN_CMD     生图命令模板，支持占位符 {photo} {prompt} {out}
#                         要求执行后在 {out} 位置产出 PNG
#       FORGE_VISION_CMD  视觉命令模板，支持占位符 {paths} {task}
#                         要求把分析结果打到 stdout；{paths} 为空格分隔的多个路径
#
#   例：
#       export FORGE_PROVIDER=cmd
#       export FORGE_GEN_CMD='mytool img2img --src {photo} --prompt {prompt} --out {out}'
#       export FORGE_VISION_CMD='mytool vision --images {paths} --ask {task}'
#
#   ⚠️ 像素能力未知：外部命令能出多大图这里无从得知，所以 PROVIDER_CAPS 不登记
#      它的上限，dpi_plan() 会按请求 dpi 放行、把真实分辨率交给 generate_image()
#      的输出端门禁（短边 ≥ MIN_SHORT_EDGE_PX）去卡。这不是偷懒：唯一可靠的
#      判据就是它真正吐出来的那张图。
# ══════════════════════════════════════════════════════════════════════════

REQUIRED_ENV["cmd"] = []              # 不需要 key，但需要命令模板，见下面自检


def _cmd_selftest():
    """cmd provider 的自检：命令模板在不在、占位符写没写对。
    不执行命令本身（执行就可能真花钱/真出图），只做静态校验。"""
    out = []
    for env, need, what in (("FORGE_GEN_CMD", ("{photo}", "{prompt}", "{out}"), "生图"),
                            ("FORGE_VISION_CMD", ("{paths}", "{task}"), "视觉")):
        tpl = os.environ.get(env, "")
        if not tpl.strip():
            out.append(("fail", "缺环境变量 %s（%s命令模板）" % (env, what),
                        "export %s='你的命令 %s'" % (env, " ".join(need))))
            continue
        missing = [p for p in need if p not in tpl]
        if missing:
            out.append(("fail", "%s 缺占位符 %s" % (env, " ".join(missing)),
                        "模板里必须出现 %s，否则命令拿不到输入/输出路径"
                        % " ".join(need)))
        else:
            out.append(("ok", "%s 已设置且占位符完整" % env, ""))
    return out


_PROVIDER_SELFTEST["cmd"] = _cmd_selftest


def _cmd_generate(photo_path, prompt, out_png):
    tpl = os.environ.get("FORGE_GEN_CMD")
    if not tpl:
        raise ProviderError("FORGE_PROVIDER=cmd 需要设置 FORGE_GEN_CMD")
    cmd = tpl.format(photo=shlex.quote(os.path.abspath(photo_path)),
                     prompt=shlex.quote(prompt),
                     out=shlex.quote(os.path.abspath(out_png)))
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if not os.path.exists(out_png):
        raise ProviderError("外部生图命令未产出文件：\n%s\n%s"
                            % (cmd, (r.stdout + r.stderr)[-800:]))
    return out_png


def _cmd_analyze(paths, task):
    tpl = os.environ.get("FORGE_VISION_CMD")
    if not tpl:
        raise ProviderError("FORGE_PROVIDER=cmd 需要设置 FORGE_VISION_CMD")
    cmd = tpl.format(paths=" ".join(shlex.quote(os.path.abspath(p)) for p in paths),
                     task=shlex.quote(task))
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        raise ProviderError("外部视觉命令失败：\n%s" % (r.stdout + r.stderr)[-800:])
    return r.stdout.strip() or r.stderr.strip()


# ══════════════════════════════════════════════════════════════════════════
# Provider: volcengine —— 推荐的对外方案
#   生图：Seedream 4.0（doubao-seedream-4-0，支持 4K，图生图）
#   视觉：doubao-vision / doubao-1.5-vision-pro
#   两者都走火山方舟 OpenAI 兼容端点，所以只需要 openai SDK。
#
#   环境变量：
#       ARK_API_KEY            火山方舟 API Key
#       ARK_IMAGE_MODEL        默认 doubao-seedream-4-0-250828
#                              ★★ 这个默认值【随时可能过期】：火山的模型 ID 带日期
#                                 后缀（-250828 = 2026-08-28 的版本），厂商下线旧版本
#                                 后调用会直接报 model not found / InvalidParameter。
#                                 报这个错时不要改代码，先去火山方舟控制台
#                                 「开通管理 / 模型广场」抄当前可用的准确模型 ID，
#                                 再 export ARK_IMAGE_MODEL=…（selftest 会提示这句）
#       ARK_IMAGE_SIZE         默认按公式推导的 A5 300dpi 尺寸（见 _ark_default_size）
#                              注意 Seedream 有总像素上限（约 462 万），
#                              A5 400dpi ≈ 771 万必然超限 → 走火山只能 300dpi，
#                              dpi_plan() 会显式降级并要求交付说明标注
#       ARK_VISION_MODEL       默认 doubao-1.5-vision-pro-250328（同样带日期后缀）
#
#   ⚠️ 状态：接口形状按火山方舟公开文档写的，**没有 Key，一次真实调用都没跑过**。
#      跑正式单前必须先过 `python3 providers.py --selftest`（不花生图额度）。
# ══════════════════════════════════════════════════════════════════════════

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def _ark_default_size():
    """火山默认输出尺寸：由 dpi 公式推导，不写死。Seedream 只能到 300dpi。"""
    w, h = sheet_px(FALLBACK_DPI)
    return "%dx%d" % (w, h)


def _volc_body(prompt, photo_path=None):
    """
    组装火山生图请求体。抽成函数是为了让 --selftest 能在【不发请求】的前提下
    对同一个请求体做本地 schema 校验 —— 校验的必须是真正会发出去的那个 body，
    否则校验通过、真跑仍然 400，等于没校验。
    """
    body = {
        "model": os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-4-0-250828"),
        "prompt": prompt,
        # 不用 "4K" 这种档位词：它不保证宽高比，可能返回 16:9 或 1:1，
        # 导致后续 relayout 拿到的不是 A5 竖版比例。直接传明确像素。
        "size": os.environ.get("ARK_IMAGE_SIZE", "").strip() or _ark_default_size(),
        "response_format": "url",
        "extra_body": {"watermark": False},
    }
    if photo_path:
        body["image"] = _b64_data_url(photo_path)
    return body



def _b64_data_url(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "png")
    with open(path, "rb") as f:
        return "data:image/%s;base64,%s" % (mime, base64.b64encode(f.read()).decode())


def _ark_client():
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise ProviderError("缺少环境变量 ARK_API_KEY")
    try:
        from openai import OpenAI
    except ImportError:
        raise ProviderError("请先安装 SDK：pip install openai")
    return OpenAI(api_key=key, base_url=ARK_BASE_URL)


def _volc_generate(photo_path, prompt, out_png):
    import urllib.request
    client = _ark_client()
    body = _volc_body(prompt, photo_path)
    problems = validate_request_payload("volcengine", body)
    if problems:
        raise ProviderError("火山请求体本地校验不通过（还没发出去就拦下了）：\n  - "
                            + "\n  - ".join(problems))
    extra = body.pop("extra_body", None)
    try:
        # Seedream 图生图：把原照片作为 image 传入，size 用明确像素。
        resp = client.images.generate(extra_body=extra, **body)
    except Exception as e:
        msg = str(e)
        hint = ""
        if "model" in msg.lower() and ("not found" in msg.lower()
                                       or "404" in msg or "InvalidParameter" in msg):
            hint = ("\n提示：模型 ID 可能已过期（默认值带日期后缀 %s）。"
                    "去火山方舟控制台「开通管理」抄当前可用的模型 ID，"
                    "再 export ARK_IMAGE_MODEL=…" % body.get("model"))
        raise ProviderError("Seedream 调用失败：%s%s" % (msg, hint))
    url = resp.data[0].url
    with urllib.request.urlopen(url, timeout=180) as r, open(out_png, "wb") as f:
        shutil.copyfileobj(r, f)
    return out_png


def _volc_analyze(paths, task):
    client = _ark_client()
    model = os.environ.get("ARK_VISION_MODEL", "doubao-1.5-vision-pro-250328")
    content = [{"type": "image_url", "image_url": {"url": _b64_data_url(p)}} for p in paths]
    content.append({"type": "text", "text": task})
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        temperature=0.1,
    )
    return resp.choices[0].message.content or ""


# ══════════════════════════════════════════════════════════════════════════
# Provider: openai
#   视觉（gpt-4o / gpt-4.1）完全够用 ✅
#   ★ 2026-04 起 gpt-image-2 可直接用于生图（此前 gpt-image-1 分辨率不够，结论已作废）
#   gpt-image-2 规格：最长边 3840px，宽高须为 16 的倍数，比例 1:3~3:1，支持图生图/参考图。
#   本项目默认输出 1760×2480 = A5 300dpi（宽高均为 16 倍数，短边 1760 ≥ 门禁 1748）。
#   想上 400dpi 用 OPENAI_IMAGE_SIZE=2336x3312（= A5 400dpi），但 >2560px 官方标为实验性，
#   长稿建议先小批量试跑再批量出图。
#   计费：API 按 token 单独计费，ChatGPT/Codex 订阅额度不能抵扣 API 调用。
#   仍可混搭（视觉用 openai，生图用火山）：
#       FORGE_PROVIDER=openai  FORGE_IMAGE_PROVIDER=volcengine
# ══════════════════════════════════════════════════════════════════════════

def _openai_client():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderError("缺少环境变量 OPENAI_API_KEY")
    try:
        from openai import OpenAI
    except ImportError:
        raise ProviderError("请先安装 SDK：pip install openai")
    return OpenAI(api_key=key)


# gpt-image-2 的尺寸硬约束：宽高须为 16 的倍数，最长边 ≤3840，总像素 ≤8.29M，比例 1:3~3:1
# 档位【由 dpi 公式推导】，不写死像素：a5-300 = round(148/25.4×300)→1748 上取到 16 的倍数
# = 1760；a5-400 = round(148/25.4×400)→2331 上取到 2336。改成品尺寸只改 SHEET_*_MM。
def _oai_preset(dpi):
    f = fits_dpi(PROVIDER_CAPS["openai"], dpi)
    return "%dx%d" % (f["w"], f["h"])


_OAI_SIZE_PRESETS = {
    "a5-300": _oai_preset(FALLBACK_DPI),   # 1760x2480（A5 300dpi 上取 16 倍数）
    "a5-400": _oai_preset(TARGET_DPI),     # 2336x3312。>2560px 官方标为实验性，先小批量试
    "square": "2048x2048",
}


def _oai_size():
    v = os.environ.get("OPENAI_IMAGE_SIZE", "a5-300").strip().lower()
    v = _OAI_SIZE_PRESETS.get(v, v)
    if v in ("auto", "1024x1024"):
        return v
    try:
        w, h = (int(x) for x in v.split("x"))
    except Exception:
        raise ProviderError(
            "OPENAI_IMAGE_SIZE 格式不对：%r。用 a5-300 / a5-400 或 宽x高（如 %s）"
            % (v, _OAI_SIZE_PRESETS["a5-300"]))
    # 约束值全部取自 PROVIDER_CAPS，避免「表里写一套、校验写另一套」慢慢漂移
    caps = PROVIDER_CAPS["openai"]
    bad = []
    if caps["multiple_of"] and (w % caps["multiple_of"] or h % caps["multiple_of"]):
        bad.append("宽高必须是 %d 的倍数（当前 %dx%d）" % (caps["multiple_of"], w, h))
    if caps["max_long_edge"] and max(w, h) > caps["max_long_edge"]:
        bad.append("最长边不能超过 %d（当前 %d）" % (caps["max_long_edge"], max(w, h)))
    if caps["max_pixels"] and w * h > caps["max_pixels"]:
        bad.append("总像素不能超过 %.2fM（当前 %.2fM）"
                   % (caps["max_pixels"] / 1e6, w * h / 1e6))
    r = max(w, h) / float(min(w, h))
    if r > 3.0:
        bad.append("宽高比不能超过 3:1（当前 1:%.2f）" % r)
    if bad:
        raise ProviderError("gpt-image-2 尺寸不合法：\n  - " + "\n  - ".join(bad))
    return "%dx%d" % (w, h)


def _openai_kwargs(prompt):
    """
    组装 gpt-image-2 请求参数。抽成函数的目的同 _volc_body：--selftest 要校验
    【真正会发出去的那个请求体】，而不是另写一份近似的。
    """
    kw = dict(model=os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2"),
              prompt=prompt, size=_oai_size(), n=1)
    # quality: 贴纸线条要干净，默认拉满；省钱可设 medium/low
    q = os.environ.get("OPENAI_IMAGE_QUALITY", "high").strip().lower()
    if q and q != "auto":
        kw["quality"] = q
    # background=transparent 可直接出透明底，理论上能让后续元素分割更准。
    # 默认保持 opaque，因为我们的 relayout 是按浅色背景分割的，换透明底需要实测。
    bg = os.environ.get("OPENAI_IMAGE_BACKGROUND", "").strip().lower()
    if bg:
        kw["background"] = bg
    # input_fidelity=high 让模型更贴近输入照片（图生图保形关键参数）。
    # 走 extra_body 传，避免老版本 SDK 不认识这个字段直接报错。
    fid = os.environ.get("OPENAI_INPUT_FIDELITY", "high").strip().lower()
    if fid and fid != "off":
        kw["extra_body"] = {"input_fidelity": fid}
    return kw


def _openai_generate(photo_path, prompt, out_png):
    client = _openai_client()
    kw = _openai_kwargs(prompt)
    problems = validate_request_payload("openai", kw)
    if problems:
        raise ProviderError("gpt-image-2 请求体本地校验不通过（还没发出去就拦下了）：\n  - "
                            + "\n  - ".join(problems))

    try:
        with open(photo_path, "rb") as f:
            resp = client.images.edit(image=f, **kw)

    except Exception as e:
        msg = str(e)
        hint = ""
        if "input_fidelity" in msg or "Unknown parameter" in msg:
            hint = ("\n提示：你的账号/SDK 可能不支持某个新参数。"
                    "试 export OPENAI_INPUT_FIDELITY=off 再跑。")
        elif "size" in msg.lower():
            hint = ("\n提示：尺寸被拒。gpt-image-2 要求宽高为 16 的倍数、最长边≤3840。"
                    "试 export OPENAI_IMAGE_SIZE=a5-300。")
        elif "model" in msg.lower() and ("not found" in msg.lower() or "404" in msg):
            hint = ("\n提示：模型名不可用。确认账号已开通 gpt-image-2，"
                    "或 export OPENAI_IMAGE_MODEL=你控制台里的准确模型名。")
        elif "verif" in msg.lower() or "403" in msg:
            hint = "\n提示：gpt-image 系列通常要求组织完成身份验证（Verify Organization）。"
        raise ProviderError("gpt-image-2 调用失败：%s%s" % (msg, hint))

    data = resp.data[0]
    raw = base64.b64decode(data.b64_json) if getattr(data, "b64_json", None) else None
    if raw is None:
        import urllib.request
        with urllib.request.urlopen(data.url, timeout=180) as r:
            raw = r.read()
    with open(out_png, "wb") as f:
        f.write(raw)
    return out_png


def _openai_analyze(paths, task):
    client = _openai_client()
    model = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")
    content = [{"type": "image_url", "image_url": {"url": _b64_data_url(p)}} for p in paths]
    content.append({"type": "text", "text": task})
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": content}], temperature=0.1)
    return resp.choices[0].message.content or ""


# ══════════════════════════════════════════════════════════════════════════
# Provider: gemini
#   生图 gemini-2.5-flash-image 分辨率不达标 ❌，视觉可用 ✅
#   仅提供骨架，实现方式与上面同构（google-genai SDK）。
# ══════════════════════════════════════════════════════════════════════════

def _gemini_not_ready(*_a, **_k):
    raise ProviderError(
        "gemini provider 未实现。生图分辨率（约 1024~2048px）达不到 A5 印刷要求，"
        "不建议用于生图；如只想用它做视觉理解，请参考 _openai_analyze 的写法用 "
        "google-genai SDK 实现 analyze_images，并把生图交给 volcengine。")


# ══════════════════════════════════════════════════════════════════════════
# 分发
# ══════════════════════════════════════════════════════════════════════════

_GEN = {"cmd": _cmd_generate, "volcengine": _volc_generate,
        "openai": _openai_generate, "gemini": _gemini_not_ready}
_ANA = {"cmd": _cmd_analyze, "volcengine": _volc_analyze,
        "openai": _openai_analyze, "gemini": _gemini_not_ready}


def _pick(table, kind):
    # 允许生图和视觉分别用不同厂商，例如视觉用 openai、生图用 volcengine
    name = os.environ.get("FORGE_%s_PROVIDER" % kind.upper(), "").strip().lower() or PROVIDER
    if name not in table:
        raise ProviderError("未知的 provider：%s（可选 %s）" % (name, "/".join(table)))
    return table[name], name


def _pick_name(kind):
    """只要名字、不要函数，也不因为名字未知而抛错（dpi_plan / 自检要用）。"""
    return (os.environ.get("FORGE_%s_PROVIDER" % kind.upper(), "").strip().lower()
            or PROVIDER)


# ══════════════════════════════════════════════════════════════════════════
# 请求体本地 schema 校验（R1：不花额度也能验出「请求发出去必然 400」）
# ══════════════════════════════════════════════════════════════════════════
# 校验对象必须是 _openai_kwargs() / _volc_body() 真正会发出去的那个 dict，
# 不能另写一份近似的 —— 否则校验通过、真跑仍然 400，等于没校验。
_REQUEST_SCHEMA = {
    "openai": {"required": ("model", "prompt", "size", "n"),
               "size_caps": "openai"},
    "volcengine": {"required": ("model", "prompt", "size", "response_format"),
                   "size_caps": "volcengine"},
}


def validate_request_payload(provider, body):
    """返回问题清单（空 = 通过）。纯本地、可离线、可单测。"""
    schema = _REQUEST_SCHEMA.get((provider or "").strip().lower())
    if schema is None:
        return []
    problems = []
    for k in schema["required"]:
        if k not in body:
            problems.append("缺字段 %s" % k)
        elif body[k] in (None, "", []):
            problems.append("字段 %s 不能为空" % k)
    model = body.get("model")
    if model is not None and not (isinstance(model, str) and model.strip()):
        problems.append("model 必须是非空字符串（当前 %r）" % (model,))
    prompt = body.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        problems.append("prompt 必须是字符串（当前 %s）" % type(prompt).__name__)
    n = body.get("n")
    if n is not None and n != 1:
        problems.append("n 必须为 1（产线一轮只要一张，多张会让产物认领不唯一，当前 %r）" % (n,))
    size = body.get("size")
    if isinstance(size, str) and re.match(r"^\d+x\d+$", size):
        w, h = (int(x) for x in size.split("x"))
        caps = PROVIDER_CAPS.get(schema.get("size_caps")) or {}
        if caps.get("multiple_of") and (w % caps["multiple_of"] or h % caps["multiple_of"]):
            problems.append("size %s 的宽高必须是 %d 的倍数" % (size, caps["multiple_of"]))
        if caps.get("max_long_edge") and max(w, h) > caps["max_long_edge"]:
            problems.append("size %s 最长边超过上限 %d" % (size, caps["max_long_edge"]))
        if caps.get("max_pixels") and w * h > caps["max_pixels"]:
            problems.append("size %s 总像素 %.2fM 超过上限 %.2fM"
                            % (size, w * h / 1e6, caps["max_pixels"] / 1e6))
        if min(w, h) < min_short_edge_px():
            problems.append("size %s 短边 %d < 门禁 %dpx（有效 %.0fdpi，低于 %ddpi 底线）"
                            % (size, min(w, h), min_short_edge_px(),
                               effective_dpi(min(w, h)), FALLBACK_DPI))
        if h < w:
            problems.append("size %s 是横版，A5 贴纸整版必须竖版（宽 < 高）" % size)
    elif size is not None and size not in ("auto",):
        problems.append("size 必须是 '宽x高' 形如 %s（当前 %r）"
                        % (_OAI_SIZE_PRESETS["a5-300"], size))
    return problems


# ══════════════════════════════════════════════════════════════════════════
# 自检（R1）：在【不消耗生图额度】的前提下尽可能验证链路
# ══════════════════════════════════════════════════════════════════════════
# 能验的：key 存在且格式合理 → SDK 在不在 → base_url 可达 → models 接口能否
# 列出目标模型 ID（提供该接口的厂商才有）→ 请求体本地 schema 校验。
# 不能验的（老实说）：模型实际出图质量、真实计费、内容审核会不会拒。
# 这些只能等第一次真调用，所以 selftest 通过 ≠ 一定能出图，只代表「配置层面没坑」。

def _key_format_problem(env_name, value):
    """通用 key 格式体检。只查【一定错】的形态，不猜厂商前缀规则。"""
    if value != value.strip():
        return "%s 首尾有空白字符（复制粘贴常见错误），会导致 401" % env_name
    if len(value) < 16:
        return "%s 只有 %d 个字符，明显不是完整的 key" % (env_name, len(value))
    if value[0] in "'\"" or value[-1] in "'\"":
        return "%s 带着引号，export 时不要把引号写进值里" % env_name
    if any(c.isspace() for c in value):
        return "%s 中间有空白字符（可能是换行/折行），不是合法 key" % env_name
    if env_name == "OPENAI_API_KEY" and not value.startswith(("sk-", "sess-")):
        return "%s 不以 sk- 开头，确认抄的是 API key 而不是 org id / project id" % env_name
    return None


def _tcp_reachable(url, timeout=5.0):
    """只做 DNS + TCP 连接，不发请求 —— 一分钱不花，也不碰额度。"""
    m = re.match(r"^https?://([^/:]+)(?::(\d+))?", url or "")
    if not m:
        return False, "base_url 形如 %r，解析不出主机名" % url
    host = m.group(1)
    port = int(m.group(2) or (443 if url.startswith("https") else 80))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "%s:%d 可达" % (host, port)
    except Exception as e:
        return False, "%s:%d 连不上（%s）" % (host, port, e)


def _models_api_check(name):
    """能列模型就把目标模型 ID 对一遍 —— 这是最便宜的「模型是否过期」验证。"""
    if name == "openai":
        target = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
        try:
            client = _openai_client()
            ids = [m.id for m in client.models.list().data]
        except Exception as e:
            return ("fail", "models 接口调用失败：%s" % e,
                    "key 无效 / 无网络 / 组织未验证都会走到这里；"
                    "确认 key 可用后重试，或用 --offline 跳过联网检查")
        if target in ids:
            return ("ok", "models 接口列出了目标模型 %s" % target, "")
        near = [i for i in ids if "image" in i][:6]
        return ("fail", "models 接口里没有 %s" % target,
                "你的账号可用的图像模型：%s。去控制台确认后 "
                "export OPENAI_IMAGE_MODEL=准确名字" % (", ".join(near) or "（一个都没有）"))
    if name == "volcengine":
        return ("warn", "火山方舟不提供公开的 models 列举接口，无法预验模型 ID",
                "默认 ARK_IMAGE_MODEL=%s 带日期后缀，可能已下线。"
                "真跑若报 model not found / InvalidParameter，去控制台"
                "「开通管理」抄当前模型 ID 再 export ARK_IMAGE_MODEL=…"
                % os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-4-0-250828"))
    return ("warn", "provider %s 没有可用的模型列举接口，跳过该项" % name, "")


def selftest(provider=None, offline=False, requested_dpi=TARGET_DPI):
    """
    返回 (checks, exit_code)。
      checks: [{"level": ok/warn/fail, "title":…, "howto":…, "kind": config/link/dpi}]
      exit_code: 0 通过；1 配置问题（还没开始花钱，改环境变量就能解决）；
                 2 链路问题（网络不通 / key 无效 / 模型 ID 不存在）
    不发任何生图请求，不消耗生图额度。
    """
    checks = []

    def add(level, title, howto="", kind="config"):
        checks.append({"level": level, "title": title, "howto": howto, "kind": kind})

    names = []
    for k in ("image", "vision"):
        n = (provider or _pick_name(k)).strip().lower()
        if n not in names:
            names.append(n)

    for name in names:
        if name not in _GEN:
            add("fail", "未知 provider：%s" % name,
                "FORGE_PROVIDER 可选：%s" % " / ".join(sorted(_GEN)))
            continue
        caps = PROVIDER_CAPS.get(name, {})
        add("ok", "provider %s（%s）· 实测状态：%s"
            % (name, caps.get("label", "未登记像素能力"),
               "已实测" if caps.get("tested") else "⚠️ 从未实测过真实调用"))

        # 1) SDK 依赖
        if name in ("openai", "volcengine"):
            try:
                import openai  # noqa: F401
            except ImportError:
                add("fail", "%s 需要 openai SDK，当前没装" % name,
                    "pip install 'openai>=1.40'")

        # 2) key 是否存在 + 格式是否合理
        for k in REQUIRED_ENV.get(name, []):
            v = os.environ.get(k)
            if not v:
                add("fail", "缺环境变量 %s" % k,
                    "export %s='你的key'（openai 在 platform.openai.com/api-keys，"
                    "火山在方舟控制台 API Key 页面）" % k)
                continue
            bad = _key_format_problem(k, v)
            if bad:
                add("fail", bad, "重新复制一遍 key，不要带引号和换行")
            else:
                add("ok", "%s 已设置（%s…%s，长度 %d）" % (k, v[:6], v[-4:], len(v)))

        # 3) provider 私有自检（内部脚本存在性 / 外部命令模板等）
        for lvl, title, howto in (_PROVIDER_SELFTEST.get(name, lambda: [])() or []):
            add(lvl, title, howto)

        # 4) base_url 可达（只做 DNS+TCP，不发请求）
        ep = caps.get("endpoint")
        if ep and not offline:
            ok_, msg = _tcp_reachable(ep)
            add("ok" if ok_ else "fail", "base_url %s" % msg,
                "" if ok_ else "确认外网/代理可用；内网环境用 --offline 或 "
                               "FORGE_SELFTEST_OFFLINE=1 跳过联网检查",
                "link")
        elif ep:
            add("warn", "已跳过 base_url 连通性检查（--offline）", "", "link")

        # 5) models 接口核对目标模型 ID（要 key + 网络；离线或缺 key 就如实说没验）
        have_key = all(os.environ.get(k) for k in (REQUIRED_ENV.get(name) or []))
        if caps.get("models_api") and not offline and have_key:
            lvl, title, howto = _models_api_check(name)
            add(lvl, title, howto, "link")
        elif name in _REQUEST_SCHEMA:
            if caps.get("models_api"):
                add("warn", "已跳过 models 接口校验（离线或缺 key）—— 模型 ID 未经验证",
                    "拿到 key 且能联网后重跑一次 --selftest，把模型 ID 这一项验掉", "link")
            else:
                lvl, title, howto = _models_api_check(name)
                add(lvl, title, howto, "link")

        # 6) 请求体本地 schema 校验（不需要 key、不需要网络，永远能跑）
        if name in _REQUEST_SCHEMA:
            try:
                body = (_openai_kwargs("selftest prompt") if name == "openai"
                        else _volc_body("selftest prompt"))
                problems = validate_request_payload(name, body)
            except ProviderError as e:
                body, problems = {}, [str(e)]
            if problems:
                add("fail", "请求体本地校验不通过：%s" % "；".join(problems),
                    "多半是尺寸环境变量设错了：openai 用 OPENAI_IMAGE_SIZE=a5-300/a5-400，"
                    "火山用 ARK_IMAGE_SIZE=宽x高")
            else:
                add("ok", "请求体本地校验通过（%s）"
                    % json.dumps({k: v for k, v in body.items()
                                  if k in ("model", "size", "n")}, ensure_ascii=False))

        # 7) dpi 计划：达不到 400dpi 就在这里显式说明降级，不留到跑完才发现
        plan = dpi_plan(name, requested_dpi)
        if plan["dpi"] is None:
            add("fail", "分辨率不达标：%s" % plan["reason"],
                "换 provider，或调大尺寸环境变量", "dpi")
        elif plan["degraded"]:
            add("warn", "分辨率降级：%s" % plan["reason"],
                "可接受，但交付说明必须标注本单 dpi", "dpi")
        else:
            add("ok", "分辨率：%s" % plan["reason"], "", "dpi")

    fails = [c for c in checks if c["level"] == "fail"]
    if not fails:
        code = 0
    elif any(c["kind"] == "link" for c in fails):
        code = 2
    else:
        code = 1
    return checks, code


def print_selftest(provider=None, offline=None, requested_dpi=TARGET_DPI,
                   stream=None):
    """打印自检结果，返回 exit code。forge.py 的 fail-fast 直接用它。"""
    out = stream or sys.stdout
    if offline is None:
        offline = os.environ.get("FORGE_SELFTEST_OFFLINE", "").strip() not in ("", "0")
    checks, code = selftest(provider, offline, requested_dpi)
    icon = {"ok": "  [OK]   ", "warn": "  [警告] ", "fail": "  [FAIL] "}
    for c in checks:
        print(icon[c["level"]] + c["title"], file=out)
        if c["howto"]:
            print("          → 怎么办：" + c["howto"], file=out)
    print(("✅ provider 自检通过（未消耗任何生图额度）" if code == 0
           else "❌ provider 自检不通过（退出码 %d：%s）—— 按上面的「怎么办」处理后再跑图"
                % (code, "配置问题" if code == 1 else "链路问题")), file=out)
    return code


def _purge_stale(out_png):
    """生图前先清掉可能存在的同名旧文件。

    同一个 outdir 被重跑时，上一次的 round1.png 会留在那儿；本次生图失败的话，
    后面的 os.path.exists 检查就会把【旧文件】当成本次产物放行并交付 ——
    和「认领别人的图」是同一类缺陷。重试循环里每一次尝试前都要做。
    """
    if os.path.exists(out_png):
        try:
            os.remove(out_png)
        except OSError as e:
            raise ProviderError("无法清除旧产物 %s（%s），拒绝在可能交付旧图的情况下继续"
                                % (out_png, e))


def generate_image(photo_path, prompt, out_png):
    """图生图。返回 out_png 路径。会校验输出分辨率并在不足时明确报错。

    瞬时错误（`400 Client Error` / 5xx / 超时 / 产物认领不唯一）走有限次
    指数退避重试，见文件头「瞬时错误重试」。永久性错误立刻抛出，不浪费额度。
    """
    fn, name = _pick(_GEN, "image")
    for attempt in range(1, GEN_MAX_ATTEMPTS + 1):
        _purge_stale(out_png)
        try:
            fn(photo_path, prompt, out_png)
            if not os.path.exists(out_png) or os.path.getsize(out_png) == 0:
                raise ProviderError("provider %s 没有产出有效图片" % name)
            break
        except Exception as e:
            transient = _is_transient(str(e))
            if attempt >= GEN_MAX_ATTEMPTS or not transient:
                if transient:
                    raise ProviderError(
                        "provider %s 连续 %d 次生图失败（已按 %s 秒指数退避重试），"
                        "判定为瞬时错误但一直没恢复，最后一次：%s"
                        % (name, GEN_MAX_ATTEMPTS,
                           "/".join(str(s) for s in GEN_BACKOFF_SEC[:GEN_MAX_ATTEMPTS]), e))
                raise
            wait = _backoff_sec(attempt)
            print("⚠️ provider %s 第 %d/%d 次生图失败，判定为【瞬时错误】，%d 秒后重试：%s"
                  % (name, attempt, GEN_MAX_ATTEMPTS, wait, str(e)[:400]),
                  file=sys.stderr)
            time.sleep(wait)

    try:
        from PIL import Image
        w, h = Image.open(out_png).size
    except Exception as e:
        # 不能静默放过：这道分辨率门禁挡的是「所有自动检查都过、实物却糊掉」
        # 的唯一一类失败（详见文件头「分辨率陷阱」）。读不到尺寸时至少要吼一声，
        # 否则门禁形同关闭而没人知道。
        print("⚠️ 无法读取 %s 的尺寸（%s），本次跳过分辨率门禁 —— "
              "请手动确认短边 ≥ %dpx" % (out_png, e, MIN_SHORT_EDGE_PX),
              file=sys.stderr)
        return out_png

    if min(w, h) < MIN_SHORT_EDGE_PX:
        raise ProviderError(
            "provider %s 输出 %dx%d，短边不足 %dpx（A5 @300dpi 底线）。\n"
            "后果不是画质差一点，而是重排时元素不被放大 → 每枚贴纸物理尺寸过小。\n"
            "请改用支持 4K 输出的生图模型（如 volcengine 的 Seedream 4.0），"
            "或设置 FORGE_IMAGE_PROVIDER=volcengine 只把生图换掉。"
            % (name, w, h, MIN_SHORT_EDGE_PX))
    return out_png


def analyze_images(paths, task):
    """多图视觉理解，返回模型原始文本。"""
    fn, _ = _pick(_ANA, "vision")
    return fn(paths, task)


def describe():
    """当前生效的 provider，供 forge.py 打印，避免"以为在用A其实在用B"。"""
    _, g = _pick(_GEN, "image")
    _, v = _pick(_ANA, "vision")
    return "生图=%s 视觉=%s" % (g, v)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="providers.py —— 生图/视觉适配层。自检与能力表都在这里，"
                    "两个入口都不消耗生图额度。")
    ap.add_argument("--selftest", action="store_true",
                    help="链路自检：key 是否存在且格式合理 / base_url 可达 / "
                         "models 接口能否列出目标模型 / 请求体本地 schema 校验")
    ap.add_argument("--capabilities", action="store_true",
                    help="打印各 provider 像素能力 vs 400/300dpi 需求与余量表")
    ap.add_argument("--offline", action="store_true",
                    help="跳过联网检查（内网/代理环境）。等价 FORGE_SELFTEST_OFFLINE=1")
    ap.add_argument("--dpi", type=int, default=TARGET_DPI, help="目标分辨率，默认 400")
    a = ap.parse_args(argv)

    print("FORGE_PROVIDER =", PROVIDER)
    try:
        print("当前生效：", describe())
    except ProviderError as e:
        print("⚠️ %s" % e)
    print()
    if a.capabilities or not a.selftest:
        print(capability_table(a.dpi))
        print()
    if not a.selftest:
        print("链路自检（不花钱）：python3 providers.py --selftest [--offline]")
        return 0
    return print_selftest(offline=True if a.offline else None, requested_dpi=a.dpi)


if __name__ == "__main__":
    sys.exit(main())

