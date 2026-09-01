#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型返回形状健壮性回归测试（2026-09-01 离线冒烟实测踩到的崩溃）。

背景：视觉模型不是稳定 API。prompt 里要 `[["A","B"]]`，它完全可能回
`{"A":"B"}`；要 `["a","b"]`，它可能回 `"a, b"` 或 `[{"name":"a"}]`。
旧实现直接 `list + list` / `", ".join(...)`，遇到 dict 就 TypeError，
**抛点在 G0 之后** —— 表现就是「这一单当场崩掉，交付不出来」。
这比漏判严重：漏判还能靠质检兜，崩了就直接没有交付物。

所以这里逐一钉住：任何形状都不许把产线弄崩，解析不了就退化成空，
让词库和通用中心词规则接手（失败可收敛，而不是失败即崩）。
不调用任何 AI，可离线运行。
"""
import os
import sys

FORGE_DIR = os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge"))
sys.path.insert(0, FORGE_DIR)

import forge  # noqa: E402


# ── _pairs_from：整体→单件映射的各种形状 ────────────────────────────────
def test_pairs_from_accepts_dict():
    assert forge._pairs_from({"lego set": "lego brick"}) == [("lego set", "lego brick")]


def test_pairs_from_accepts_list_of_pairs():
    assert forge._pairs_from([["drum kit", "bass drum"]]) == [("drum kit", "bass drum")]


def test_pairs_from_accepts_list_of_dicts():
    got = forge._pairs_from([{"whole": "gachapon machine", "part": "capsule toy"},
                             {"object": "camera tripod",
                              "representative_part": "camera"}])
    assert got == [("gachapon machine", "capsule toy"), ("camera tripod", "camera")]


def test_pairs_from_accepts_arrow_strings():
    for s in ("lego set -> lego brick", "lego set → lego brick",
              "lego set: lego brick"):
        got = [(a.strip(), b.strip()) for a, b in forge._pairs_from([s])]
        assert got == [("lego set", "lego brick")], (s, got)


def test_pairs_from_never_raises_on_garbage():
    for junk in (None, 0, "", [], {}, 3.14, ["单个字符串没有分隔符"],
                 [None, [], {}, [1]], {"a": None}, object()):
        forge._pairs_from(junk)          # 只要不抛异常即可


def test_composite_map_survives_dict_shape():
    """就是这条崩过：composite_parts 回 dict 时 `dict + list` 抛 TypeError。"""
    info = {"composite_parts": {"lego set": "lego brick"},
            "_composite_parts": [["drum kit", "bass drum"]]}
    m = forge.composite_map(info)
    assert m["lego set"] == "lego brick" and m["drum kit"] == "bass drum"
    assert forge._decompose("lego set", m)[0] == "lego brick"


# ── _strlist：名字清单的各种形状 ─────────────────────────────────────────
def test_strlist_shapes():
    assert forge._strlist(["a", "b"]) == ["a", "b"]
    assert forge._strlist("a, b、c") == ["a", "b", "c"]
    assert forge._strlist([{"name": "water bottle"}, {"element": "hat"}]) == \
        ["water bottle", "hat"]
    assert forge._strlist([["bass drum", "支架"]]) == ["bass drum"]
    assert forge._strlist({"k": "v"}) == ["v"]
    assert forge._strlist(None) == [] and forge._strlist({}) == []


def test_strlist_never_raises():
    for junk in (0, 3.14, object(), [None], [[]], [{}], set(["x"])):
        forge._strlist(junk)


def test_qc_visual_fail_text_survives_dict_shaped_lists(monkeypatch=None):
    """质检文案拼接不能因为模型回 [{...}] 而崩。"""
    d = {"thin_parts": [{"name": "mic stand"}],
         "elements_with_support_rig": "drum rack, cymbal stand",
         "banned_ip_or_landmark": {"x": "national stadium"},
         "missing_objects": [["paper ticket"]],
         "composite_elements": {"lego set": "lego brick"},
         "objects_with_container": [{"name": "cake on table"}]}
    # 直接复用 qc_visual 的判定段：这里用 count_fails 之外的分支，
    # 所以借 forge.fail_tags 验证文案能被正常生成并归类
    txt = "以下结构仍过细，必须加粗（禁止删除物品本体）：%s" % ", ".join(
        forge._strlist(d["thin_parts"]))
    assert "mic stand" in txt and forge.fail_tags(txt) == {"too_thin"}
    assert forge._strlist(d["elements_with_support_rig"]) == ["drum rack", "cymbal stand"]
    assert forge._strlist(d["banned_ip_or_landmark"]) == ["national stadium"]
    assert forge._strlist(d["missing_objects"]) == ["paper ticket"]
    assert forge._pairs_from(d["composite_elements"]) == [("lego set", "lego brick")]
    assert forge._strlist(d["objects_with_container"]) == ["cake on table"]


# ── G0 边界归一：崩点在 G0 之后，所以必须在边界就修好 ────────────────────
def test_preflight_normalizes_model_shapes(tmp_path=None):
    import tempfile
    import json as _json
    from PIL import Image
    tmp = tempfile.mkdtemp()
    photo = os.path.join(tmp, "p.jpg")
    Image.new("RGB", (1800, 2400), (180, 170, 160)).save(photo)
    raw = {
        "scene": "校园",
        "standalone_objects": "paper ticket, water bottle、bucket hat, lego set",
        "keepsake_objects": [{"name": "paper ticket"}],
        "thin_parts": None,
        "ip_items": {"a": "brand logo"},
        "modern_landmark_items": "national stadium",
        "carried_items": [{"name": "water bottle"}],
        "composite_parts": {"lego set": "lego brick"},
        "people_count": 1,
    }
    forge.call_vision = lambda paths, task: _json.dumps(raw, ensure_ascii=False)
    info, _small = forge.preflight(photo, tmp)
    assert info["standalone_objects"] == ["paper ticket", "water bottle",
                                          "bucket hat", "lego set"]
    assert info["keepsake_objects"] == ["paper ticket"]
    assert info["ip_items"] == ["brand logo"]
    assert info["modern_landmark_items"] == ["national stadium"]
    assert info["carried_items"] == ["water bottle"]
    assert info["composite_parts"] == [["lego set", "lego brick"]]
    # 归一之后，选品/收敛判断都能正常跑（以前这里直接 TypeError）
    picked, _dropped = forge.select_objects(info, 4)
    assert "lego brick" in picked and "national stadium" not in picked
    assert forge.candidate_pool(info)


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
