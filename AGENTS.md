# AGENTS.md

本文件供 AI Agent 接手本项目时阅读。第 0 节由接手方按实际情况维护，第 2 节为固定协作纪律。

---

## 0. 项目信息

### 0.1 这个项目是做什么的

把一张现场照片，变成能直接送厂印刷模切的 A5 手账贴纸版。

**最常见的任务就是这一条**：用户给一张照片，要拿到 **SVG（含刀线）+ 原图 PNG** 去找数码模切店。对应命令见 0.4。

### 0.2 环境与安装依赖

| 项 | 值 |
|---|---|
| 语言 | Python 3（开发环境 3.9+） |
| 包管理 | pip |
| 依赖清单 | `print-ready-doctor/requirements.txt` |
| `package.json` | 未找到（本项目非 Node 项目） |
| `Makefile` | 未找到 |
| CI 配置 | 未找到 |

```bash
pip install -r print-ready-doctor/requirements.txt
```

依赖：`numpy` `opencv-python-headless` `scikit-image` `scipy` `Pillow` `openai`
系统依赖：`cairosvg`（导出 PDF 用）需要 libcairo；缺失时 `export_for_digital_cut.py` 会报错，其余功能不受影响。

### 0.3 配置生图后端（跑之前必须做）

本项目**不自带模型**，需要接一家生图 + 视觉服务。二选一：

```bash
# 方式 A：OpenAI（默认）
export FORGE_PROVIDER=openai
export OPENAI_API_KEY=sk-xxx
export OPENAI_IMAGE_MODEL=gpt-image-2
export OPENAI_IMAGE_SIZE=a5-300

# 方式 B：火山方舟
export FORGE_PROVIDER=volcengine
export ARK_API_KEY=xxx

# 方式 C：接你自己的命令行工具
export FORGE_PROVIDER=cmd
export FORGE_GEN_CMD='mytool img2img --src {photo} --prompt {prompt} --out {out}'
export FORGE_VISION_CMD='mytool vision --images {paths} --ask {task}'
```

配好后先自检，**这一步不花生图的钱**：

```bash
python3 memory-sticker-forge/tools/selftest_provider.py          # 干检查配置
python3 memory-sticker-forge/tools/selftest_provider.py --all    # 实测调用
```

退出码：`0` 通过 / `1` 配置缺失 / `2` 调用失败 / `3` 输出不达标。

### 0.4 本地运行

```bash
# 【主线】一张照片 → A5 贴纸版（这是用户最常要的）
cd memory-sticker-forge
python3 forge.py /path/to/photo.jpg --outdir out/order1

# 只做照片体检，不出图、不花钱
python3 forge.py /path/to/photo.jpg --preflight-only

# 导出交付给数码模切店的文件
python3 ../print-ready-doctor/export_for_digital_cut.py out/order1/FINAL.png --outdir 交付/

# 把 SVG 里的外部位图内嵌进去（单独发厂不丢图，发厂前必做）
python3 ../print-ready-doctor/embed_svg.py out/order1/production/cutline.svg -o 交付/刀线_含图.svg

# 凑满 4 单拼 A3（一张 A3 = 4 张 A5，印厂更省钱）
python3 ../print-ready-doctor/impose_a3.py 单1/FINAL.png 单2/FINAL.png 单3/FINAL.png 单4/FINAL.png --outdir a3_out/
```

**交付给用户 SVG + 原图时，必须给这两个文件：**
- `交付/刀线_含图.svg` — 位图已 base64 内嵌，单独发给店家不会丢图
- `out/order1/FINAL.png` — A5 印刷图，400dpi

### 0.5 运行测试

```bash
python3 tests/test_select_objects.py        # 无需 pytest
python3 -m pytest tests/ -v                 # 有 pytest 时
```

测试覆盖三个真实出现过的缺陷（纪念物被截断、容器重复、同族重复），**不调用任何 AI 接口、不产生费用、可离线运行**。当前 11 项全绿。改动 `select_objects()` / `_family()` / `build_prompt()` 后必须重跑。

