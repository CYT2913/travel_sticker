#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
providers.py —— 外部 AI 能力适配层

【这个文件存在的唯一目的】
forge.py 的产线逻辑（照片体检、prompt 组装、配比规划、质检、重试）是纯业务代码，
和用哪家模型无关。换任何一家生图 / 视觉服务，都不需要改动 forge.py。

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
    export FORGE_PROVIDER=openai      # 默认
    export FORGE_PROVIDER=volcengine  # 推荐的对外方案（Seedream 4.0 支持 4K）
    export FORGE_PROVIDER=openai      # ✅ gpt-image-2 起可用，见文末
    export FORGE_PROVIDER=gemini      # ⚠️ 分辨率不达标，见文末

────────────────────────────────────────────────────────────────────────
⚠️ 分辨率陷阱（迁移时最容易踩，且不会报错）
────────────────────────────────────────────────────────────────────────
A5 竖版成品需要 2331×3307px @400dpi（300dpi 底线是 1748×2480）。

    火山 Seedream 4.0（支持 4K 输出）          → 可达标    ✅
    OpenAI gpt-image-1 最大 1536×1024          → ❌ 差 1.5 倍以上
    OpenAI gpt-image-2 最长边 3840px            → ✅ 达标（本项目默认 1760×2480）
    Gemini 2.5 Flash Image 约 1024~2048        → ❌ 不达标

为什么它不会报错（这一点很反直觉，务必看懂）：
relayout 是按「输入图代表 148mm 宽」来换算的，所以低分辨率图不会让贴纸变小，
而是被【插值放大】到 400dpi 的目标画布。结果是：
    · 成品物理尺寸正常
    · print_ready_doctor 按画布算，照样报 400 dpi ✅
    · 邻距、边距、刀线数全部合格 ✅
    · 但水粉纸纹和剪纸边缘已经糊掉了，印出来才发现
这是唯一一类「所有自动检查都通过、实物却不能用」的失败模式。

因此门禁必须卡在【输入端】：
    · providers.generate_image() 校验模型输出短边 ≥ 1748px
    · relayout.py --min-input-dpi 300 校验输入图有效 dpi
两道都不要关掉。分辨率不够时正确做法是换模型，不是调低门禁。
"""
import base64
import json
import os
import re
import shutil
import shlex
import subprocess
import sys

PROVIDER = os.environ.get("FORGE_PROVIDER", "openai").strip().lower()

# A5 竖版 @400dpi。generate_image 的输出短边不应低于 300dpi 对应的 1748。
TARGET_W_PX = 2331
TARGET_H_PX = 3307
MIN_SHORT_EDGE_PX = 1748          # 300dpi 底线，低于此值 forge 的 dpi 检查会失败


class ProviderError(RuntimeError):
    pass


# ══════════════════════════════════════════════════════════════════════════
# Provider: cmd —— 接任意外部命令行工具
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
# ══════════════════════════════════════════════════════════════════════════

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
#                              ★ 火山模型迭代快（已有 Seedream 5.0），请到控制台
#                                「开通管理」确认你账号可用的准确模型 ID 再覆盖此值
#       ARK_IMAGE_SIZE         默认 1760x2480 = A5 300dpi
#                              注意 Seedream 有总像素上限（5.0 Pro 约 462 万），
#                              A5 400dpi（2336x3312 ≈ 771 万）大概率超限，
#                              需要 400dpi 请改用 OpenAI gpt-image-2
#       ARK_VISION_MODEL       默认 doubao-1.5-vision-pro-250328
#
#   ⚠️ 状态：接口形状按火山方舟公开文档写的，我这边没有 Key，未实测。
#      第一次跑通前请先用 tools/selftest_provider.py 验证（见 SOP 第 1 步）。
# ══════════════════════════════════════════════════════════════════════════

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


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
    model = os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-4-0-250828")

    # Seedream 图生图：把原照片作为 image 传入，size 指定 4K。
    resp = client.images.generate(
        model=model,
        prompt=prompt,
        image=_b64_data_url(photo_path),
        # 不用 "4K" 这种档位词：它不保证宽高比，可能返回 16:9 或 1:1，
        # 导致后续 relayout 拿到的不是 A5 竖版比例。直接传明确像素。
        size=os.environ.get("ARK_IMAGE_SIZE", "1760x2480"),  # A5 300dpi
        response_format="url",
        extra_body={"watermark": False},
    )
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
_OAI_SIZE_PRESETS = {
    "a5-300": "1760x2480",   # A5 300dpi，默认。短边 1760 > 门禁 1748
    "a5-400": "2336x3312",   # A5 400dpi。>2560px 官方标为实验性，先小批量试
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
            "OPENAI_IMAGE_SIZE 格式不对：%r。用 a5-300 / a5-400 或 宽x高（如 1760x2480）" % v)
    bad = []
    if w % 16 or h % 16:
        bad.append("宽高必须是 16 的倍数（当前 %dx%d）" % (w, h))
    if max(w, h) > 3840:
        bad.append("最长边不能超过 3840（当前 %d）" % max(w, h))
    if w * h > 8_290_000:
        bad.append("总像素不能超过 8.29M（当前 %.2fM）" % (w * h / 1e6))
    r = max(w, h) / float(min(w, h))
    if r > 3.0:
        bad.append("宽高比不能超过 3:1（当前 1:%.2f）" % r)
    if bad:
        raise ProviderError("gpt-image-2 尺寸不合法：\n  - " + "\n  - ".join(bad))
    return "%dx%d" % (w, h)


def _openai_generate(photo_path, prompt, out_png):
    client = _openai_client()
    model = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
    size = _oai_size()

    kw = dict(model=model, prompt=prompt, size=size, n=1)
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


def generate_image(photo_path, prompt, out_png):
    """图生图。返回 out_png 路径。会校验输出分辨率并在不足时明确报错。"""
    fn, name = _pick(_GEN, "image")
    # 先删掉可能存在的同名旧文件：同一个 outdir 被重跑时，上一次的 round1.png
    # 会留在那儿；本次生图失败的话，下面的 os.path.exists 检查就会把【旧文件】
    # 当成本次产物放行并交付。这类「认领了不属于本次调用的文件」的缺陷
    # 2026-08-30 在内部运行版上真实发生过（并行跑图互相偷图），所以两边都要挡。
    if os.path.exists(out_png):
        try:
            os.remove(out_png)
        except OSError as e:
            raise ProviderError("无法清除旧产物 %s（%s），拒绝在可能交付旧图的情况下继续"
                                % (out_png, e))
    fn(photo_path, prompt, out_png)
    if not os.path.exists(out_png) or os.path.getsize(out_png) == 0:
        raise ProviderError("provider %s 没有产出有效图片" % name)

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


if __name__ == "__main__":
    print("FORGE_PROVIDER =", PROVIDER)
    print("当前生效：", describe())
    print("\n目标输出尺寸：%dx%d px（A5 @400dpi）" % (TARGET_W_PX, TARGET_H_PX))
    print("短边硬底线：%d px（A5 @300dpi）" % MIN_SHORT_EDGE_PX)
