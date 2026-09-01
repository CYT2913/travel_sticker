#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G0 重试 / 召回下限 / 断点续跑 / provider 退避 —— 回归测试
锁死 2026-09-01 v36 四张真图交付暴露的三个缺陷。

三件都是实测过的真事，不是假想：

  缺陷 A（G0 无重试）
      02 演出现场首跑，G0 视觉模型返回的 JSON 在 `"mode` 处被截断，
      `preflight()` 一句 raise SystemExit 就把整单结束了 —— 零重试。
      前面的 provider 自检、照片体检全部作废，人得手动重跑。

  缺陷 B（无断点续跑）
      01 生日跑了 5 次才成功，其中 2 次是 provider `400 Client Error`。
      每次都从 G0 重头开始，已经成功的 G0 与已经跑完的轮次全部白烧。

  06 银杏产品级事故（G0 召回抖动）
      同一张照片，G0 有一次只召回 6 项且全是树/墙同族，⭐随身物档只有
      `ginkgo leaf` 一项 —— 它一轮内撞互斥规则被永久剔除后池子当场见底，
      最终交付「4 物品 + 2 人物、银杏叶缺席」，技术指标全过但产品不合格。

不调用任何 AI 接口，不产生费用，可离线运行：
    python3 -m pytest tests/ -v
    python3 tests/test_g0_retry_and_resume.py      # 不装 pytest 也能跑
"""
import json
import os
import sys
import tempfile

FORGE_DIR = os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge"))
sys.path.insert(0, FORGE_DIR)

import forge        # noqa: E402
import providers    # noqa: E402

from PIL import Image  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# 公共夹具
# ══════════════════════════════════════════════════════════════════════════

# 02 演出现场首跑那次的返回形态：JSON 在字段中途被砍断（真实现场是断在 `"mode`）
TRUNCATED = ('好的，分析结果如下：\n{\n  "scene": "室内演出现场，舞台强光",\n'
             '  "lighting": "night",\n  "people_count": 4,\n'
             '  "standalone_objects": ["electric guitar", "bass drum",\n'
             '  "mode')

# 达标的返回：≥10 项、横跨随身物 / 自然物 / 建筑设施
GOOD_OBJECTS = ["ginkgo leaf", "backpack", "jacket", "sneakers", "smartphone",
                "water bottle", "ginkgo tree", "fallen leaves", "stone step",
                "traditional wall", "street lamp"]


def _good_json(objects=None, **over):
    d = {"scene": "秋日银杏大道", "lighting": "day", "people_count": 3,
         "has_minor": False, "ip_items": [], "modern_landmark_items": [],
         "carried_items": ["ginkgo leaf", "backpack"],
         "standalone_objects": list(objects if objects is not None else GOOD_OBJECTS),
         "composite_parts": [], "keepsake_objects": ["ginkgo leaf"],
         "container_pairs": [], "similar_pairs": [], "thin_parts": [],
         "reject_reason": None}
    d.update(over)
    return "这是结果：\n" + json.dumps(d, ensure_ascii=False)


# v36 06 银杏那次真实的召回（6 项，全是树/墙同族，随身物只有一片叶子）
THIN_OBJECTS = ["ginkgo leaf", "ginkgo tree", "traditional wall",
                "wall lattice", "tree trunk", "fallen leaves"]


class FakeVision(object):
    """替掉 forge.call_vision，按脚本逐次返回，并记录每次收到的 task 全文。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.tasks = []

    def __call__(self, paths, task):
        self.tasks.append(task)
        i = min(len(self.tasks) - 1, len(self.replies) - 1)
        return self.replies[i]

    @property
    def calls(self):
        return len(self.tasks)


class Vision(object):
    """with Vision(...) as fv: —— 装卸 forge.call_vision 的上下文管理器。"""

    def __init__(self, *replies):
        self.fake = FakeVision(*replies)

    def __enter__(self):
        self._orig = forge.call_vision
        forge.call_vision = self.fake
        return self.fake

    def __exit__(self, *_exc):
        forge.call_vision = self._orig
        return False


