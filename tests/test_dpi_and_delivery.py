#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交付说明 / dpi 标注 / fail-fast 接线 回归测试（红队 R3 + R4）。

R3：达不到 400dpi 必须【显式降级并标注】，不许静默假装 400dpi；跑图前必须先自检。
R4：色差没有实测过，代码侧能做的就是把色彩管理说明写进模切店须知，
    并且如实写「我们没做过色差实测」，不在代码里假装解决了色差。

这些是「接线」测试：光有 providers.dpi_plan() 不够，主流程必须真的用上它。
不调用任何 AI 接口，可离线运行。
"""
import os
import sys

FORGE_DIR = os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge"))
sys.path.insert(0, FORGE_DIR)


def _src(name):
    p = os.path.join(FORGE_DIR, name)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()


def _need(name):
    """拿不到文件就【显式 skip】，不静默 pass —— 否则公开副本里这两条会假绿。"""
    src = _src(name)
    if src is None:
        try:
            import pytest
            pytest.skip("%s 不在本副本内（交付打包脚本只在运行版）" % name)
        except ImportError:
            print("     (skip: %s 不在本副本内)" % name)
    return src


# ── 1. forge.py 真的把自检和 dpi 计划接起来了 ────────────────────────────
def test_forge_runs_selftest_before_spending_money():
    src = _src("forge.py")
    assert src, "找不到 forge.py"
    assert "providers.print_selftest" in src, "跑图前没有 fail-fast 自检"
    assert "--skip-selftest" in src, "没有留跳过自检的开关"
    assert "capability_table" in src, "启动期没有打印能力/余量表"
    # 自检必须在 G0 之前（不合格就退出，一分钱不花）
    assert src.index("providers.print_selftest") < src.index("【G0】"), \
        "自检位置在 G0 之后，等于跑到一半才发现配置错"


def test_forge_uses_planned_dpi_not_requested_dpi():
    src = _src("forge.py")
    assert "providers.dpi_plan" in src, "主流程没有做分辨率能力探测"
    assert "do_relayout(png, a.outdir, str(rnd), a.gap, a.margin, dpi)" in src, \
        "重排仍在用请求 dpi，而不是探测后的实际 dpi —— 会写出假 400dpi"
    assert "已降级" in src and "标注" in src, "降级时没有要求标注"


def test_report_records_delivered_dpi():
    import forge
    import inspect
    sig = inspect.signature(forge.write_report)
    assert "dpi" in sig.parameters and "guard" in sig.parameters, sig


# ── 2. 模切店须知：dpi 不写死 + 色彩管理说明齐全 ─────────────────────────
def test_notice_dpi_is_templated():
    src = _need("make_delivery_v35.py")
    if src is None:
        return
    i = src.index("NOTICE = ")
    j = src.index('"""', src.index('"""', i) + 3)
    notice = src[i:j]
    assert "{dpi} dpi" in notice, "须知里的分辨率仍是写死的"
    assert "400 dpi（" not in notice, "须知里还留着写死的 400 dpi"
    assert "{dpi_note}" in notice, "降级时没有额外说明位"


def test_notice_has_color_management_section():
    src = _need("make_delivery_v35.py")
    if src is None:
        return
    for key in ("色彩管理", "sRGB", "打样", "深色", "高饱和",
                "相对比色", "暖白", "没有做过印刷色差实测"):
        assert key in src, "模切店须知缺少色彩管理要点：%s" % key
    # 不许在代码里假装解决了色差
    assert "色差已校准" not in src and "色彩已校准" not in src


# ── 3. 交付打包不再假设卡纸图的文件名里写着 400dpi ───────────────────────
def _load_delivery():
    if _need("make_delivery_v35.py") is None:
        return None
    import importlib
    return importlib.import_module("make_delivery_v35")


def test_card_png_is_found_by_pattern_not_hardcoded_dpi(tmp_path=None):
    mod = _load_delivery()
    if mod is None:
        return
    import tempfile
    pdir = tempfile.mkdtemp()
    card = os.path.join(pdir, "card")
    os.makedirs(card)
    # 故意用一个【不是 400dpi】的文件名：卡纸渲染 dpi 改了也要能找到
    for n in ("卡纸打印图_210x148mm_360dpi.png",
              "卡纸打印图_210x148mm_360dpi_含出血3mm.png",
              "卡纸打印图_210x148mm_360dpi.pdf"):
        open(os.path.join(card, n), "wb").close()
    assert os.path.basename(mod._card_png(pdir)) == "卡纸打印图_210x148mm_360dpi.png"
    assert os.path.basename(mod._card_png(pdir, bleed=True)).endswith("含出血3mm.png")


def test_card_png_refuses_to_guess_when_multiple_versions():
    """同目录混了两种 dpi 时必须报错，不能挑一个 —— 挑错就是把低分辨率当成品交付。"""
    mod = _load_delivery()
    if mod is None:
        return
    import tempfile
    pdir = tempfile.mkdtemp()
    card = os.path.join(pdir, "card")
    os.makedirs(card)
    for n in ("卡纸打印图_210x148mm_400dpi.png", "卡纸打印图_210x148mm_300dpi.png"):
        open(os.path.join(card, n), "wb").close()
    try:
        mod._card_png(pdir)
    except SystemExit as e:
        assert "恰好 1 个" in str(e)
    else:
        raise AssertionError("混了两个 dpi 版本却没有报错")


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
