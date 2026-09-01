#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
隐私回归测试（红队 R5）。

R5 的教训是：**不能靠肉眼读 .gitignore 判断隐私是否安全**。
两次真实漏洞都是「规则看着挺全，实测有洞」：
  ① 只忽略了 *.jpg/*.jpeg，漏了 *.png；
  ② 忽略规则大小写敏感，手机导出的 IMG_1234.JPG / .HEIC 全部漏网。

所以这里不检查 .gitignore 的文本，而是：
  · 用 git check-ignore 实测一批「典型客户照片路径」是否真的被忽略；
  · 用 git ls-files 实测【当前被跟踪的】媒体文件是否都在 examples/ 白名单里。
第二条是关键 —— .gitignore 管不住已经被跟踪的文件，只有 ls-files 能查出来。

不联网、不调用 AI。仓库不是 git 仓库时整体 skip（例如从 zip 解包的副本）。
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp",
              ".gif", ".tif", ".tiff", ".dng", ".raw", ".mp4", ".mov",
              ".avi", ".hevc")

# 白名单：只有这里的前缀允许在 examples/ 下被跟踪（示例图必须显式改名，
# 改名这个动作就是「我确认这张已脱敏」的人工确认）。
ALLOW_PREFIXES = ("examples/示例", "examples/example_", "examples/README")

# 典型的「不该进仓库」的路径。大小写混写是故意的：手机导出就是大写。
MUST_IGNORE = [
    "客户照片.jpg", "客户照片.JPG", "IMG_1234.JPG", "IMG_1234.jpeg",
    "photo.PNG", "photo.png", "IMG_0001.HEIC", "IMG_0001.heic",
    "clip.MOV", "clip.mp4", "scan.TIF", "raw/IMG.DNG",
    "examples/IMG_1234.JPG",            # 白名单目录里也不能随手放原图
    "run_01/round1.png", "交付_20260901/A5贴纸版.png",
    "artifacts/edited_image_101010.png", "forge_out/x.jpg",
]

# 必须仍然可提交的东西（防止「一刀切全忽略」把示例图和源码也挡了）
MUST_TRACKABLE = [
    "examples/示例2_银杏_A5贴纸版.png",
    "examples/example_ginkgo.png",
    "memory-sticker-forge/forge.py",
    "README.md",
]


def _git(*args):
    return subprocess.run(("git",) + args, cwd=REPO,
                          capture_output=True, text=True)


def _skip(msg):
    try:
        import pytest
        pytest.skip(msg)
    except ImportError:
        print("     (skip: %s)" % msg)
    return True


def _is_repo():
    r = _git("rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def test_customer_photo_paths_are_really_ignored():
    if not _is_repo():
        return _skip("不是 git 仓库")
    leaked = [p for p in MUST_IGNORE
              if _git("check-ignore", "-q", p).returncode != 0]
    assert not leaked, ("这些路径没有被 .gitignore 挡住，客户照片可能被推到公开仓库：\n  "
                        + "\n  ".join(leaked))


def test_examples_and_source_are_still_committable():
    if not _is_repo():
        return _skip("不是 git 仓库")
    blocked = [p for p in MUST_TRACKABLE
               if _git("check-ignore", "-q", p).returncode == 0]
    assert not blocked, ("忽略规则过宽，把这些该进仓库的文件也挡掉了：\n  "
                         + "\n  ".join(blocked))


def test_no_tracked_media_outside_examples_whitelist():
    """.gitignore 管不住【已经被跟踪】的文件，所以这条必须查 git ls-files。"""
    if not _is_repo():
        return _skip("不是 git 仓库")
    r = _git("-c", "core.quotePath=false", "ls-files")
    assert r.returncode == 0, r.stderr
    tracked = [l for l in r.stdout.splitlines() if l.strip()]
    media = [p for p in tracked if os.path.splitext(p)[1].lower() in MEDIA_EXTS]
    bad = [p for p in media if not p.startswith(ALLOW_PREFIXES)]
    assert not bad, ("仓库里有被跟踪的媒体文件不在 examples/ 白名单内，"
                     "可能是真实用户照片：\n  " + "\n  ".join(bad))


def test_tracked_examples_are_deliverables_not_source_photos():
    """示例图必须是【成品图】（贴纸版/卡纸打印图），不能是原始照片。
    命名约定就是唯一的机器可判据，所以约定要守住。"""
    if not _is_repo():
        return _skip("不是 git 仓库")
    r = _git("-c", "core.quotePath=false", "ls-files", "examples")
    tracked = [l for l in r.stdout.splitlines() if l.strip()]
    media = [p for p in tracked if os.path.splitext(p)[1].lower() in MEDIA_EXTS]
    assert media, "examples/ 下一张示例图都没有，README 会挂图失败"
    ok_words = ("贴纸版", "卡纸打印图", "sticker", "card")
    bad = [p for p in media if not any(w in p for w in ok_words)]
    assert not bad, ("这些示例图看不出是成品图（文件名里没有 贴纸版/卡纸打印图 等字样），"
                     "按可疑处理：\n  " + "\n  ".join(bad))


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