def _photo(d, name="src.jpg", w=1800, h=2400):
    p = os.path.join(d, name)
    Image.new("RGB", (w, h), (210, 170, 60)).save(p, "JPEG", quality=90)
    return p


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 A · G0 截断 JSON 必须重试
# ══════════════════════════════════════════════════════════════════════════

def test_parse_problem_names_truncation():
    """先钉住判据本身：截断的 JSON 必须被识别成「未闭合/疑似截断」。"""
    got = forge.g0_parse_problem(TRUNCATED, forge.grab_json(TRUNCATED))
    assert got, "截断的 JSON 被当成可用返回了 —— 这就是 02 演出现场栽的地方"
    assert "截断" in got or "未闭合" in got, got


def test_parse_problem_flags_empty_and_non_json():
    assert "空" in (forge.g0_parse_problem("", None) or "")
    assert "空" in (forge.g0_parse_problem("   \n ", None) or "")
    assert forge.g0_parse_problem("模型说：这张图我看不了", None)
    # 能解析但 standalone_objects 空 + 没给拒稿原因 = 半截 JSON，也不算可用
    assert forge.g0_parse_problem('{"scene":"x","standalone_objects":[]}',
                                  {"scene": "x", "standalone_objects": []})
    # 正常返回不许误判
    txt = _good_json()
    assert forge.g0_parse_problem(txt, forge.grab_json(txt)) is None


def test_preflight_retries_truncated_json_then_succeeds():
    """核心回归：第 1 次截断、第 2 次正常 → 必须重试并最终成功，不是整单失败。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        with Vision(TRUNCATED, _good_json()) as fv:
            info, small = forge.preflight(photo, d)
        assert fv.calls == 2, "没有重试（调用了 %d 次）—— 缺陷 A 未修" % fv.calls
        assert "ginkgo leaf" in info["standalone_objects"]
        assert info["_short_edge_px"] == 1800
        assert os.path.isfile(small)


def test_preflight_retry_asks_model_for_json_only():
    """重试不能原样再问一遍：必须改请求，明确要求只输出 JSON、把值写短。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        with Vision(TRUNCATED, _good_json()) as fv:
            forge.preflight(photo, d)
        first, second = fv.tasks[0], fv.tasks[1]
        assert first != second, "重试用的是完全相同的请求，对「输出被截断」无效"
        assert "只输出一个 JSON 对象" in second
        assert "完整闭合" in second or "写短" in second


