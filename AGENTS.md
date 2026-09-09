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

依赖：`numpy` `opencv-python-headless` `scikit-image` `scipy` `Pillow` `openai`（与 `requirements.txt` 一致）
系统依赖：`cairosvg`（导出 PDF 用）**不在 requirements.txt 里**，需另装且需要 libcairo；缺失时只有 `export_for_digital_cut.py` 的 PDF 环节报错，其余功能不受影响。

### 0.3 配置生图后端（跑之前必须做）

本项目**不自带模型**，需要接一家生图 + 视觉服务。三选一：

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

**断点续跑（省钱关键，别手动重来）**：`forge.py` 在 `--outdir` 下写 `resume_state.json`。中途失败后**重跑同一条命令即可续跑** —— G0 与已过质检的轮次会复用（日志打 `♻️`），不重复调视觉模型、不重复生图。要从头再跑一次才加 `--fresh`。默认 `--elements 6 --max-rounds 4 --gap 8 --margin 10 --dpi 400 --figure-style collage`；轮数不够时加大 `--max-rounds` 再跑一次，前面的轮次不会白花钱。

**卡纸打印图链路（非主线，客户要卡纸才用）**：

```bash
python3 forge_scene.py /path/to/photo.jpg --outdir out/order1        # 主视觉场景图
python3 ../print-ready-doctor/make_memory_card.py --scene 场景图.png --stickers FINAL.png --outdir 卡纸/
```

**交付给用户 SVG + 原图时，必须给这两个文件：**
- `交付/刀线_含图.svg` — 位图已 base64 内嵌，单独发给店家不会丢图
- `out/order1/FINAL.png` — A5 印刷图，400dpi

### 0.5 运行测试

```bash
python3 -m pytest tests/ -q                 # 全量，约 6 秒
python3 tests/test_select_objects.py        # 单文件，不装 pytest 也能跑（每个文件都可以）
```

**基线：128 项收集 = 124 通过 + 4 跳过，0 失败。** 那 4 项跳过是正常的：交付打包脚本 `make_delivery_v35.py` 只存在于运行版，不在本副本内。跑出来不是这个数就是有人改坏了东西。

`tests/` 下 11 个文件，都**不调用任何 AI 接口、不产生费用、可离线运行**：

| 文件 | 项数 | 守的是什么 |
|---|---|---|
| `test_g0_retry_and_resume.py` | 27 | G0 截断 JSON 有限重试、断点续跑不拿旧图冒充新图 |
| `test_select_objects.py` | 24 | 选品四级过滤（纪念物被截断 / 容器重复 / 同族重复） |
| `test_provider_capability.py` | 13 | 像素全部公式推导、**禁止写死魔数**、300dpi 显式降级 |
| `test_photo_io_and_counts.py` | 12 | EXIF 方向、总枚数硬门禁 |
| `test_model_output_shapes.py` | 10 | 模型返回形状归一（dict / list / 垃圾数据都不许崩） |
| `test_ip_policy.py` | 10 | IP 三级边界 + 随身物优先级（见 0.10） |
| `test_convergence.py` | 9 | `ConvergenceGuard` 保证失败可收敛 |
| `test_provider_claim.py` | 7 | 生图产物认领不串图 |
| `test_dpi_and_delivery.py` | 7 | dpi 推导与交付产物（4 项跳过在这里） |
| `test_card_element_numbering.py` | 5 | 卡纸元素编号行带数不写死 |
| `test_privacy_gitignore.py` | 4 | **实测** `git check-ignore`，客户照片不许进仓库 |

**改动下列任一处，必须重跑全量 pytest：**