### 0.6 代码检查

未找到 lint / formatter 配置（无 ruff / flake8 / black 配置文件）。当前基线检查：

```bash
python3 -m py_compile memory-sticker-forge/*.py print-ready-doctor/*.py
```

### 0.7 不得触碰的目录与文件

| 路径 | 原因 |
|---|---|
| `run_*/` `forge_out/` `out*/` `a3_*/` `probe_out*/` | 运行产物，已 gitignore，体积极大（历史上超过 1GB），**不要提交** |
| `artifacts/` | 生图中间产物，同上 |
| `*.jpg` `*.jpeg` `交付_*/` | **客户原始照片与成品，含个人隐私，严禁提交到仓库** |
| `examples/` | 已脱敏的示例图，可读不要删 |
| `CHANGELOG.md` | **只增不改**，新记录加在最上方，不要重写历史条目 |

### 0.8 改动前必读的约束

这些是踩坑换来的，改之前先看 `CHANGELOG.md` 里的根因：

- **排版不要交给模型**。间距由 `relayout.py` 用代码硬保证（8mm 邻距 / 10mm 留白 / 400dpi），不依赖模型服从性。
- **选品不要退回 `objs[:n]`**。必须走 `select_objects()` 四级过滤，否则纪念物会被截断、容器会重复、同族会撞车。
- **同族判定必须用中心词**，不能用子串匹配（会把 `microphone` 判成 phone 族）。
- **发厂的 SVG 必须先内嵌位图**，否则店家单独打开只有刀线没有图。
- **导出 PDF 前也必须先内嵌位图**，否则 `整版_图加刀线.pdf` 会只有 3KB、没有图像。

### 0.9 关键规格（改动前先确认影响面）

| 项 | 值 |
|---|---|
| 成品 | A5 148×210mm，400dpi = 2331×3307px |
| 生图最小短边 | 1748px（A5@300dpi 底线，低于此直接报错） |
| 每版枚数 | 6 枚 = 5 物品 + 1 人物 |
| 单枚尺寸 | 22~62mm |
| 元素最小净距 | 8mm；四边留白 10mm |
| 刀线 | 图层 `CutContour`，#FF00FF，0.25pt，半切 kiss cut |

---

## 1. 上手顺序

1. 读 `README.md` —— 了解产线的六个设计要点
2. 读 `CONTEXT.md` —— 当前进度、已知问题、待办
3. 读 `CHANGELOG.md` 最近两条 —— 知道最近改了什么、为什么
4. 跑 `python3 tests/test_select_objects.py` —— 确认基线是绿的
5. 再动手

---

## 2. 提问纪律

提问消耗用户注意力，是有成本的行为。默认不问。

- **先动手，再开口。** 先自己调研、读代码、推理、尝试。穷尽能力后仍存在方向性歧义，才可提问。

- **三条件自检**（必须同时满足才能问）：

  ① 两个选项导致完全不同的产出；

  ② 你无法自行验证哪个正确；

  ③ 选错的返工代价大。

  任一不满足 → 自己决定，一句话说明你的假设，继续执行。

- **要问一次问完。** 禁止连环追问。用户回答一次后，剩余细节自己判断。

- **不许提空问题。**「你希望怎么设计」「还有什么边界」「要不要写测试」这类把分析工作甩回给用户的问题一律禁止。

- **必须带推荐答案来问。** 合格的提问必须包含：项目里的事实依据 + 两个都说得通的选项 + 各自代价 + 你的推荐 + 这个选择会如何改变验收标准。

- **有明显更优方案时直接执行并告知依据**，不要包装成开放式问题让用户选。

### 2.4 替代动作：写假设，不要问

所有自行决定的取舍，记进 `ASSUMPTIONS.md`（或交付报告的「我做的假设」段），标注：假设内容 / 为什么这么选 / 如果选错影响多大 / 改回来的成本。

用户看这份清单来纠偏，而不是被逐个打断。