def test_preflight_gives_up_after_limit_and_says_g0_stage():
    """一直截断：必须在上限处停手（不许无限重试），且报错说清是 G0 阶段失败。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        with Vision(TRUNCATED) as fv:
            try:
                forge.preflight(photo, d)
            except SystemExit as e:
                msg = str(e)
            else:
                raise AssertionError("一直截断却没有失败退出")
        assert fv.calls == forge.G0_MAX_ATTEMPTS, \
            "重试次数是 %d，应为上限 %d（不能无限重试，生图/视觉都花钱）" \
            % (fv.calls, forge.G0_MAX_ATTEMPTS)
        assert "G0" in msg, "报错没说清是 G0 阶段失败：%s" % msg
        assert "3" in msg or "连续" in msg
        assert "截断" in msg or "未闭合" in msg, "报错没写清可恢复错误的类型：%s" % msg


def test_g0_retry_limit_is_finite_and_small():
    assert 2 <= forge.G0_MAX_ATTEMPTS <= 5, \
        "G0 重试上限 %d 不合理（要有限且小）" % forge.G0_MAX_ATTEMPTS


# ══════════════════════════════════════════════════════════════════════════
# 06 银杏 · G0 召回数量与类别下限
# ══════════════════════════════════════════════════════════════════════════

def test_recall_report_flags_the_real_v36_ginkgo_pool():
    """v36 06 银杏那次的 6 项召回必须被判为不达标。"""
    ok, n, cats, desc = forge.g0_recall_report(THIN_OBJECTS)
    assert not ok, "6 项全树/墙的候选池被判达标了：%s" % desc
    assert n == 6
    ok2, n2, cats2, _ = forge.g0_recall_report(GOOD_OBJECTS)
    assert ok2, "跨类别的 11 项候选池被判不达标"
    assert {"carry", "nature", "facility"} <= cats2


def test_recall_categories_are_sane():
    assert forge.g0_category("backpack") == "carry"
    assert forge.g0_category("ginkgo leaf") == "carry"      # 捡起来能带走
    assert forge.g0_category("ginkgo tree") == "nature"
    assert forge.g0_category("traditional wall") == "facility"
    assert forge.G0_MIN_OBJECTS >= 10
    assert forge.G0_MIN_CATEGORIES >= 3


def test_preflight_reasks_when_recall_is_thin_and_merges_result():
    """召回不足 → 补问一次 → 结果并集。银杏叶不能在合并中丢掉。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        with Vision(_good_json(THIN_OBJECTS), _good_json()) as fv:
            info, _ = forge.preflight(photo, d)
        assert fv.calls == 2, "召回只有 6 项却没有补问（调用 %d 次）" % fv.calls
        assert "再补充一些不同类别的物品" in fv.tasks[1]
        assert "随身携带" in fv.tasks[1], "补问没有强调「人物随身携带的东西」"
        objs = info["standalone_objects"]
        assert "ginkgo leaf" in objs, "补问后把主角银杏叶弄丢了"
        assert "backpack" in objs and "sneakers" in objs, objs
        assert forge.g0_recall_report(objs)[0], "补问后仍不达标"


