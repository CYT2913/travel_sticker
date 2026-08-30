#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读图方向 + 枚数门禁回归测试 —— 锁死 2026-08-30 实拍暴露的两个缺陷。

两个都是真事，不是假想：
  1. 06 银杏的源图 EXIF Orientation=6，产线按像素读图 → 场景图整幅横躺。
     所有自动检查（dpi / 邻距 / 枚数 / 刀线）全部通过，只有肉眼能发现。
  2. 02 演出现场第一次跑，模型只画了 5 枚（漏了人物那一枚），
     `qc_visual` 只判「纯物品 ≥ 5」，5 >= 5 成立 → 判「双重质检全过」并交付。

不调用任何 AI 接口，不产生费用，可离线运行：
    python3 -m pytest tests/ -v
    python3 tests/test_photo_io_and_counts.py     # 不装 pytest 也能跑
"""
import os
import sys
import tempfile

# 默认测同目录旁边的那份 forge.py；用 FORGE_DIR 可指向另一份副本
sys.path.insert(0, os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge")))
import forge  # noqa: E402

from PIL import Image  # noqa: E402


# ── 缺陷 1 · EXIF 方向 ────────────────────────────────────────────────────

def _make_rotated_jpeg(path, w=48, h=24, orientation=6):
    """造一张「像素是横的、EXIF 说它该竖着显示」的照片，等于手机竖拍的原图。"""
    im = Image.new("RGB", (w, h), (200, 60, 40))
    im.paste((40, 60, 200), (0, 0, w // 2, h))      # 左半蓝，方便判断有没有转
    ex = Image.Exif()
    ex[274] = orientation                            # 274 = Orientation
    im.save(path, "JPEG", exif=ex, quality=95)
    return path


def test_open_photo_applies_exif_orientation():
    """open_photo 必须按 EXIF 摆正；直接 Image.open 是躺着的。"""
    with tempfile.TemporaryDirectory() as d:
        p = _make_rotated_jpeg(os.path.join(d, "ginkgo.jpg"))
        assert Image.open(p).size == (48, 24), "前提不成立：造的图本身不是横躺的"
        assert forge.open_photo(p).size == (24, 48), \
            "open_photo 没有按 EXIF Orientation=6 旋转 —— 06 银杏横躺的根因"
        # 转完必须把标签抹掉，否则下游再转一次就又躺回去了
        assert forge.open_photo(p).getexif().get(274) in (None, 1)


def test_shrink_output_is_upright():
    """喂给视觉模型和 provider 的压缩图必须已经是正的（产线全靠它）。"""
    with tempfile.TemporaryDirectory() as d:
        p = _make_rotated_jpeg(os.path.join(d, "ginkgo.jpg"), 1200, 600)
        out = forge.shrink(p, os.path.join(d, "small.jpg"), box=400)
        w, h = Image.open(out).size
        assert h > w, "压缩图仍是横躺的（%dx%d）—— 场景图会整幅躺下" % (w, h)


def test_open_photo_is_noop_without_exif():
    """读自己生成的中间产物（PNG，无 EXIF）时必须原样返回，不能有副作用。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "round1.png")
        Image.new("RGB", (60, 30), (255, 255, 255)).save(p)
        assert forge.open_photo(p).size == (60, 30)


def test_preflight_size_fields_use_upright_pixels():
    """G0 记录的尺寸也必须是摆正后的，否则报告里长宽是反的。"""
    with tempfile.TemporaryDirectory() as d:
        p = _make_rotated_jpeg(os.path.join(d, "ginkgo.jpg"), 2000, 1500)
        w, h = forge.open_photo(p).size
        assert (w, h) == (1500, 2000)
        assert min(w, h) == 1500      # 短边门禁不受方向影响，但 _size_px 会


def test_print_ready_doctor_also_corrects_orientation():
    """印前侧同样要摆正：doctor 直接吃用户源图/成品图，方向错了刀线也跟着错。
    整条产线里读用户照片的入口都得走 open_photo，漏一个就等于没修。"""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "print-ready-doctor"))
    import print_ready_doctor as prd
    with tempfile.TemporaryDirectory() as d:
        p = _make_rotated_jpeg(os.path.join(d, "sheet.jpg"), 1200, 600)
        assert prd.open_photo(p).size == (600, 1200)


def test_print_ready_doctor_missing_file_message_is_clear():
    """文件不存在时要报明确的错，不能等到后面 NoneType / 空序列才炸。"""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "print-ready-doctor"))
    import print_ready_doctor as prd
    try:
        prd.open_photo("/tmp/__does_not_exist__.png")
    except BaseException as e:              # 目前是 SystemExit（带中文提示）
        assert "/tmp/__does_not_exist__.png" in str(e)
    else:
        raise AssertionError("读不存在的文件竟然没报错")


# ── 缺陷 2 · 总枚数硬门禁 ─────────────────────────────────────────────────

def test_missing_figure_element_is_rejected():
    """02 演出现场实拍：模型只画了 5 枚（漏人物），旧逻辑判通过。"""
    d = {"n_elements": 5, "n_object_only": 5, "n_with_people": 0}
    fails = forge.count_fails(d, n=6, n_obj=5, n_ppl=1)
    assert fails, "总枚数 5 ≠ 6 却判通过 —— 这就是那单交付了 5 枚的原因"
    assert any("5" in f and "6" in f for f in fails), fails


def test_pure_object_sheet_not_falsely_blocked():
    """03 故宫是纯物品 6 枚（照片里没人），合规，绝不能误拦。"""
    d = {"n_elements": 6, "n_object_only": 6, "n_with_people": 0}
    assert forge.count_fails(d, n=6, n_obj=6, n_ppl=0) == []


def test_five_objects_plus_one_figure_passes():
    """有人的照片：5 物品 + 1 人物 = 6 枚，正常构成。"""
    d = {"n_elements": 6, "n_object_only": 5, "n_with_people": 1}
    assert forge.count_fails(d, n=6, n_obj=5, n_ppl=1) == []


def test_too_many_elements_is_rejected():
    """多画一枚同样是不合格：刀线数和交付清单都会对不上。"""
    d = {"n_elements": 7, "n_object_only": 6, "n_with_people": 1}
    assert forge.count_fails(d, n=6, n_obj=5, n_ppl=1)


def test_unknown_count_is_rejected():
    """视觉模型没给出枚数时不能默认放行 —— 未经确认就是不合格。"""
    assert forge.count_fails({}, n=6, n_obj=5, n_ppl=1)
    assert forge.count_fails({"n_elements": None}, n=6, n_obj=5, n_ppl=1)


def test_figure_missing_but_count_right_is_rejected():
    """总数对但人物那枚没画（6 枚全是物品）：产品规格是 5 物品 + 1 人物。"""
    d = {"n_elements": 6, "n_object_only": 6, "n_with_people": 0}
    assert forge.count_fails(d, n=6, n_obj=5, n_ppl=1)


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
