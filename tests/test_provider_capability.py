#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
provider 能力 / 分辨率推导 / 自检 回归测试（红队 R1 + R3）。

R3 原话：Seedream 像素上限约 462 万 < A5@400dpi 需要的 771 万，只能 300dpi；
gpt-image-2 的 a5-300 档短边 1760px，硬门禁是 1748px，余量 12 像素，
任何一方调默认尺寸就停摆。

所以这里锁三件事：
  1. 所有印刷像素都是【公式推导】的，没有魔数，改 dpi/纸张不需要手改常量；
  2. 达不到 400dpi 时【显式降级】到 300dpi 并标注，绝不静默假装 400dpi；
  3. 自检不消耗生图额度，缺 key / 尺寸配错时给出可操作报错和正确的退出码。

不调用任何生图接口、不联网（一律 offline=True），可离线运行：
    python3 -m pytest tests/ -v
    python3 tests/test_provider_capability.py
"""
import math
import os
import sys

sys.path.insert(0, os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge")))
import providers  # noqa: E402


class _env(object):
    """临时改环境变量，退出即还原 —— 不污染其它测试。"""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── 1. 尺寸全部由公式推导，不写魔数 ──────────────────────────────────────
def test_sheet_pixels_are_derived_not_hardcoded():
    """A5 竖版在 400/300dpi 下的像素必须等于公式值，也必须等于业界惯用值。"""
    assert providers.px_at(148, 400) == 2331
    assert providers.px_at(210, 400) == 3307
    assert providers.sheet_px(400) == (2331, 3307)
    assert providers.sheet_px(300) == (1748, 2480)
    # 公式本身：mm ÷ 25.4 × dpi，四舍五入
    for mm, dpi in ((148, 350), (210, 600), (105, 300)):
        assert providers.px_at(mm, dpi) == int(math.floor(mm / 25.4 * dpi + 0.5))


def test_min_short_edge_gate_is_derived_with_explicit_tolerance():
    """
    门禁 = 300dpi 理论短边 × (1 − 容差)，容差是显式常量而不是拍出来的 1748。
    这条同时锁住 R3 指出的「余量只有 12px」：现在余量必须 ≥20px。
    """
    gate = providers.min_short_edge_px()
    assert gate == providers.MIN_SHORT_EDGE_PX
    assert gate == int(math.floor(providers.px_at(148, 300) *
                                  (1 - providers.SHORT_EDGE_TOL)))
    assert gate == 1739, gate
    # 容差只吸收量化误差，不能放行低分辨率模型：折算有效 dpi 不低于 298
    assert providers.effective_dpi(gate) >= 298.0
    # gpt-image-2 的 a5-300 档短边 1760px 对门禁的余量
    assert 1760 - gate >= 20, "余量又缩回红队指出的十几像素了：%d" % (1760 - gate)
    # 换纸张/换 dpi 时不需要改常量
    assert providers.min_short_edge_px(600) == int(math.floor(
        providers.px_at(148, 600) * (1 - providers.SHORT_EDGE_TOL)))


def test_no_bare_magic_number_in_source():
    """源码里不允许再出现「MIN_SHORT_EDGE_PX = 1748」这类写死赋值。"""
    src = open(providers.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    for bad in ("MIN_SHORT_EDGE_PX = 1748", "MIN_SHORT_EDGE_PX = 1739",
                "TARGET_W_PX = 2331", "TARGET_H_PX = 3307"):
        assert bad not in src, "又写死了魔数：%s" % bad


# ── 2. 能力判定与显式降级 ────────────────────────────────────────────────
def test_seedream_cannot_do_400_but_can_do_300():
    caps = providers.PROVIDER_CAPS["volcengine"]
    hi, lo = providers.fits_dpi(caps, 400), providers.fits_dpi(caps, 300)
    assert not hi["ok"] and "总像素" in hi["why_not"], hi
    assert lo["ok"], lo
    assert lo["short_edge_headroom_px"] >= 0


def test_gemini_is_rejected_outright():
    plan = providers.dpi_plan("gemini", 400)
    assert plan["dpi"] is None, plan
    assert "换 provider" in plan["reason"] or "做不到" in plan["reason"], plan


def test_volcengine_plan_degrades_explicitly():
    with _env(ARK_IMAGE_SIZE=None):
        plan = providers.dpi_plan("volcengine", 400)
    assert plan["dpi"] == 300 and plan["degraded"] is True, plan
    assert "300dpi 交付" in plan["reason"], plan
    assert "标注" in plan["reason"], "降级必须要求标注：%s" % plan["reason"]


def test_openai_size_preset_controls_dpi_and_is_not_silent():
    if "openai" not in providers.PROVIDER_CAPS:
        return
    with _env(OPENAI_IMAGE_SIZE="a5-300"):
        p300 = providers.dpi_plan("openai", 400)
    with _env(OPENAI_IMAGE_SIZE="a5-400"):
        p400 = providers.dpi_plan("openai", 400)
    assert p300["dpi"] == 300 and p300["degraded"], p300
    # 能力够但配置压低了 → 必须提示怎么调，而不是默默按 300 交付
    assert "a5-400" in p300["reason"], p300["reason"]
    assert p400["dpi"] == 400 and not p400["degraded"], p400
    assert min(p400["configured_px"]) >= providers.MIN_SHORT_EDGE_PX


def test_capability_table_shows_requirements_and_headroom():
    txt = providers.capability_table(400)
    for key in ("400dpi", "300dpi", "余量", "门禁", "25.4"):
        assert key in txt, "能力表缺少「%s」这一信息：\n%s" % (key, txt)
    assert "未实测" in txt, "未实测的 provider 必须如实标注：\n%s" % txt


# ── 3. 请求体本地校验（不花钱就能挡住的错误） ────────────────────────────
def test_request_payload_validation_catches_bad_size():
    if "openai" not in providers._REQUEST_SCHEMA:
        return
    ok = providers.validate_request_payload(
        "openai", {"model": "gpt-image-2", "prompt": "x", "size": "2336x3312", "n": 1})
    assert ok == [], ok
    bad = providers.validate_request_payload(
        "openai", {"model": "gpt-image-2", "prompt": "x", "size": "800x600", "n": 1})
    assert bad, "800x600 远低于 300dpi 底线却校验通过了"
    missing = providers.validate_request_payload("openai", {"model": "gpt-image-2"})
    assert missing, "缺字段也应报出来"


# ── 4. 自检：不消耗额度，退出码分类正确，报错可操作 ──────────────────────
def test_selftest_reports_missing_key_as_config_error():
    if "volcengine" not in providers.REQUIRED_ENV:
        return
    with _env(ARK_API_KEY=None):
        checks, code = providers.selftest("volcengine", offline=True)
    assert code == 1, "缺 key 属配置问题，应退出码 1，实际 %d" % code
    fails = [c for c in checks if c["level"] == "fail"]
    assert any("ARK_API_KEY" in c["title"] for c in fails), fails
    assert all(c["howto"] for c in fails), "每条 fail 都必须给出怎么办"


def test_selftest_flags_malformed_key():
    if "openai" not in providers.REQUIRED_ENV:
        return
    with _env(OPENAI_API_KEY="not-a-real-key"):
        checks, code = providers.selftest("openai", offline=True)
    assert code == 1, code
    assert any(c["level"] == "fail" and "OPENAI_API_KEY" in c["title"] for c in checks), checks


def test_selftest_never_generates_images():
    """自检必须不产生任何生图调用 —— 用一个必然失败的假 key 也不能抛网络异常。"""
    with _env(OPENAI_API_KEY="sk-" + "x" * 40, ARK_API_KEY="x" * 32):
        for name in sorted(set(providers._GEN)):
            checks, _code = providers.selftest(name, offline=True)
            assert checks, name
            assert not any("生成" in c["title"] and "额度" in c["title"] for c in checks)


def test_selftest_entrypoints_exist_for_fail_fast():
    """forge.py 的 fail-fast 依赖这两个入口，签名不许改。"""
    assert callable(providers.print_selftest)
    assert callable(providers.capability_table)
    assert callable(providers.dpi_plan)


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