def test_preflight_survives_permanently_thin_photo():
    """照片确实只有这些元素时：只警告不阻断，且补问次数有上限。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        with Vision(_good_json(THIN_OBJECTS)) as fv:
            info, _ = forge.preflight(photo, d)
        assert fv.calls == 1 + forge.G0_RECALL_ATTEMPTS, \
            "补问次数 %d 超出上限（不能无限追问）" % (fv.calls - 1)
        assert info.get("_recall_warning"), "召回仍不足却没有留下警告"
        assert info["standalone_objects"], "不该因为召回薄就把候选清空"


def test_preflight_task_asks_for_ten_plus_cross_category():
    assert "至少10个" in forge.PREFLIGHT_TASK
    assert "跨类别" in forge.PREFLIGHT_TASK


def test_merge_is_order_preserving_and_deduped():
    base = {"standalone_objects": ["ginkgo leaf", "ginkgo tree"],
            "carried_items": ["ginkgo leaf"], "keepsake_objects": [],
            "ip_items": [], "modern_landmark_items": [], "thin_parts": [],
            "container_pairs": [], "similar_pairs": [], "composite_parts": []}
    extra = {"standalone_objects": ["Ginkgo Leaf", "backpack"],
             "carried_items": ["backpack"], "keepsake_objects": [],
             "ip_items": [], "modern_landmark_items": [], "thin_parts": [],
             "container_pairs": [["a", "b"]], "similar_pairs": [],
             "composite_parts": [["lego set", "lego brick"]]}
    got = forge._g0_merge(base, extra)
    assert got["standalone_objects"] == ["ginkgo leaf", "ginkgo tree", "backpack"], \
        got["standalone_objects"]
    assert got["carried_items"] == ["ginkgo leaf", "backpack"]
    assert got["container_pairs"] == [["a", "b"]]
    assert got["composite_parts"] == [["lego set", "lego brick"]]


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 B · 断点续跑
# ══════════════════════════════════════════════════════════════════════════

def _write_preflight(outdir, **over):
    d = {"scene": "x", "lighting": "day", "people_count": 3,
         "standalone_objects": list(GOOD_OBJECTS), "keepsake_objects": [],
         "carried_items": ["backpack"], "ip_items": [],
         "container_pairs": [], "similar_pairs": [], "thin_parts": [],
         "_short_edge_px": 1800, "_size_px": "1800x2400"}
    d.update(over)
    with open(os.path.join(outdir, "preflight.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    return d


def test_cached_preflight_accepts_valid_and_rejects_garbage():
    with tempfile.TemporaryDirectory() as d:
        assert forge.cached_preflight(d)[0] is None            # 文件不存在
        _write_preflight(d)
        assert forge.cached_preflight(d)[0] is not None
        # 空候选 = 那次 G0 本来就没成功，不能复用
        _write_preflight(d, standalone_objects=[])
        info, why = forge.cached_preflight(d)
        assert info is None and "standalone_objects" in why
        # 缺 _short_edge_px = 旧版本产物，复用会让分辨率门禁失效
        _write_preflight(d, _short_edge_px=None)
        assert forge.cached_preflight(d)[0] is None
        # 坏 JSON
        with open(os.path.join(d, "preflight.json"), "w", encoding="utf-8") as f:
            f.write('{"scene": "x", ')
        info, why = forge.cached_preflight(d)
        assert info is None and "JSON" in why


def test_resolve_preflight_reuses_existing_and_never_calls_vision():
    """核心回归 B-1：preflight.json 有效时，绝不再调用视觉模型。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        work = os.path.join(d, "_work"); os.makedirs(work)
        _write_preflight(d)
        with Vision(_good_json()) as fv:
            info, small, note = forge.resolve_preflight(photo, d, work, fresh=False)
        assert fv.calls == 0, "复用断点时仍调用了 %d 次视觉模型 —— 缺陷 B 未修" % fv.calls
        assert note and "preflight.json" in note, "没有说明复用了什么"
        assert info["standalone_objects"] == GOOD_OBJECTS
        assert os.path.isfile(small), "复用 G0 也必须产出喂给 provider 的压缩图"