- 选品与同族：`select_objects()` / `_family()` / `_rank_of()` / `candidate_pool()` / `_decompose()`
- IP 判定：`_hard_banned()` / `_modern_landmark()` / `_ancient_arch()` / `_carry_item()` 及 `HARD_BAN_*` / `MODERN_LANDMARK_*` / `ANCIENT_ARCH_*` / `CARRY_*` 词表
- prompt 组装：`build_prompt()` / `build_scene_prompt()` / `IP_POLICY` / `SCENE_LANDMARK_OVERRIDE`
- 收敛：`ConvergenceGuard` / `fail_tags()` / `objects_in_fail()`
- 形状归一：`_strlist()` / `_pairs_from()` / `_g0_normalize()`
- 分辨率与 provider：`providers.py` 里 `px_at()` / `sheet_px()` / `min_short_edge_px()` / `dpi_plan()` / `generate_image()`
- G0 与续跑：`preflight()` / `_g0_ask_with_retry()` / `prompt_sig()` / `round_cache()`
- 改 `.gitignore`（隐私测试是实测的，会跟着变红）

### 0.6 代码检查

未找到 lint / formatter 配置（无 ruff / flake8 / black 配置文件）。当前基线检查（注意 `*.py` 通配符不会递归，`tools/` 和 `tests/` 要单独列）：

```bash
python3 -m py_compile memory-sticker-forge/*.py memory-sticker-forge/tools/*.py \
                      print-ready-doctor/*.py tests/*.py
```

### 0.7 不得触碰的目录与文件

| 路径 | 原因 |
|---|---|
| `run_*/` `forge_out/` `out*/` `a3_*/` `probe_out*/` `pf_*` `production/` `_work/` | 运行产物，已 gitignore，体积极大（历史上超过 1GB），**不要提交** |
| `artifacts/` | 生图中间产物，同上 |
| 所有位图 / 视频（`jpg` `jpeg` `png` `heic` `heif` `webp` `bmp` `gif` `tif` `dng` `raw` `mp4` `mov` …）、`交付_*/` | **客户原始照片与成品，含个人隐私，严禁提交到仓库** |
| `examples/` | 已脱敏的示例图，可读不要删 |
| `CHANGELOG.md` | **只增不改**，新记录加在最上方，不要重写历史条目 |

**`.gitignore` 的隐私规则已收紧，改它之前先看文件头注释**（两个洞都是用 `git check-ignore` 实测出来的，不是肉眼看出来的）：

- 原先只忽略 `*.jpg` / `*.jpeg`，**漏了 `*.png`** —— 客户照片存成 PNG 放在 `run_*/` 之外就会被正常跟踪并推到公开仓库。
- 忽略规则**大小写敏感**：手机导出的是 `IMG_1234.JPG` / `.HEIC`，Linux 上 `*.jpg` 匹配不到。现在统一写成 `*.[jJ][pP][gG]` 这种**字符组**形式覆盖全部大小写组合，不要手工枚举 `.JPG/.Jpg`。
- 做法是「图片视频**全部先忽略**，再用 `examples/` 白名单放行」，且白名单按**文件名前缀**（`示例*` / `example_*`）而不是扩展名 —— 往 `examples/` 里随手拖一张 `IMG_xxxx.jpg` 不会自动变成可提交状态，必须显式改名，改名这个动作本身就是一次「我确认已脱敏」的人工确认。
- `tests/test_privacy_gitignore.py` 会**实测**这些规则（含「白名单外没有被跟踪的媒体文件」），改完必须重跑。

### 0.8 改动前必读的约束

这些是踩坑换来的，改之前先看 `CHANGELOG.md` 里的根因：

