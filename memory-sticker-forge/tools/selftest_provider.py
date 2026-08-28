#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
provider 自检：换环境 / 换厂商 / 刚充完钱之后，先跑这个，再跑 forge.py。

目的是把"配置错了"和"模型不行"分开，避免你花了钱却以为是代码坏了。

用法（从便宜到花钱，建议按顺序来）：

  # 1) 干检查，一分钱不花：只看 provider 解析、SDK、环境变量齐不齐
  python3 tools/selftest_provider.py

  # 2) 实测视觉理解，花几分钱
  python3 tools/selftest_provider.py --vision 某张照片.jpg

  # 3) 实测生图，花一张图的钱，并校验分辨率和宽高比是否能印
  python3 tools/selftest_provider.py --image 某张照片.jpg

  # 4) 全测
  python3 tools/selftest_provider.py --all 某张照片.jpg

退出码：
  0 = 通过
  1 = 配置缺失（环境变量 / SDK），还没开始花钱
  2 = 调用失败（key 无效 / 模型 ID 不对 / 网络不通）
  3 = 调用成功但输出不达标（分辨率或宽高比不能印）—— 需要换模型或调尺寸
"""
import argparse
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# A5 竖版宽高比 = 148:210，允许 3% 误差
A5_RATIO = 210.0 / 148.0
RATIO_TOL = 0.03

# 每个 provider 必需的环境变量
REQUIRED_ENV = {
    "volcengine": ["ARK_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "cmd": ["FORGE_GEN_CMD", "FORGE_VISION_CMD"],
    "gemini": ["GEMINI_API_KEY"],
}


def c(s, color):
    if not sys.stdout.isatty():
        return s
    return {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m",
            "b": "\033[1m"}.get(color, "") + s + "\033[0m"


def ok(m):
    print(c("  [OK]   ", "g") + m)


def bad(m):
    print(c("  [FAIL] ", "r") + m)


def warn(m):
    print(c("  [警告] ", "y") + m)


def step(m):
    print("\n" + c("── " + m, "b"))


def check_config():
    """不花钱的干检查。返回 (生图provider, 视觉provider)。"""
    step("1. provider 解析")
    try:
        import providers
    except Exception as e:
        bad("providers.py 导入失败：%s" % e)
        sys.exit(1)

    try:
        desc = providers.describe()
    except Exception as e:
        bad("provider 配置有问题：%s" % e)
        print("     FORGE_PROVIDER 可选：openai / volcengine / cmd")
        sys.exit(1)

    print("     当前生效： " + c(desc, "b"))
    gen = os.environ.get("FORGE_IMAGE_PROVIDER", "").strip().lower() \
        or providers.PROVIDER
    vis = os.environ.get("FORGE_VISION_PROVIDER", "").strip().lower() \
        or providers.PROVIDER
    ok("解析正常（生图=%s，视觉=%s）" % (gen, vis))

    step("2. SDK 依赖")
    if gen == "cmd" and vis == "cmd":
        ok("cmd provider 走外部命令，不需要 openai SDK")
    else:
        try:
            import openai  # noqa: F401
            ok("openai SDK 已安装（火山方舟也走它的兼容端点）")
        except ImportError:
            bad("缺 openai SDK → 执行：pip install 'openai>=1.40'")
            sys.exit(1)
    try:
        from PIL import Image  # noqa: F401
        ok("Pillow 已安装")
    except ImportError:
        bad("缺 Pillow → 执行：pip install Pillow")
        sys.exit(1)

    step("3. 环境变量")
    missing = []
    for who, kind in ((gen, "生图"), (vis, "视觉")):
        for k in REQUIRED_ENV.get(who, []):
            if os.environ.get(k):
                v = os.environ[k]
                ok("%s(%s) %s = %s…%s（长度 %d）"
                   % (kind, who, k, v[:6], v[-4:], len(v)))
            else:
                bad("%s(%s) 缺 %s" % (kind, who, k))
                missing.append(k)
    if missing:
        print("\n     设置方法（当前终端生效）：")
        for k in dict.fromkeys(missing):
            print("       export %s='你的key'" % k)
        sys.exit(1)

    step("4. 关键参数")
    if gen == "volcengine":
        print("     ARK_IMAGE_MODEL = %s"
              % os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-4-0-250828（默认）"))
        print("     ARK_IMAGE_SIZE  = %s"
              % os.environ.get("ARK_IMAGE_SIZE", "1760x2480（默认，A5 300dpi）"))
        warn("火山模型迭代快，若报模型不存在，去控制台「开通管理」抄准确的模型 ID，"
             "再 export ARK_IMAGE_MODEL=…")
        warn("Seedream 有总像素上限，A5 400dpi(2336x3312) 大概率超限；"
             "要 400dpi 请用 openai 的 gpt-image-2")
    elif gen == "openai":
        print("     OPENAI_IMAGE_MODEL = %s"
              % os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2（默认）"))
        print("     OPENAI_IMAGE_SIZE  = %s"
              % os.environ.get("OPENAI_IMAGE_SIZE", "1760x2480（默认，A5 300dpi）"))
        warn("订阅（Plus/Pro/Codex）额度不能抵扣 API 调用，需在 platform 单独充 credits")
    ok("干检查全部通过，还没有产生任何费用")
    return gen, vis


def test_vision(photo):
    step("5. 实测视觉理解（会产生少量费用）")
    import providers
    try:
        out = providers.analyze_images(
            [photo], "用一句话说明这张图里有什么。只回一句中文。")
    except Exception as e:
        bad("视觉调用失败：%s" % e)
        print("     常见原因：key 无效 / 模型 ID 不对 / 网络或地区限制")
        sys.exit(2)
    if not (out or "").strip():
        bad("调用成功但返回空文本")
        sys.exit(2)
    ok("视觉通路正常，模型回答：" + out.strip()[:80])


def test_image(photo):
    step("6. 实测生图（会产生一张图的费用）")
    import providers
    from PIL import Image
    out_png = os.path.join(tempfile.mkdtemp(prefix="selftest_"), "gen.png")
    prompt = ("Gouache paper-collage sticker sheet on matte off-white paper. "
              "6 separate cut-out sticker elements with soft white borders, "
              "evenly spaced, plain background, no text, no logo, no watermark.")
    try:
        providers.generate_image(photo, prompt, out_png)
    except Exception as e:
        msg = str(e)
        if "短边不足" in msg:
            bad("生图成功但分辨率不达标：\n     " + msg.replace("\n", "\n     "))
            print("\n     怎么修：调大尺寸环境变量（ARK_IMAGE_SIZE / OPENAI_IMAGE_SIZE），"
                  "或换支持高分辨率的模型")
            sys.exit(3)
        bad("生图调用失败：%s" % msg)
        sys.exit(2)

    w, h = Image.open(out_png).size
    ok("已出图 %dx%d → %s" % (w, h, out_png))

    if min(w, h) < providers.MIN_SHORT_EDGE_PX:
        bad("短边 %d < 门禁 %d，不能印" % (min(w, h), providers.MIN_SHORT_EDGE_PX))
        sys.exit(3)
    ok("分辨率过关（短边 %d ≥ %d）" % (min(w, h), providers.MIN_SHORT_EDGE_PX))

    ratio = max(w, h) / float(min(w, h))
    if abs(ratio - A5_RATIO) / A5_RATIO > RATIO_TOL:
        bad("宽高比 1:%.3f 偏离 A5 竖版 1:%.3f 超过 %d%%"
            % (ratio, A5_RATIO, int(RATIO_TOL * 100)))
        print("     后果：relayout 重排时会裁掉或留出多余白边。")
        print("     怎么修：把尺寸环境变量设成 A5 比例，例如 1760x2480；"
              "不要用 '4K'、'2K'、'auto' 这类档位词，它们不保证比例。")
        sys.exit(3)
    ok("宽高比 1:%.3f，符合 A5 竖版" % ratio)
    if h < w:
        warn("出的是横版，A5 贴纸版应为竖版；检查尺寸参数的宽高顺序")


def main():
    ap = argparse.ArgumentParser(
        description="provider 自检：先干检查，再按需实测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="建议顺序：先不带参数跑（免费），通过后再 --vision，最后 --image")
    ap.add_argument("--vision", metavar="照片", help="实测视觉理解")
    ap.add_argument("--image", metavar="照片", help="实测生图并校验能否印")
    ap.add_argument("--all", metavar="照片", help="视觉 + 生图都测")
    a = ap.parse_args()

    print(c("provider 自检", "b"))
    check_config()

    vis_img = a.all or a.vision
    gen_img = a.all or a.image
    for p in (vis_img, gen_img):
        if p and not os.path.isfile(p):
            bad("找不到文件：%s" % p)
            sys.exit(1)

    if vis_img:
        test_vision(vis_img)
    if gen_img:
        test_image(gen_img)

    print()
    if not (vis_img or gen_img):
        print(c("配置检查通过。", "g")
              + " 下一步用真实照片实测（会花钱）：")
        print("  python3 tools/selftest_provider.py --all 你的照片.jpg")
    else:
        print(c("自检通过，可以跑正式产线了：", "g"))
        print("  python3 forge.py 你的照片.jpg --outdir run_01")
    sys.exit(0)


if __name__ == "__main__":
    main()