def test_resolve_preflight_fresh_forces_a_real_g0_run():
    """核心回归 B-2：--fresh 必须无视断点，重新真跑 G0。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        work = os.path.join(d, "_work"); os.makedirs(work)
        _write_preflight(d, scene="旧的缓存场景")
        with Vision(_good_json(scene="重新跑出来的场景")) as fv:
            info, _small, note = forge.resolve_preflight(photo, d, work, fresh=True)
        assert fv.calls == 1, "--fresh 没有强制重跑 G0（调用 %d 次）" % fv.calls
        assert note is None
        assert info["scene"] == "重新跑出来的场景"


def test_resolve_preflight_reruns_when_cache_is_invalid():
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        work = os.path.join(d, "_work"); os.makedirs(work)
        _write_preflight(d, standalone_objects=[])
        with Vision(_good_json()) as fv:
            info, _s, note = forge.resolve_preflight(photo, d, work, fresh=False)
        assert fv.calls == 1 and note is None
        assert info["standalone_objects"]


def _touch(path, size=16):
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


def test_round_cache_reuses_only_when_prompt_is_identical():
    """轮产物的复用键必须是 prompt 摘要：换过元素后旧图必须作废。"""
    with tempfile.TemporaryDirectory() as d:
        _touch(os.path.join(d, "round1.png"))
        _touch(os.path.join(d, "round1_relayout.png"))
        state = forge.record_round({}, 1, "PROMPT-A",
                                   os.path.join(d, "round1.png"),
                                   os.path.join(d, "round1_relayout.png"),
                                   {"n_elements": 6}, {"fails": [], "dpi": 400},
                                   {"fails": [], "n_elements": 6}, d)
        assert os.path.isfile(os.path.join(d, forge.RESUME_FILE))
        rec, why = forge.round_cache(state, 1, "PROMPT-A", d)
        assert rec is not None, why
        assert rec["png"] == "round1.png"
        rec2, why2 = forge.round_cache(state, 1, "PROMPT-B（换过元素）", d)
        assert rec2 is None and "prompt" in why2.lower(), why2
        assert forge.round_cache(state, 2, "PROMPT-A", d)[0] is None


def test_round_cache_rejects_when_artifact_is_gone():
    with tempfile.TemporaryDirectory() as d:
        png = _touch(os.path.join(d, "round1.png"))
        sheet = _touch(os.path.join(d, "round1_relayout.png"))
        state = forge.record_round({}, 1, "P", png, sheet, {},
                                   {"fails": []}, {"fails": []}, d)
        os.remove(sheet)
        rec, why = forge.round_cache(state, 1, "P", d)
        assert rec is None and "不在" in why, why


def test_resume_state_survives_reload_and_ignores_corruption():
    with tempfile.TemporaryDirectory() as d:
        png = _touch(os.path.join(d, "round1.png"))
        sheet = _touch(os.path.join(d, "round1_relayout.png"))
        forge.record_round({}, 1, "P", png, sheet, {}, {"fails": []}, {"fails": []}, d)
        assert forge.round_cache(forge.load_resume(d), 1, "P", d)[0] is not None
        with open(os.path.join(d, forge.RESUME_FILE), "w", encoding="utf-8") as f:
            f.write("{坏文件")
        assert forge.load_resume(d) == {"rounds": {}}, "坏断点文件必须当作没有断点"


def test_resume_never_persists_the_huge_raw_report():
    with tempfile.TemporaryDirectory() as d:
        png = _touch(os.path.join(d, "round1.png"))
        sheet = _touch(os.path.join(d, "round1_relayout.png"))
        forge.record_round({}, 1, "P", png, sheet, {},
                           {"fails": [], "raw": "R" * 5000}, {"fails": []}, d)
        with open(os.path.join(d, forge.RESUME_FILE), encoding="utf-8") as f:
            txt = f.read()
        assert "RRRR" not in txt, "断点文件里存了几十 KB 的报告全文"


def test_main_is_actually_wired_to_resume():
    """接线测试：光有 helper 不算修好，主流程必须真的用上，且开关要在。"""
    with open(os.path.join(FORGE_DIR, "forge.py"), encoding="utf-8") as f:
        src = f.read()
    for token in ("--fresh", "--no-resume", "resolve_preflight(",
                  "round_cache(", "record_round(", "_log_reused("):
        assert token in src, "forge.py 主流程里找不到 %s" % token
    assert "复用" in src, "续跑日志里没有「复用」字样，人会误以为是重新跑的"


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 B · provider 400 指数退避
# ══════════════════════════════════════════════════════════════════════════

class FakeGen(object):
    """按脚本失败若干次再成功的假生图 provider，登记进 providers._GEN。"""

    NAME = "fake_retry"

    def __init__(self, errors):
        self.errors = list(errors)      # 每次调用要抛的异常（None = 成功出图）
        self.calls = 0

    def __call__(self, photo, prompt, out_png):
        i = self.calls
        self.calls += 1
        err = self.errors[i] if i < len(self.errors) else None
        if err is not None:
            raise err
        w = providers.MIN_SHORT_EDGE_PX + 8
        Image.new("RGB", (w, int(w * 210 / 148)), (240, 230, 200)).save(out_png)
        return out_png

    def __enter__(self):
        providers._GEN[self.NAME] = self
        self._orig_provider = providers.PROVIDER
        self._orig_env = os.environ.pop("FORGE_IMAGE_PROVIDER", None)
        providers.PROVIDER = self.NAME
        self.slept = []
        self._orig_sleep = providers.time.sleep
        providers.time.sleep = lambda s: self.slept.append(s)
        return self

    def __exit__(self, *_exc):
        providers.time.sleep = self._orig_sleep
        providers.PROVIDER = self._orig_provider
        providers._GEN.pop(self.NAME, None)
        if self._orig_env is not None:
            os.environ["FORGE_IMAGE_PROVIDER"] = self._orig_env
        return False


HTTP400 = "400 Client Error: Bad Request for url: https://example/api/generate"


def test_transient_classification():
    assert providers._is_transient(HTTP400), "400 Client Error 没被判成瞬时错误"
    assert providers._is_transient("HTTPError 503 Service Unavailable")
    assert providers._is_transient("Read timed out")
    # 永久性错误一律不重试，否则白等 14 秒、白烧 3 倍额度
    assert not providers._is_transient("401 Unauthorized: invalid api key")
    assert not providers._is_transient("model not found: doubao-seedream-4-0-250828")
    assert not providers._is_transient("rejected by content policy")
    # 分辨率不达标是必然失败，且消息里带 1739/2352x3520 这类数字，不许被状态码误命中
    assert not providers._is_transient(
        "provider x 输出 1024x1536，短边不足 1739px（A5 @300dpi 底线）")


def test_backoff_is_2_4_8():
    assert providers.GEN_BACKOFF_SEC[:3] == (2, 4, 8)
    assert providers.GEN_MAX_ATTEMPTS == 3
    assert [providers._backoff_sec(i) for i in (1, 2, 3)] == [2, 4, 8]


def test_generate_image_retries_400_then_succeeds():
    """核心回归：连撞两次 400 后第 3 次成功 → 整体成功，间隔 2/4 秒。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        out = os.path.join(d, "round1.png")
        with FakeGen([RuntimeError(HTTP400), RuntimeError(HTTP400), None]) as fg:
            got = providers.generate_image(photo, "prompt", out)
        assert fg.calls == 3, "只调用了 %d 次，没有重试" % fg.calls
        assert fg.slept == [2, 4], "退避间隔是 %s，应为 [2, 4]" % fg.slept
        assert os.path.isfile(got) and os.path.getsize(got) > 0