- **排版不要交给模型**。间距由 `relayout.py` 用代码硬保证（8mm 邻距 / 10mm 留白 / 400dpi），不依赖模型服从性。
- **选品不要退回 `objs[:n]`**。必须走 `select_objects()` 四级过滤，否则纪念物会被截断、容器会重复、同族会撞车。
- **同族判定必须用中心词**，不能用子串匹配（会把 `microphone` 判成 phone 族）。
- **收敛保护不可绕过**。`ConvergenceGuard` 负责保证「不会 4 轮都卡在同一组失败元素」：同一物体连续 2 轮失败、或一轮内同时命中互斥规则，就永久剔除换候选。配套铁律：`select_objects()` 数量不足时的**兜底回填也必须过同族检查**（现在分三档递进：非容器不同族 → 非容器允许同族且日志明确告警 → 才动容器项）。旧实现回填时只排除容器、不查同族，把刚剔掉的同族件原样放回来，日志打了「已剔除 tree trunk」最终却选中 ginkgo leaf + ginkgo tree + tree trunk 三枚树体部件，去重白做（v36 银杏踩过）。
- **provider 产物认领必须自证**。**不许靠「扫目录取最新文件」认领生成结果** —— 并发时会互相偷图，实测两张交付图 md5 完全相同、质检报告串了元素。公开副本三个 provider 都直接写调用方给的 `{out}`，一调用一文件；`generate_image()` 每次尝试前先删同名旧文件，生图失败时不可能把上一轮的 `round1.png` 当本次产物交付。候选不唯一（一次落多张 / 并发干扰）时**抛错而不是猜**。`tests/test_provider_claim.py` 用 8 线程并发锁这条。
- **模型返回的形状不可信**。G0 与质检的 JSON 可能截断、同一字段可能是 dict 也可能是 list 也可能是 `"a → b"` 字符串。已有 `_strlist()` / `_pairs_from()` 在 G0 出口和质检入口做形状归一，原则是「**格式抖动只允许降级成漏判，不允许崩产线**」。代价是可能静默丢掉解析不了的字段，排查时要看原始返回。
- **发厂的 SVG 必须先内嵌位图**，否则店家单独打开只有刀线没有图。
- **导出 PDF 前也必须先内嵌位图**，否则 `整版_图加刀线.pdf` 会只有 3KB、没有图像。

### 0.9 关键规格（改动前先确认影响面）

| 项 | 值 |
|---|---|
| 成品 | A5 148×210mm，400dpi = 2331×3307px |
| 生图最小短边 | **1739px**（= 300dpi 理论值 1748px × (1−0.5% 量化容差)，低于此 `providers.py` 直接报错） |
| 每版枚数 | 6 枚 = 5 物品 + 1 人物 |
| 单枚尺寸 | 22~62mm |
| 元素最小净距 | 8mm；四边留白 10mm |
| 刀线 | 图层 `CutContour`，#FF00FF，0.25pt，半切 kiss cut |

**所有印刷像素都是公式算出来的，源码里禁止再出现写死的魔数。** 唯一来源是 `providers.py` 的 `px_at(mm, dpi) = round(mm ÷ 25.4 × dpi)`，`MIN_SHORT_EDGE_PX = min_short_edge_px()`，容差是显式常量 `SHORT_EDGE_TOL = 0.005`。

为什么不写死：

- 写死 1748 时，改成品尺寸（比如出 A6 档）必须手改常量，**改漏就静默降质**；门禁与目标 dpi 还剩多少余量也藏在注释里，看不出来。
- 0.5% 容差是为了吸收厂商**尺寸量化**（gpt-image-2 要求宽高是 16 的倍数，火山只给固定档位）：门禁从 1748 降到 1739px（等效 298.5dpi，印刷与模切都感知不到），把 gpt-image-2 `a5-300` 档（短边 1760px）的余量从 12px 抬到 21px，任何一方微调默认尺寸不会立刻停摆。
- 容差**不是用来放行低分辨率模型**的：真低分辨率模型差 30% 以上，`dpi_plan()` 会直接判不达标。
- `tests/test_provider_capability.py` 有守护测试：`MIN_SHORT_EDGE_PX = 1748` / `= 1739` / `TARGET_W_PX = 2331` 这类写死赋值一出现就红，同时锁住「余量必须 ≥20px」。**不要为了让测试过而把数字填回去。**

### 0.10 IP 合规策略（最近最重要的产品规则，出事就是法律风险）

三级边界，`forge.py` 里落地：

