#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生图产物「认领」回归测试 —— 锁死 2026-08-30 v35 并行跑图暴露的串图缺陷。

真事，不是假想：
    `providers._aime_generate()` 原来靠「扫描共享 artifacts/ 目录里最新出现的
    文件」认领自己的生成结果。4 张照片并行时，A 的调用认领到 B 刚落地的图 ——
    run_v35/p5/round1.png 与 p6/round2.png 字节完全相同（md5 4eb4dc22…），
    p5 的质检报告里出现了银杏元素。交付文件串图 = 数据正确性问题。

这里不调用任何真实生图接口，也不花钱：用一个假的 image_edit.py 模拟
「落地行为」（含并发同名落地、一次落多张、无视环境变量落到共享目录、
什么都没产出四种情况），然后并发调用 provider 验证认领结果。

    python3 -m pytest tests/ -v
    python3 tests/test_provider_claim.py        # 不装 pytest 也能跑
"""
import os
import shutil
import sys
import tempfile
import threading

_FORGE_DIR = os.environ.get(
    "FORGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "memory-sticker-forge"))
sys.path.insert(0, _FORGE_DIR)
import providers  # noqa: E402

# 内部运行版才有 aime provider（公开副本里没有内部实现）。
# 没有它时这几项自动跳过，由下面的 cmd provider 用例覆盖公开副本。
HAS_AIME = hasattr(providers, "_aime_generate")
HAS_CMD = "cmd" in getattr(providers, "_GEN", {})

# 假的 image_edit.py。行为由环境变量控制，模拟 provider 的落地机制。
FAKE_SCRIPT = r'''
import os, sys
mode = os.environ.get("FAKE_MODE", "private")
prompt = sys.argv[sys.argv.index("--prompt") + 1]
stage = os.environ.get("AIME_WORKSPACE_PATH", "")
shared = os.environ.get("FAKE_SHARED_ART", "")

def put(d, name, body):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(body.encode())
    return p

# 故意所有调用都用同一个文件名：真实的 image_edit.py 用 edited_image_<时分秒>.png，
# 并发时同一秒内本来就会互相覆盖。
NAME = "edited_image_120000.png"

if mode == "private":                      # 正常：落在本次调用的私有目录
    p = put(os.path.join(stage, "artifacts"), NAME, prompt)
    print("编辑后的图片已保存到: %s" % p)
elif mode == "multi":                      # 一次落了两张，无法唯一确定
    for n in ("edited_image_120000_1_1.png", "edited_image_120000_1_2.png"):
        print("编辑后的图片已保存到: %s" % put(os.path.join(stage, "artifacts"), n, prompt))
elif mode == "shared":                     # 无视环境变量，落到共享 artifacts/
    p = put(shared, "mine_%s.png" % prompt, prompt)
    put(shared, "foreign_of_another_call.png", "OTHER-CALL-IMAGE")   # 并发的别人
    print("编辑后的图片已保存到: %s" % p)
elif mode == "shared_only_foreign":        # 只有别人的图落进来，自己什么都没产出
    put(shared, "foreign_of_another_call.png", "OTHER-CALL-IMAGE")
    print("图片编辑失败")
else:                                      # nothing：什么都没产出
    print("图片编辑失败")
'''


class FakeAime(object):
    """搭一个假的 Aime 根目录：inner_skills/image-generate/script/image_edit.py"""

    def __init__(self, mode):
        self.mode = mode
        self.root = tempfile.mkdtemp(prefix="fake_aime_")
        script_dir = os.path.join(self.root, "inner_skills", "image-generate", "script")
        os.makedirs(script_dir)
        with open(os.path.join(script_dir, "image_edit.py"), "w", encoding="utf-8") as f:
            f.write(FAKE_SCRIPT)
        self.shared_art = os.path.join(self.root, "artifacts")
        os.makedirs(self.shared_art)
        self.photo = os.path.join(self.root, "src.jpg")
        with open(self.photo, "wb") as f:
            f.write(b"not-a-real-jpeg")
        self.outdir = os.path.join(self.root, "out")
        os.makedirs(self.outdir)

    def __enter__(self):
        self._orig_root = providers._aime_root
        self._orig_env = {k: os.environ.get(k) for k in ("FAKE_MODE", "FAKE_SHARED_ART")}
        providers._aime_root = lambda: self.root
        os.environ["FAKE_MODE"] = self.mode
        os.environ["FAKE_SHARED_ART"] = self.shared_art
        return self

    def __exit__(self, *_exc):
        providers._aime_root = self._orig_root
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    def out(self, tag):
        return os.path.join(self.outdir, "round%s.png" % tag)


def _read(p):
    with open(p, "rb") as f:
        return f.read().decode()


# ── 1. 并发不得串图（核心回归）──────────────────────────────────────────────

def test_parallel_calls_never_steal_each_others_image():
    """8 个调用并发，每个都必须拿到写着自己 prompt 的那张图。

    这是 v35 的原始故障场景：并行 4 张时 p5 拿到了 p6 的图。
    假 provider 里所有调用落地用的是【同一个文件名】，所以只要还靠
    「共享目录里最新出现的文件」认领，这个测试必然失败。
    """
    if not HAS_AIME:
        return
    with FakeAime("private") as fake:
        results, errors = {}, []

        def work(i):
            try:
                dst = providers._aime_generate(fake.photo, "PROMPT-%d" % i, fake.out(i))
                results[i] = _read(dst)
            except BaseException as e:      # noqa: BLE001 - 测试里要看到任何异常
                errors.append("%d: %r" % (i, e))

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)

        assert not errors, "并发调用出现异常：%s" % errors
        assert len(results) == 8, "只有 %d 个调用拿到结果" % len(results)
        for i in range(8):
            assert results[i] == "PROMPT-%d" % i, \
                "第 %d 个调用认领到了别人的图（拿到 %r）—— 串图缺陷复发" % (i, results[i])
        assert len(set(results.values())) == 8, "出现内容完全相同的产物：串图"


# ── 2. 无法唯一确定时必须报错，不许猜 ─────────────────────────────────────

def test_ambiguous_claim_raises_instead_of_guessing():
    """候选多于 1 个（并发干扰或一次返回多张）时必须抛错，绝不随便挑一个。"""
    if not HAS_AIME:
        return
    with FakeAime("multi") as fake:
        try:
            providers._aime_generate(fake.photo, "P", fake.out(1))
        except providers.ProviderError as e:
            assert "唯一" in str(e) or "候选" in str(e), "报错信息没说清原因：%s" % e
        else:
            raise AssertionError("两个候选却认领成功了 —— 又在猜")
        assert not os.path.exists(fake.out(1)), "认领失败却留下了产物文件"


def test_no_output_raises():
    """什么都没产出时要明确报失败，而不是把上一轮的旧文件当成本轮产物。"""
    if not HAS_AIME:
        return
    with FakeAime("nothing") as fake:
        # 先放一个上一轮留下的同名旧文件：不能被当成本次的产物
        with open(fake.out(1), "wb") as f:
            f.write(b"STALE-FROM-LAST-ROUND")
        try:
            providers._aime_generate(fake.photo, "P", fake.out(1))
        except providers.ProviderError as e:
            assert "失败" in str(e) or "没有产出" in str(e), e
        else:
            raise AssertionError("没有任何产物却认领成功了")
        assert _read(fake.out(1)) == "STALE-FROM-LAST-ROUND", \
            "旧文件被改写了，说明认领逻辑动过它"


# ── 3. 退化到共享目录时，只认自己 stdout 打印过的那一张 ────────────────────

def test_shared_dir_fallback_only_trusts_own_stdout():
    """
    万一 image-generate 改实现、不再认 AIME_WORKSPACE_PATH，图仍会落到共享
    artifacts/。此时目录差集里会同时有别人的图，但自己的 stdout 里不会有 ——
    必须只认自己打印过的那一张。
    """
    if not HAS_AIME:
        return
    with FakeAime("shared") as fake:
        dst = providers._aime_generate(fake.photo, "MINE", fake.out(1))
        assert _read(dst) == "MINE", "退化路径认领到了并发调用的图：%r" % _read(dst)


def test_shared_dir_fallback_refuses_foreign_only():
    """共享目录只多出了别人的图、自己什么都没产出时，必须报错而不是认领它。"""
    if not HAS_AIME:
        return
    with FakeAime("shared_only_foreign") as fake:
        try:
            providers._aime_generate(fake.photo, "MINE", fake.out(1))
        except providers.ProviderError as e:
            assert "并发" in str(e) or "没有产出" in str(e), e
        else:
            raise AssertionError("认领了并发调用落下的图 —— 这正是 v35 的串图根因")


# ── 4. 公开副本：外部命令 provider 的并发认领 ──────────────────────────────

def test_cmd_provider_parallel_calls_are_isolated():
    """
    公开副本的 cmd provider 把产物直接写到调用方指定的 {out}，天然一调用一文件。
    这里并发跑一遍，锁住「不得改成扫目录认领」这个约束。
    """
    if not HAS_CMD:
        return
    d = tempfile.mkdtemp(prefix="cmd_prov_")
    orig = os.environ.get("FORGE_GEN_CMD")
    os.environ["FORGE_GEN_CMD"] = "printf %s {prompt} > {out}"
    try:
        photo = os.path.join(d, "src.jpg")
        with open(photo, "wb") as f:
            f.write(b"x")
        results, errors = {}, []

        def work(i):
            try:
                out = os.path.join(d, "round%d.png" % i)
                providers._cmd_generate(photo, "PROMPT-%d" % i, out)
                results[i] = _read(out)
            except BaseException as e:      # noqa: BLE001
                errors.append("%d: %r" % (i, e))

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        assert not errors, errors
        for i in range(8):
            assert results[i] == "PROMPT-%d" % i, "cmd provider 串图：%r" % results[i]
    finally:
        if orig is None:
            os.environ.pop("FORGE_GEN_CMD", None)
        else:
            os.environ["FORGE_GEN_CMD"] = orig
        shutil.rmtree(d, ignore_errors=True)


def test_stale_output_file_is_not_delivered_as_this_calls_product():
    """
    通用约束（两份副本都要成立）：generate_image 之前必须清掉同名旧文件。
    否则生成失败时，上一次跑留下的 round1.png 会被当成本次产物交付 ——
    和「认领别人的图」是同一类缺陷。
    """
    d = tempfile.mkdtemp(prefix="stale_")
    try:
        out = os.path.join(d, "round1.png")
        with open(out, "wb") as f:
            f.write(b"STALE-FROM-LAST-RUN")
        photo = os.path.join(d, "src.jpg")
        with open(photo, "wb") as f:
            f.write(b"x")

        def boom(*_a, **_k):
            raise providers.ProviderError("模拟生图失败")

        providers._GEN["__test__"] = boom
        orig_env = os.environ.get("FORGE_IMAGE_PROVIDER")
        os.environ["FORGE_IMAGE_PROVIDER"] = "__test__"
        try:
            try:
                providers.generate_image(photo, "P", out)
            except providers.ProviderError:
                pass
            else:
                raise AssertionError("生图失败却没抛错")
            assert not os.path.exists(out), \
                "生图失败后旧文件还在，会被当成本次产物交付"
        finally:
            providers._GEN.pop("__test__", None)
            if orig_env is None:
                os.environ.pop("FORGE_IMAGE_PROVIDER", None)
            else:
                os.environ["FORGE_IMAGE_PROVIDER"] = orig_env
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