def test_generate_image_stops_at_three_attempts():
    """一直 400：必须在 3 次处停手，报错要说清已重试过。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        out = os.path.join(d, "round1.png")
        with FakeGen([RuntimeError(HTTP400)] * 9) as fg:
            try:
                providers.generate_image(photo, "prompt", out)
            except providers.ProviderError as e:
                msg = str(e)
            else:
                raise AssertionError("一直 400 却没有失败")
        assert fg.calls == 3, "重试了 %d 次（应为 3，生图花钱）" % fg.calls
        assert fg.slept == [2, 4], fg.slept
        assert "重试" in msg and "瞬时" in msg, msg


def test_generate_image_does_not_retry_permanent_errors():
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        out = os.path.join(d, "round1.png")
        err = RuntimeError("401 Unauthorized: invalid api key")
        with FakeGen([err] * 5) as fg:
            try:
                providers.generate_image(photo, "prompt", out)
            except RuntimeError:
                pass
            else:
                raise AssertionError("永久性错误却成功了？")
        assert fg.calls == 1, "永久性错误被重试了 %d 次，白烧额度" % fg.calls
        assert fg.slept == []


def test_retry_never_delivers_the_previous_rounds_leftover():
    """重试前必须清掉同名旧文件，绝不能把上一次的产物当本次结果交付。"""
    with tempfile.TemporaryDirectory() as d:
        photo = _photo(d)
        out = os.path.join(d, "round1.png")
        _touch(out, size=1234)                      # 上一次跑剩下的旧图
        with FakeGen([RuntimeError(HTTP400)] * 9) as fg:
            try:
                providers.generate_image(photo, "prompt", out)
            except providers.ProviderError:
                pass
        assert not os.path.exists(out), "生图全败后旧产物还在，可能被当成本次结果交付"


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