- 🔴 **硬禁止（一枚都不出）**：主题乐园元素（迪士尼 / 环球 / 城堡 / 设施品牌）、景区吉祥物、景区文创商品设计、商标字标 logo / 品牌名 / 赞助牌，以及卡通角色 / 玩偶 / 手办 / 盲盒形象。
- 🔴 **硬禁止：现代地标建筑本体** —— 体育场馆、摩天楼、电视塔 / 观光塔、会展中心、歌剧院、商场、城市天际线。**理由：这些多有在世建筑师署名，属受著作权保护的建筑作品**，不是「拍到就能画」的背景。
- 🟢 **允许**：自然景观（山 / 树 / 湖 / 石）与**古建筑本体**（城墙、古塔 / 塔檐、飞檐、斗拱、石狮、瓦片、牌坊），属公共领域。
- ⭐ **最高优先：「那天你带着的东西」** —— 门票 / 票根、纸质地图、水壶、背包、帽子、食物饮料、落叶、合影等随身物与消耗品。零 IP 风险，且比地标更能唤起当天的记忆。

代码落地：

| 位置 | 做什么 |
|---|---|
| `HARD_BAN_WORDS` / `HARD_BAN_PHRASES` | 🔴 硬禁止词表 → `_hard_banned()` |
| `MODERN_LANDMARK_WORDS` / `MODERN_LANDMARK_PHRASES` | 🔴 现代地标 → `_modern_landmark()` |
| `ANCIENT_ARCH_WORDS` | 🟢 古建放行；命中古建词就**推翻**现代地标判定，所以 `ancient watchtower`、`pagoda tower` 不会被误杀，`national stadium`、`glass tower` 会被拦 |
| `CARRY_WORDS` / `CARRY_PHRASES` | ⭐ 随身物 → `_carry_item()` |
| `select_objects()` → `_rank_of()` | **五档排序**：⭐随身物(0) > 纪念物(1) > 普通物品(2) > 古建筑本体(3) > 文字依赖件(4)。古建可以画但排在普通物品之后；现代地标根本走不到排序，入池阶段就被硬剔除 |

**三层拦截，缺一层都会漏：**

1. **G0 体检**：让视觉模型自己填 `ip_items` / `modern_landmark_items` / `carried_items`（**模型判断 > 词表**，覆盖词库没有的新品类）。
2. **选品**：`select_objects()` 入池时按词表 + G0 标注硬剔除，并写进 `dropped` 日志说明为什么没有它。
3. **生图后复检**：目视质检问 `banned_ip_or_landmark`，漏网的**当轮判废**，被判废的物体交给 `ConvergenceGuard` 永久剔除换候选，不原地重试。

**⚠️ 场景图链路另有一条，别删也别挪位置：** 卡纸的主视觉场景图（`build_scene_prompt()`）没有选品层，只能靠 prompt，而 `SCENE_OUTPUT` 里写着「keep the setting…the same subject in the same place」，和地标禁令直接打架。**prompt 里后出现的段落赢** —— v36 的 05 鸟巢就是因为 `IP_POLICY` 排在 `SCENE_OUTPUT` 前面，把受著作权保护的钢结构外立面原样画了出来。所以 `SCENE_LANDMARK_OVERRIDE` **必须拼在 prompt 最末尾**（`SCENE_OUTPUT` / `STYLE_REMINDER` 之后），并点名替换方案（用普通树线或空天填掉建筑的位置），不留解释空间。改 `build_scene_prompt()` 的拼接顺序前先想清楚这一条。

词表只是先验，不是判据全部：遇到新品类（乐高、手办、宠物用品）照旧会漏判，兜底靠上面第 1、3 层。

---

## 1. 上手顺序

1. 读 `README.md` —— 了解产线的六个设计要点
2. 读 `CONTEXT.md` —— 当前进度、已知问题、待办（**当前版本 v3.4.1**）
3. 读 `CHANGELOG.md` 最近两条 —— 知道最近改了什么、为什么
4. 读本文件 0.8（约束）+ 0.10（IP 红线）—— 这两节是踩坑换来的，比代码好读
5. 跑 `python3 -m pytest tests/ -q` —— 确认基线是 **124 passed, 4 skipped**
6. 再动手

配了生图后端的话，动手前再跑一次免费自检：`python3 memory-sticker-forge/tools/selftest_provider.py`。

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
