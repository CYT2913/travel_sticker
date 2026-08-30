#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卡纸元素编号回归测试 —— 锁死「行带数写死 6」这个隐患（2026-08-30 复盘存疑项 1）。

为什么要测：`make_memory_card.grab_elements()` 按阅读顺序给元素编号，编号是
交付包 01.png~06.png 以及 `--only` / `--drop` 参数的依据。它原来把行带数写死成
6，一旦把每版枚数改成别的（--elements 9 / --max-elements 12），同一行的元素会
被分到不同带里 → 编号顺序错乱，而这种错乱肉眼很难发现（图都对，只是序号乱）。

不调用任何 AI 接口，用合成图（白底 + 若干色块）离线跑：
    python3 -m pytest tests/ -v
    python3 tests/test_card_element_numbering.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.environ.get(
    "DOCTOR_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "print-ready-doctor")))
import make_memory_card as mc  # noqa: E402

PPMM = 10.0                       # 合成图 10px/mm
SHEET_W_MM, SHEET_H_MM = 148.0, 210.0


def _sheet(rows, cols, blob_mm=15.0):
    """造一张白底贴纸版：rows×cols 个方色块，行列都均匀分布。"""
    W = int(SHEET_W_MM * PPMM)
    H = int(SHEET_H_MM * PPMM)
    img = np.full((H, W, 3), 255, np.uint8)
    b = int(blob_mm * PPMM)
    centers = []
    for r in range(rows):
        cy = int(H * (r + 0.5) / rows)
        for c in range(cols):
            cx = int(W * (c + 0.5) / cols)
            img[cy - b // 2:cy + b // 2, cx - b // 2:cx + b // 2] = (180, 90, 60)
            centers.append((cy, cx))
    return img, centers


def _assert_reading_order(elems, rows, cols):
    assert len(elems) == rows * cols, "只检出 %d 枚，应为 %d 枚" % (len(elems), rows * cols)
    for r in range(rows):
        row = elems[r * cols:(r + 1) * cols]
        xs = [e["cx"] for e in row]
        assert xs == sorted(xs), "第 %d 行内不是从左到右：%s" % (r + 1, xs)
        if r:
            prev = elems[(r - 1) * cols:r * cols]
            assert min(e["cy"] for e in row) > max(e["cy"] for e in prev), \
                "第 %d 行的元素混进了上一行（行带划分错了）" % (r + 1)


def test_grab_elements_takes_max_elems_parameter():
    """行带数必须是参数/常量，不能再写死 6。"""
    import inspect
    sig = inspect.signature(mc.grab_elements)
    assert "max_elems" in sig.parameters, "grab_elements 仍然没有枚数参数"
    assert mc.DEFAULT_MAX_ELEMS == 6, "默认值应与产品规格（每版 6 枚）一致"
    assert sig.parameters["max_elems"].default == mc.DEFAULT_MAX_ELEMS


def test_six_element_sheet_numbering_unchanged():
    """标准 6 枚（2 列 × 3 行）的编号顺序必须和以前完全一样。"""
    img, _ = _sheet(3, 2)
    _assert_reading_order(mc.grab_elements(img, PPMM, 6), 3, 2)


def test_twelve_element_sheet_numbering_is_reading_order():
    """改成 12 枚（2 列 × 6 行）时也必须是阅读顺序 —— 写死 6 时这里会错乱。"""
    img, _ = _sheet(6, 2, blob_mm=12.0)
    _assert_reading_order(mc.grab_elements(img, PPMM, 12), 6, 2)


def test_numbering_survives_stale_max_elems():
    """
    调用方忘了同步传枚数（仍用默认 6）也不能乱：行带数取「预期枚数」与
    「实际检出枚数」的较大值，所以 12 枚的版面照样按阅读顺序编号。
    """
    img, _ = _sheet(6, 2, blob_mm=12.0)
    _assert_reading_order(mc.grab_elements(img, PPMM), 6, 2)


def test_area_thresholds_stay_intentionally_different():
    """
    存疑项 2 的结论是「不统一，只补注释」。这里把结论锁住：
    卡纸的「值得上卡」阈值必须显著高于印前的「视为有效元素」阈值（25mm²），
    谁哪天顺手把两者对齐，这条会失败并提醒他去看注释。
    """
    assert mc.MIN_ELEM_AREA_MM2 == 120.0
    assert mc.MIN_ELEM_AREA_MM2 > 25.0 * 2, \
        "两个阈值服务不同目的（上卡门槛 vs 有效元素门槛），不要强行统一"


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
