# 变更日志 · 现场记忆手账

> 每次完成开发任务后追加记录。**只增不改，最新记录在最上方。**

---

## 2026-09-01 · 缺陷 A/B/C 三修 + 06 银杏 v37 重跑交付 · v3.4.1

修 v36 暴露的 3 个缺陷，并重跑 06 银杏 → `run_v37/p6/`、`交付_v37_20260901/06_银杏_v37厂家文件/`（`run_v36/` 未覆盖）。`tests/` 101 → **128 项全绿**（新增 `tests/test_g0_retry_and_resume.py` 27 项）。

**缺陷 A（已修）· G0 返回截断 JSON 时整单直接崩**

`preflight()` 拆成 `_g0_ask_with_retry()` + `g0_parse_problem()` + `_g0_normalize()`，对**可恢复错误**做**有限 3 次**重试：空返回 / 非 JSON / 花括号未闭合（v36 02 就是在 `"mode` 处断的）/ 顶层不是对象 / `standalone_objects` 空且没给拒单理由。重试时在任务描述后追加 `G0_JSON_ONLY_HINT`（只输出闭合的 JSON、字段值尽量短、不要解释文字），**不改判定标准、不放宽字段**。3 次仍失败才退出，报错明确写「G0 阶段失败」并带最后一次的解析问题。不可恢复错误（模型明确拒单等）不重试。

**缺陷 B（已修）· 无断点续跑 + provider 400 整单重来**

- 断点续跑：新增 `resume_state.json` + `prompt_sig()`（prompt 的 sha256 前 16 位）。`resolve_preflight()` 校验 `preflight.json` 有效就复用，**不再调视觉模型**；每轮 QC 结论一拿到就 `record_round()` 落盘，重跑时 `round_cache()` 只在「同一轮号 + prompt 哈希一致 + 产物文件都在 + QC 数据齐」四条同时成立时复用，否则重生成 —— 换了选品必然改 prompt，哈希跟着变，不会拿旧图冒充新图。
- `--fresh` / `--no-resume` 强制从 G0 重跑。
- 复用必须可见：G0 与每一轮都打「♻️ 复用…本次未调用视觉模型 / 未重复生图」，结尾再打一次「【续跑汇总】」列出全部复用项，避免误以为是新跑的。
- provider 400 退避重试：`providers.generate_image()` 加 `GEN_MAX_ATTEMPTS=3` + `GEN_BACKOFF_SEC=(2,4,8)`。`_is_transient()` 把 `400 Client Error`、连接/超时/5xx 当瞬时错误重试（v36 01+06 共撞 5 次，实测相当比例是瞬时的）；invalid api key / model not found / 内容审核 / 余额不足 / 分辨率门禁不合格属永久错误，**不重试**。HTTP 码用词边界正则匹配，不会把 `1739px`、`2352x3520` 里的数字误判成状态码。每次尝试前先删同名旧产物，失败时不可能交付上一轮的图。
- 生图彻底失败时退出码 4，**断点保留**，日志直接给出「重跑同一条命令即可续跑」。

**缺陷 C（已修）· `SCENE_LANDMARK_OVERRIDE` 之前只同步没推送**

复查确认：该常量在两份 `forge.py` 里都在（`sync_studio.py --check` 一致），但 `b000dde` 里没有它 —— v36 那轮只同步到公开副本工作区，从未 commit/push。本轮随这次提交一并推到 GitHub `main`。

**G0 召回稳定性（06 银杏根因）**

`PREFLIGHT_TASK` 的 `standalone_objects` 要求从「至少 6 个」提到「**至少 10 个、最多 14 个**」，并显式要求跨类别（随身物 / 自然物 / 建筑设施）。新增 `g0_category()` + `g0_recall_report()`，下限 `G0_MIN_OBJECTS=10` / `G0_MIN_CATEGORIES=3`；不达标时 `_g0_fill_recall()` 最多补问 2 次，提示「再补充一些不同类别的物品，尤其是人物随身携带的东西（背包/外套/鞋/手机/水壶/门票…）」，`_g0_merge()` 只做增量合并，**不会挤掉已召回的 `ginkgo leaf`**。补问后仍不足只告警不阻断（有些照片确实物件少）。实测 06 银杏本轮 G0 一次给出 **12 项 / 4 类**（v36 那次只有 6 项且全为树/墙同族）。

**06 银杏重跑结果（v37）**

跑 2 次命令、共 5 轮，**无人工干预**（没用 `--objects`）：首次 `--max-rounds 4` 四轮未过；第二次同一条命令加 `--max-rounds 8`，**断点续跑复用了 G0 + 第 1~4 轮全部产物**（未重复调视觉模型、未重复生图），第 5 轮双重质检通过。最终 6 枚 = `ginkgo leaf / backpack / baseball cap / coat / stone pillar` **5 枚纯物品 + 1 枚人物群像**，**银杏叶在，且是版面主角**；人物为 collage 分色（有肤色/衣服色、无脸），画风与 v36 其他三张一致。中途 `ConvergenceGuard` 因「焦点色占比 >15%」两次换元素（shoes → wall lattice → stone pillar），这正是 v35 就记录过的「银杏固有色单一」老问题，靠换选品绕过，未调阈值。

**印前**：400dpi（2331×3307）/ 6 枚 / 刀线 6 条 / 必须修 0 / 高危 0 / 最小邻距 8.63mm / 最小边距 10.2mm / 最窄笔画 0.81mm / 最窄刀线通道 9.02mm / RGB / layout_ok。内嵌 SVG cairosvg 渲染校验：1 张内嵌位图、非白像素 39.5%、无外链。卡纸图 3307×2331 = 210×148mm@400dpi。

**交付**：`交付_v37_20260901/06_银杏_v37厂家文件/` 共 11 个文件（三份核心文件 + 三个 PDF + 单枚透明 PNG + 模切店须知）；v36 的 06 银杏交付包**已作废，不要再发给模切店**。

**变更文件**：`memory-sticker-forge/forge.py`、`memory-sticker-forge/providers.py`、`tests/test_g0_retry_and_resume.py`（新增 27 项）、`CONTEXT.md`

---

## 2026-09-01 · 红队评审 R1~R5 整改 + IP 合规策略收紧 · v3.4.0

**本轮未跑生图**，全部是代码 + 离线测试。红队 5 条问题按「有道理就修」处理，另落地客户新的 IP 合规策略。`tests/` 48 → **101 项全绿**。

**R2 · 有些订单永远交付不出来（最高优先级）**

红队指出的 drum kit 案例成立：整套架子鼓同时触发「结构过细」（要求加粗保留）和「自带支架」（要求整体去掉）两条**互斥**质检，旧实现只把两条 fail 原样喂回模型再画一次 → 4 轮全败。核心不是继续堆词库，而是让「失败可收敛」成为结构性保证。

- 新增 `ConvergenceGuard`（`forge.py`），每轮质检后调一次，三条规则：
  - **R-a** 同一物体连续 2 轮（`--strikes` 可调）触发失败 → **永久剔除并换下一个候选**，日志打「物体 X 连续 2 轮不收敛（tag），已替换为 Y」
  - **R-b** 同一物体一轮内同时命中互斥规则对（`CONTRADICTORY_TAG_PAIRS`：过细×支架、过细×复合体、缺失×IP、缺失×支架）→ **立刻换，不等第二轮**
  - **R-c** 归因不到任何物体、但同一组元素已连续失败 → 换掉优先级最低那枚，**绝不空转**
- 归因靠 `fail_tags()`（文案 → 规则标签）+ `objects_in_fail()`（文案 → 涉及物体，整名匹配后退回中心词匹配）
- 全局保证：`candidate_pool()` 里只要还有没试过的物体就必须换，池子见底才保持原组重试且日志明说「候选池已见底」
- **通用兜底替代硬编码词库**（词库保留为先验，不再是唯一依据）：
  - 自带支架：prompt 加通用约束 `NO_SUPPORT_RIG` + 质检问模型 `elements_with_support_rig`（「这枚元素是否包含与主体无关的支撑结构」），用模型判断代替词表
  - 复合物体：`COMPOSITE_REPLACE` 词库优先，未命中时用模型返回的 `composite_parts` / `composite_elements`（「是否由多个可独立成立的部件组成，若是给出最有代表性的那个部件」），再退回通用中心词规则（`kit/set/rig/stand/tripod/rack/mount…` → 去掉中心词，lego set → lego）
  - 结构过细：保留原有最细笔画量化检测（本来就是通用的）
- 离线冒烟（假 provider，不花钱）：「校园社团日 + lego set / blind box figure / plush toy / school badge」这种词库完全没覆盖的新品类，第 1 轮撞互斥规则 → 立即换元素 → 第 2 轮交付成功

**R3 · 400dpi 只剩一条路线、余量 12px**

- 魔数清零：`providers.py` 全部尺寸改为公式推导 —— `px_at(mm, dpi) = round(mm ÷ 25.4 × dpi)`，A5 竖版 400dpi = 2331×3307、300dpi = 1748×2480；输出门禁 `MIN_SHORT_EDGE_PX = floor(1748 × (1 − 0.5%)) = 1739px`。0.5% 容差是**量化容差**（gpt-image-2 要求宽高为 16 倍数、火山只给固定档位），把 a5-300 档短边 1760px 的余量从 12px 抬到 21px；它挡不住真正低分辨率的模型（那些差 30% 以上）。`forge.py --dpi` 默认值与 `write_report()`、`make_delivery_v35.py` 的 400 判定全部改引 `providers.TARGET_DPI`
- 能力探测 + 显式降级：`dpi_plan()` 两层判定（能力层 `fits_dpi()` / 配置层 `configured_output_px()`），任一层不够就降到 300dpi 并要求标注；连 300dpi 都不够直接拒跑。**不静默插值假 400dpi** —— 那是唯一一类「所有自动检查全过、实物不能用」的失败
- 启动期自检表：`providers.capability_table()`（也可 `python3 providers.py --capabilities`）打印各 provider 400/300dpi 是否可达、300 档短边余量、当前配置尺寸、实测状态，`forge.py` 每次跑图前打印
- 交付说明与报告不再写死 400dpi：`模切店须知.txt` 用 `{dpi}`，降级时追加「⚠️ 本单为 N dpi（不是 400 dpi），请勿再做插值放大」；`qc_report.md` 记录交付分辨率

**R1 · 两个外部 provider 从未实测**

当前环境确实没有真实 key，无法真调用，只能把风险降到最低：

- 新增 `providers.py --selftest [--offline]`（**不消耗生图额度**）：key 是否存在 + 格式体检（空白/引号/长度/前缀）→ SDK 是否安装 → base_url 只做 DNS+TCP 连通（不发请求）→ models 接口核对目标模型 ID（提供该接口的厂商）→ **请求体本地 schema 校验**（校验对象就是 `_openai_kwargs()` / `_volc_body()` 真正会发出去的那个 dict，不另写近似版）→ dpi 计划。每条失败都给「怎么办」（缺哪个 env、去哪个控制台配）。退出码 0 通过 / 1 配置问题 / 2 链路问题
- **fail-fast**：`forge.py` 在 G0 之前先跑自检，不通过直接退出（未消耗任何生成额度）；`--skip-selftest` 可关
- `tools/selftest_provider.py` 不再自己维护一份 `REQUIRED_ENV`（曾与 providers.py 不一致），不花钱的部分全部委托给 `providers.print_selftest()`
- 实测状态如实写进 `PROVIDER_CAPS[*]["tested"]` 并在能力表里显示：运行版自带的 provider ✅ 已实测（十余次真实出图）；gpt-image-2 / Seedream / Gemini 全部 ⚠️ 未实测。Seedream 模型 ID `doubao-seedream-4-0-250828` 已加注释：日期后缀版本随时可能下线，报 `model not found` 时先去火山方舟控制台抄最新 ID，别改代码

**R4 · 色差没验过**

只做代码侧能做的：`模切店须知.txt` 增「色彩管理（首单必读）」段 —— 交付为 RGB（非 CMYK、不嵌输出 ICC）、请按 sRGB 解释并在 RIP 端转 CMYK（建议相对比色 + 黑点补偿）、**首单务必打样确认**、深色/高饱和暖色/大面积平涂/暖白剪纸边缘最易偏色。同时明写「我们没有做过印刷色差实测」，不在代码里假装解决了色差。

**R5 · 隐私**

只做一件事（政策文本另处理）：`.gitignore` 改为「图片视频先全忽略、再白名单放行 examples/」，并修掉两个**实测**出来的洞 —— ① 原来漏了 `*.png`；② 忽略规则大小写敏感，手机导出的 `IMG_1234.JPG` / `.HEIC` 全部漏网（现统一写 `*.[jJ][pP][gG]` 字符组）。白名单按**文件名前缀**（`示例*` / `example_*`）而不是扩展名放行，往 examples/ 里拖原图不会自动变成可提交状态。新增 `tests/test_privacy_gitignore.py` 用 `git check-ignore` + `git ls-files` **实测**（不看 .gitignore 文本），并检查被跟踪的媒体文件是否都在白名单内。

**IP 合规策略收紧（客户新要求）**

- **硬禁止层**（一枚都不出）：卡通吉祥物 / 玩偶手办盲盒 / 主题乐园元素 / 景区吉祥物与文创设计 / 任何商标字标 logo 品牌名 —— `HARD_BAN_WORDS` + `HARD_BAN_PHRASES` + `_hard_banned()`
- **新增「现代地标建筑本体」降级**：体育场馆 / 摩天楼 / 电视塔观光塔 / 商场 / 天际线等**有在世建筑师署名的建筑作品**受著作权保护，一律剔除（`MODERN_LANDMARK_WORDS`）；古建筑本体（城墙、古塔轮廓、飞檐、石狮）属公共领域，可画（`ANCIENT_ARCH_WORDS`，排序上低于随身物）
- **随身物品提到最高优先级档**：门票票根 / 地图 / 水壶 / 背包 / 帽子 / 食物饮料 / 落叶 / 相机 / 鞋 / 伞…（`CARRY_WORDS`）在 `select_objects()` 排序里高于纪念物档，原三档变四档：随身物 > 纪念物 > 普通 > 古建筑 > 文字依赖件
- 三层落地：① G0 让模型自己填 `ip_items` / `modern_landmark_items` / `carried_items`（模型判断优先于词表）；② 选品层硬剔除（模型标的 + 词库命中的都剔）；③ 生图后视觉质检复检 `banned_ip_or_landmark`（生图模型会自己把地标补进画面）。prompt 层加 `IP_POLICY` 段
- **对下一轮跑图的直接影响：05 鸟巢那张不能再画建筑本体**，会自动改画随身物品与自然元素；已有测试锁死这条

**顺手修掉一个会让订单直接崩的健壮性缺陷（离线冒烟实测踩到）**

`composite_parts` 的 prompt 要求是 `[["A","B"]]`，但视觉模型完全可能回 `{"A":"B"}`，旧实现 `dict + list` 直接 `TypeError`，**抛点在 G0 之后 = 这一单当场崩、交付不出来**。修法是在**边界**统一归一形状：新增 `_strlist()`（`["a","b"]` / `"a, b"` / `[{"name":"a"}]` 全部归一成 `[str]`）与 `_pairs_from()`（dict / 成对列表 / `[{"whole":…,"part":…}]` / `"A -> B"` 全收，解析不了就退化成空，让词库和通用规则接手）；`preflight()` 在 G0 出口处过一遍，`qc_visual()` 在质检入口处过一遍。原则：**格式抖动只允许降级成漏判，不允许崩掉产线**。

**测试**：`tests/` 48 → **101 项全绿**（+53：收敛保证 9、IP 策略 10、provider 能力与 dpi 13、dpi 接线与交付说明 7、模型返回形状 10、隐私实测 4）。全部离线，不调 AI、不花额度。公开副本自测 97 项通过 + 4 项 skip（交付打包脚本不在公开副本内，显式 skip 不假绿）。

**公开副本同步**：运行版用同步脚本（带 `--check`，可进 CI）把改动同步到本副本 —— `forge.py` 逐字节相同，`providers.py` 只替换 provider 实现段（公开副本用 `cmd` 接任意外部命令），`tools/selftest_provider.py` 只替换 provider 名字，写出前会扫一遍，确认本副本不含任何运行方内部实现。

---

## 2026-08-30 · 修并发串图 + 同族回填被绕过，并处理三条存疑项 · v3.3.4

**本轮未跑生图**，全部是代码修复 + 离线回归测试。两个缺陷都是 v35 真实跑图暴露的，按「半年后回头看没有可挑剔的地方」的标准现在就清掉，不留技术债。

**1 · 并发时结果互相偷图（数据正确性，最高优先级）**

现象（在运行版上真实发生）：某个 provider 靠「扫描共享输出目录里最新出现的文件」来认领自己的生成结果。并行跑多张照片时，A 的调用会认领到 B 刚落地的图 —— 实测两张不同照片的产出字节完全相同，其中一张的质检报告里出现了另一张的元素。交付文件串图，属数据正确性问题，不是性能问题。

修法（两道，缺一不可，任何新写的 provider 都应照此办理）：

- **物理隔离**：不要让并发调用共用一个输出目录。每次调用把产物落到一个私有临时目录（或像本仓库的 `openai` / `volcengine` / `cmd` 那样，直接写调用方指定的 `out` 路径），别的调用物理上碰不到。这同时消掉了「产物按时间戳命名、同一秒内并发互相覆盖」的隐患
- **唯一性校验**：候选只能来自「私有目录里的文件」或「本次调用自己 stdout 打印过的路径」∩「共享目录新增文件」。候选不是恰好 1 个（0 个 / 多于 1 个）就抛 `ProviderError`，让上层重试或失败，**绝不挑一个**。并发时别人的图会出现在目录差集里，但绝不会出现在自己的 stdout 里
- **不需要强制串行**：隔离是物理的（各写各的路径），认领是自证的（只认自己的输出），因此没有加进程级锁把生图区间串起来，并行跑图是安全的
- 本轮在本仓库改的是同类缺陷的另一个入口：`generate_image()` 在分发给 provider 前先删掉同名旧文件。否则同一个 outdir 被重跑、而本次生图失败时，上一次留下的 `round1.png` 会被当成本次产物交付

**2 · 同族去重被兜底回填绕过**

现象：`select_objects()` 的同族剔除本身是对的（日志确实打了「已剔除 tree trunk（与已选物品同族）」），但候选不足时的兜底回填分支**没过同族检查**，把刚剔掉的又放回来了 —— 06 银杏首跑最终选中 `ginkgo leaf` + `ginkgo tree` + `tree trunk` 三枚树体部件。

- 兜底回填改成三档递进，每档都过同族检查：① 非容器 + 不同族（正常情况到这里就够）→ ② 非容器 + 允许同族（**仅当所有剩余候选都同族、否则凑不满**）→ ③ 才动容器项
- 枚数硬约束保住：走到第 ② 档时照旧凑满，但每回填一枚都在日志里写明「⚠️ 因候选不足，回填了同族元素 X」，事后能直接定位。`test_always_fills_quota` 仍然绿
- 顺带修 `_family()` 的中心词提取：新增 `COLLECTIVE_HEADS`（pile / heap / stack / cluster / bunch / bundle / clump / bouquet…）。这类集合名词做中心词时不代表物品本身，改为优先用它前面「被数的那个东西」判族 —— `leaf pile` 现在能正确落进树体族（`pile of leaves` 这种写法本来就没问题，中心词已经是 leaves）

**3 · 上一轮列的三条存疑项**

1. **`grab_elements()` 分行排序写死的 6** —— 已抽成参数。`make_memory_card.py` 新增 `DEFAULT_MAX_ELEMS = 6`，`grab_elements(sheet, ppmm, max_elems)` 与 `--max-elements` 联动；行带数取「预期枚数」与「实际检出枚数」的较大值，调用方漏传也不会错乱。元素编号是交付包 `01.png~06.png` 与 `--only/--drop` 的依据，乱了很难发现，所以补了 4 项测试锁住阅读顺序
2. **两套面积阈值（卡纸 `MIN_ELEM_AREA_MM2=120` vs doctor `--min-area 25`）** —— **判定为不该统一，只补注释**。前者是「值得上卡的最小面积」（约 11×11mm，比这更小的碎屑摆到卡纸上只会显脏）；后者是「视为有效元素的最小面积」（印前必须把每一枚真元素都数进去，漏一枚就判「元素数 ≠ 刀线数 → 粘连」）。调成一样两个方向都会出事，已在两侧各写清用途与后果，并加一项测试防止半年后被"顺手对齐"
3. **`board` / `screen` 误命中案板、屏风** —— 已收窄。`board / boards / screen / display / notice / tablet` 从单词级词表里删掉，新增 `TEXT_DEPENDENT_PHRASES` 做按词边界的短语匹配（`display board` / `information board` / `notice board` / `display screen` / `led screen` / `stone tablet` …）。`cutting board`（案板）、`folding screen`（屏风）、`graphics tablet` 不再被当空色块剔除，真正的展板/告示屏照旧降权

**测试**：`tests/` 29 → **48 项全绿**（+19：并发认领 7、同族回填 3、集合名词 2、board/screen 2、卡纸编号与阈值 5）。缺陷 1 用假的 `image_edit.py` 模拟 provider 的落地行为（含并发同名落地、一次落多张、无视环境变量落到共享目录、什么都没产出四种），8 线程并发跑；已验证旧实现在该测试下必然失败。全部离线，不花 token。

两份代码同步：`memory-sticker-forge/forge.py`、`print-ready-doctor/{make_memory_card,relayout,print_ready_doctor}.py` 完全一致，`providers.py` 的差异仅限 provider 实现段（公开副本用 `cmd` 接任意外部命令，不含任何内部实现）。

公开副本说明：`openai` / `volcengine` / `cmd` 三个 provider 都把产物直接写到调用方指定的路径，没有「扫目录认领」这一步，因此不存在缺陷 1 的串图问题；本轮只同步了通用的旧文件清除门禁，并加了一项并发用例把这个约束锁死（防止以后有人改成扫目录）。

---

## 2026-08-30 · 技术债集中清理：EXIF 方向 / 总枚数门禁 / 同族词库 / 空色块选品 · v3.3.3

不加新功能，只修四个已确认缺陷。本轮**未跑生图**，全部是代码修复 + 离线回归测试。

**1 · 读图不做 EXIF 旋转（回归缺陷，最高优先级）**

现象：源照片 EXIF `Orientation=6`（手机竖拍）时，产线按原始像素读图，生成的场景图整幅横躺。所有自动检查（dpi / 邻距 / 枚数 / 刀线）全部通过，只有肉眼能发现 —— 属「自动检查全过、成品不能用」那一类。

- `memory-sticker-forge/forge.py` 新增 `open_photo(path)` = `ImageOps.exif_transpose(Image.open(path))`，作为读图的唯一入口
- `shrink()` 与 `preflight()` 改走 `open_photo`。用户原图的读取全链路只有这两处：`forge_scene.py` / `probe_capacity.py` / `forge_a3.py` 都是调 `forge.shrink` / `forge.preflight`，一处修复即全线生效
- `print-ready-doctor/` 复查后确认**没有**读用户原图的地方（读的都是产线自己生成的 PNG）。但它们的图片路径来自命令行，仍可能被手工喂入手机原图，故 `print_ready_doctor.py` 也提供同名 `open_photo()`（对无 EXIF 的 PNG 是无操作），`relayout.py` / `impose_a3.py` 复用

**2 · 目视质检缺「总枚数」硬门禁**

现象：`qc_visual` 只判 `n_object_only >= n_obj`，模型漏画人物那一枚时总数只有 5，而 5 >= 5 成立 → 判「双重质检全过」并直接交付 5 枚成品。

- 新增 `count_fails(d, n, n_obj, n_ppl)`（独立函数，可离线测），`qc_visual` 调用。三条门禁：总枚数必须严格等于目标；枚数缺失/非整数一律判不合格（未确认不许交付）；照片里有人时含人物元素不能为 0
- 两种合法构成都能过：有人 = n_obj 物品 + 1 人物；无人 = 全物品（纯物品 6 枚不会被误拦）
- 同时修掉：视觉模型返回不可解析 JSON 时，旧代码 `d={}` 会让所有判定静默跳过 → 直接判「全过」。现在判不合格并重试
- 同时修掉：`main()` 里 `n_obj/n_ppl` 未按 `len(picked)` 重算，选品兜底后质检拿到的构成与 prompt 写的不一致

**3 · 同族判定漏判部件-整体关系**

现象：`ginkgo tree` / `tree branch` / `tree trunk` 三枚同时入选，都是同一棵树的部件，扁平剪纸下高度重复。

- `SIMILAR_FAMILIES` 把原来的 `{leaf…}` 与 `{trunk, branch…}` 合并成一个**树体族**（tree/trunk/branch/bough/twig/limb/canopy/crown/foliage/leaf/leaves/cluster/frond）
- 新增两组部件-整体族：**建筑构件**（roof/eave/cornice/gable/rafter/wall/facade/pillar/column/beam/balustrade）、**花**（flower/blossom/bloom/petal/stem/stalk/bud/floret）；新增 door/gate 族
- `canopy` 移入树体族（树冠），遮阳器具族改为 `{tent, umbrella, parasol, awning}`
- `_family()` 新增 `_singular()` 去复数兜底：中心词带 s（`tree branches` / `stone slabs` / `petals`）原先全部漏判
- 分层兜底未动：折叠后候选不足时照旧回填

**4 · 选品会选中「去字后只剩空色块」的物件**

现象：匾额 / 展板 / 告示牌这类东西，主体价值就是那行字；合规要求必须去掉所有可读文字，去完只剩一块纯色空框，单独做贴纸价值极低。

- 新增 `TEXT_DEPENDENT_WORDS` + `_text_dependent()`
- `select_objects()` 排序由两档改三档：**纪念物 → 普通物品 → 低价值空色块件**。只降权不硬删，候选不足时仍可回填（回填时排最后）—— 缺一枚比一枚空框更糟
- 低价值判定**优先于**纪念物判定：G0 有时把匾额标成纪念物，但去字后仍是空框
- `banner` 从 `KEEPSAKE_WORDS` 移入 `TEXT_DEPENDENT_WORDS`
- 被降权且没入选的物件写进 `dropped` 日志，复盘时能看到原因

**顺手修掉的隐患**

- `print_ready_doctor.py`：分割出 0 枚元素时后续 `min(...)` 抛 `ValueError: min() arg is an empty sequence`，看不出根因 → 提前判空 + 明确报错 + 退出码 2
- `make_memory_card.py`：三处 `cv2.imread` 改 `imread_rgb()`（`np.fromfile` + `imdecode`）。`cv2.imread` 在 Windows 上遇非 ASCII 路径直接返回 `None`，而本项目输出目录名多为中文；且 `--preview-only` 分支原本没判 `None`，会在下一行抛看不懂的 `cv2.error`
- `make_memory_card.py`：贴纸版宽度 `148.0` 原写死在两处除法里 → 抽成 `STICKER_SHEET_WIDTH_MM`
- `forge.py`：交付时调 doctor 导出刀线不看返回码，失败也照样打印「生产文件：cutline.svg」 → 改为校验文件确实生成，否则明确报错
- `forge.py`：`do_relayout` 里 `except Exception: meta = {}` 静默吞掉解析失败 → 改为打印警告
- `forge.py`：`figures_note` / `text_note` 为空时拼出「人物剪影不合格：。」这类标点错乱，而这些文案会原样喂回模型当重试指令 → 空值兜底
- `providers.py`：分辨率门禁前 `except Exception: return out_png` 会静默跳过门禁 → 改为打印警告
- 所有含中文的落盘统一显式 `encoding="utf-8"`（`preflight.json` / `prompt_round*.txt` / `qc_report.md` / `scene_prompt.txt` / `report.md` / `cutline*.svg` / `厂家须知.txt` / `卡纸说明.txt`）。非 UTF-8 locale 的机器上原本会直接 `UnicodeEncodeError`

**存疑未改**：`grab_elements()` 分行排序里写死的 6（改动会影响元素编号顺序）；卡纸 `MIN_ELEM_AREA_MM2=120` 与 doctor `--min-area 25` 两套阈值；`board` / `screen` 会顺带命中案板、屏风这类本身有价值的物件（词级匹配无法区分，且只降权不删除）。

**测试**：`tests/` 由 11 项扩到 **29 项，全绿**

- 新增 `tests/test_photo_io_and_counts.py`（12 项）：EXIF 方向（造一张 `Orientation=6` 的临时 JPEG，覆盖 `forge.open_photo` / `shrink` / 无 EXIF 无操作 / 印前侧 `print_ready_doctor.open_photo`）+ 枚数门禁六种情形
- `tests/test_select_objects.py` 11 → 17 项：树体三部件同族、选品后只剩 1 枚树体件、建筑/花部件族、低价值件降权 / 兜底回填 / 压过纪念物标注
- 测试 import 路径支持 `FORGE_DIR` 覆盖，便于同一套断言测多份副本

**变更文件**：`memory-sticker-forge/forge.py`、`memory-sticker-forge/forge_scene.py`、`memory-sticker-forge/providers.py`、`print-ready-doctor/print_ready_doctor.py`、`print-ready-doctor/relayout.py`、`print-ready-doctor/impose_a3.py`、`print-ready-doctor/make_memory_card.py`、`tests/`、`CONTEXT.md`、`ASSUMPTIONS.md`

---

## 2026-08-30 · 新增 COLOR_SPREAD 跨枚色彩分布约束 · v3.3.2

**做了什么**

- `memory-sticker-forge/forge.py` 新增 `COLOR_SPREAD` 常量，并挂到贴纸版 prompt 的 PALETTE 之后（**纯增补**：`STYLE` / `STYLE_REMINDER` / `THICKNESS` / `NEGATIVE` 一字未改）
- 约束语义三条：
  1. 整套元素必须读作高对比配色，不许六枚全落在黄 / 金 / 米同一色相区间
  2. 每枚忠实源照片的物体固有色 —— 金属保持冷灰银，白色 / 米色物体保持米白，不为凑色板把物体染成暖黄
  3. 整版至少一个**深色锚点**（深赭 / 墨棕 / 深红）+ 至少一个**冷色调剂**（森绿 / 橄榄 / 灰蓝）

**解决的问题**：整版色相集中、画面发闷 —— 实拍暴露「6 枚里 4 枚全落在黄/金/米色、金属勺被染成金黄」，根因是此前只有单枚色板约束、缺少跨枚层面的配色分布规则。

**验证**：经 5 张真实照片实跑验证有效，色彩明显改善（金属回到冷灰、出现深色锚点）。

**测试**：`tests/` 11 项全绿，未修改任何断言

**变更文件**：`memory-sticker-forge/forge.py`

---

## 2026-08-28 · 修复客户反馈的三处回退（枚数 / 蛋糕形状 / 画风）· v3.3.1

**已完成**

1. **卡纸图元素数 5 → 6**（`print-ready-doctor/make_memory_card.py`）
   - `--max-elements` 与 `build_card(max_elems=)` 默认值由 5 改为 6，恢复「6 枚 = 5 物品 + 1 人物」的硬性产品规格
   - 排版逻辑**无需改动**：右栏 `rows = ceil(n/2)`，n=5 与 n=6 同为 2 列 × 3 行，第 6 枚只是填上原先空着的那格
   - 用 7 张真实 FINAL.png 实测 5 枚 vs 6 枚：**零重叠、零出血**，最差邻距 6.54 mm 两者持平，四边余量恒 >2.2 mm

2. **蛋糕被泛化成圆蛋糕**（`memory-sticker-forge/forge.py`）
   - `COMPOSITION` 新增 **SHAPE FIDELITY** 一条：必须保持原照片中该物体的实际形状、比例与朝向，禁止把局部/切片补全成完整物体（a slice of cake stays a wedge, never becomes a whole round cake）
   - 复查选品链路：`_decompose()` 只处理成套装备，`COMPOSITE_REPLACE` / `SIMILAR_FAMILIES` / `_family()` **均不含 cake 类同义词折叠**，`cake slice` 本来就原样进 prompt，无折叠可去除（已加测试外验证）

3. **画风回退：prompt 瘦身 + 去笔触**（`memory-sticker-forge/forge.py`）
   - `STYLE`：删除 `visible dry brush texture`，改为 `clean crisp edges like scissors-cut paper, flat opaque color blocks`，并补 `no visible brush strokes`
   - `STYLE_REMINDER`：改为 `flat solid colour blocks, crisp cut-paper silhouette, minimal internal texture`
   - `THICKNESS`：6 行冗长描述压成一句，保留「最细笔画不低于模切阈值」与「不许靠删物品满足粗度」两个核心语义
   - 合并重复约束：过细/文字/写实/笔触原本在 `COMPOSITION`、`LAYOUT`、`NEGATIVE` 里各说两三遍，现各归一处；`NEGATIVE` 由 21 项裁到 10 项
   - 顺手修掉 `NEGATIVE` 与人物后缀拼接产生的 `borders., people` 标点错误

**Prompt 体积**（日间/有人物，6 枚，含纪念物段）

| | 修改前 | 修改后 |
|---|---|---|
| 总长 | 8631 字符 | **5638 字符**（−34.7%） |
| 风格段占比 | 3293 / 38.2% | **2794 / 49.6%** |

夜场 8385→5551，无人 7545→4904；场景图 prompt 4768→4261。

**关键决策记录**
- 人物模式**保持 `collage` 不变**，未回退 `silhouette`（用户明确否过单色棕色剪影）。本次只让 collage 的色块更平涂干净，`FIGURE_COLLAGE` 的四块分色/无脸/纸缝/粗壮规则一条未删，仅压缩措辞
- 未达成字面上的「4500 字符以内」目标：**原始基线实测为 8631 而非 6800**，按压缩比算已达标（−34.7% vs 要求的 −33.8%）。再往下压只能削风格段或删有实拍依据的功能性约束（容器剔除、纪念物保护、IP 合规、邻距边距），两者都会造成新的回退，故止步 5638

**测试**：`tests/` 11 项全绿，未修改任何断言

**变更文件**：`memory-sticker-forge/forge.py`、`print-ready-doctor/make_memory_card.py`、`print-ready-doctor/README.md`

---

## 2026-08-28 · 修复后首次实拍验证 + 新增 2 单交付 · v3.2

**已完成**
- **修复后代码首次真跑生图**，两张新照片全部通过：
  - 鸟巢（2268×4032）：第 3 轮通过。第 1、2 轮均被**新增的容器质检**拦下（帐篷/灯头/鸟巢带着底座和地面）
  - 银杏（4032×3024）：第 2 轮通过。第 1 轮被**新增的重复质检**拦下（ginkgo leaf 与 leaf cluster 同族）
- v3.1.1 三项新质检（duplicate / container / missing）**实拍确认有效**，均真实触发了定向重试
- `select_objects()` 实拍确认有效：银杏单自动剔除 `tree branch`（与 ginkgo leaf 容器关系）
- 独立目检（analyze_image 二次复核）：两张均 6 枚齐全、无文字/logo、人物无五官、无粘连、暖白描边到位
- 导出两单完整厂家文件并上传云盘（10 个文件，含 SVG 内嵌图 / A5 PNG / 三种 PDF）
- 新建干净的 GitHub 仓库 `memory-sticker-studio`：剔除 1.2GB 历史产物，仅保留 20 个源码与文档文件（952KB），已本地 commit
- 新增顶层 `README.md`：完整说明产线six大设计要点与每个坑的根因

**关键决策记录**
- `SIMILAR_FAMILIES` 补入 6 个物品族（leaf/trunk/stone/window/bag/tent）。`leaf` 族缺失导致银杏单漏判，是 v3.1.1 遗留的词库覆盖问题
- 银杏单最终**保留**单片叶 + 叶簇：该照片 G0 候选池仅 6 个物品，剔除同族后不足 6 枚，兜底回填。属照片本身限制，非代码缺陷
- GitHub 仓库不含用户原始照片（隐私）与 `run_*/` 产物（体积）

**变更文件**：`memory-sticker-forge/forge.py`（词库）、`memory-sticker-studio/`（新仓库）

**待下一步**：需用户提供 GitHub 仓库地址与 PAT 才能 push；`gpt-image-2` 真实调用仍未验证（本次走的是内部图生图后端）

---

## 2026-08-27 · 真实出图验证 + v2 交付 · v3.2

**已完成**
- ✅ **首次真实生图验证 v3.1.1 修复效果**，4 张照片全部通过双重质检
- 新增 `COMPOSITE_REPLACE` 复合体拆解表 + `_decompose()`：整套装备自动换成单件
- `PREFLIGHT_TASK` 新增规则 5：禁止选 kit / set / rig / stand / tripod / rack 类成套装备
- 产出 v2 交付包（8 个文件）并上传云盘

**四单结果**
| 单 | 轮次 | 最终 6 枚 |
|---|---|---|
| 生日 | 3 | 蛋糕(带蜡烛)、仙女棒、玫瑰花瓣、吊灯、汤匙、人物 |
| 演出 | 2 | 电吉他、麦克风、底鼓、镲片、橙色音箱、人物 |
| 故宫 | 4 | 汉白玉栏杆、石狮、脊兽、匾额、石灯、宫殿 |
| 志愿 | 2 | 吉祥物、志愿者马甲、移动展台、宣传册、水瓶、人物 |

**新质检规则实战拦截记录**（证明修复真实生效，非纸面改动）
- 生日第 2 轮：`duplicate_pairs` 抓到"两片玫瑰花瓣 vs 单片玫瑰花瓣"→ 打回重出
- 演出第 2 轮：`duplicate_pairs` 抓到"microphone vs person holding a microphone"→ 打回重出
- 演出第 1 轮 / 故宫第 3 轮：`objects_with_container` 抓到贴纸带底座 → 打回重出
- 上述三类拦截在 v3.1.1 之前**完全不存在**，是缺陷直接流到成品的根本原因

**独立视觉复核结论**（analyze_image 交叉验证，非产线自评）
- 生日：蜡烛 ✅ 仙女棒 ✅ 全图无任何盘子、无重复 ✅
- 演出：仅一把电吉他 ✅ 无雷同乐器 ✅
- 用户报告的三个缺陷全部确认修复

**Bug 修复**
- 🔴 `drum kit` 死循环：整套架子鼓天然由镲架细杆支撑，同时触发"结构过细"和"自带支架"两条质检，**4 轮全部失败、不可能收敛**。根因是 G0 把成套装备当单品选，且列入 keepsake 强制保留 → 新增复合体拆解，`drum kit`→`bass drum`，重跑第 2 轮即通过
- 编辑 `_family()` docstring 时误删首行导致语法错误，已修复

**交付物**
- 8 个文件 = 4×内嵌图刀线 SVG + 4×A5 原图 PNG（2331×3307 = 148×210mm @400dpi，实测精确）
- 本地：`交付_v2_20260827/`（另含 4 份纯刀线 SVG）

**变更文件**：`memory-sticker-forge/forge.py`

**待下一步**：首单打样确认色差（文件为 RGB）；`gpt-image-2` 仍未实测，本次走的是内部图生图后端

---

## 2026-08-27 · 元素选品逻辑重构（丢失/重复缺陷修复）· v3.1.1

**问题现象**（用户实拍反馈）
- 生日照：烟花和蜡烛没有了
- 生日照：蛋糕自带盘子，另有一枚单独的盘子，两枚重叠
- 演出照：出现两把吉他，元素重复

**根因**（已用 `run_final/*/preflight.json` 复现，**非随机，100% 可复现**）
1. `build_prompt` 里 `picked = objs[:n_obj]` 直接截断前 5 个。生日照 G0 清单里 `candle` 排第 7 位 → **蜡烛从未进入 prompt**
2. `sparkler stick` 被 G0 归入 `thin_parts`，而 `THICKNESS` 规则允许省略 thin_parts → **烟花被明确指示删掉**
3. `cheesecake` 与 `plate` 同时入选前 5，蛋糕本就盛在盘子上 → 蛋糕贴纸自带盘子 + 单独盘子贴纸 = 重叠
4. `electric guitar` 与 `bass guitar` 同时入选前 5，扁平剪纸风格下轮廓几乎一致 → 视觉上两把吉他
5. `EXCLUDE` 里其实已有"物品必须单独画、不带承载物"的规则，但**只在跨照片（多单）时启用**，同一张纸内部完全不生效
6. `VISUAL_TASK` 采集了 `object_names`，但质检逻辑**从未检查过重复或缺失**，出问题也不会触发重试

**已完成**
- `PREFLIGHT_TASK` 新增三个字段：`keepsake_objects`（纪念物白名单）、`container_pairs`（容器承载关系）、`similar_pairs`（外形雷同关系）；排序依据从"辨识度"改为"纪念价值"
- `thin_parts` 字段加强约束，明确禁止填写主体物品名；`preflight()` 增加保险：凡同时出现在 `standalone_objects` 的一律从 thin_parts 剔除并记录 `_rescued_from_thin`
- 新增 `select_objects()` 取代 `objs[:n_obj]`：容器剔除 → 同族折叠 → 纪念物置顶 → 分层兜底回填
- 新增 `SIMILAR_FAMILIES` 内置同族词库（吉他/鼓/杯/盘/餐具/音箱/灯/手机/椅）作为 G0 漏标时的兜底
- `COMPOSITION` 新增两段常驻硬规则：`DRAW EVERY LISTED OBJECT`（禁止省略替换）、`NO DUPLICATES, NO CONTAINERS`（禁止连带容器、禁止同族雷同）
- 新增 `KEEPSAKE` 段：纪念物即使纤细也必须加粗放大保留
- `VISUAL_TASK` 新增 `duplicate_pairs` / `objects_with_container` / `missing_objects` 三项检查，`qc_visual()` 新增 `expected` 参数，三项任一不合格即触发定向重试
- main 流程打印选中物品、纪念物、被剔除项及剔除理由

**Bug 修复（本次改动中自查发现）**
- 🔴 `_family()` 初版用子串匹配，把 `microphone` 误判为 phone 族、`guitar amplifier` 误判为 guitar 族、`bass drum` 误判为 guitar 族，导致候选被过度砍光 → 改为**中心词（最后一个词）判族**
- 🔴 兜底回填初版会把刚剔除的重复项原样填回，等于没修 → 改为分层回填，容器重复项永不回填

**回归验证**（用两组真实 G0 数据）
- 生日 P1：`['cheesecake','plate','glass cup','red leather chair','spoon']` → `['candle','sparkler','cheesecake','glass cup','red leather chair']`，plate 已剔除，蜡烛烟花已置顶
- 演出 P3：`['electric guitar','bass guitar','microphone','bass drum','guitar amplifier']` → `['electric guitar','microphone','bass drum','guitar amplifier','smartphone']`，bass guitar / snare drum 已剔除
- 两组均 5 枚齐全、同族零重复；无人场景（故宫）回归正常

**变更文件**：`memory-sticker-forge/forge.py`

**待下一步**：⚠️ 本次仅验证了**选品逻辑**（不消耗生图额度）。新 prompt 的实际出图效果需拿真实 key 重跑 P1/P3 确认

---

## 2026-08-27 · 项目文档体系建立 · v3.1

**已完成**
- 创建 `CONTEXT.md`（项目上下文）与 `CHANGELOG.md`（变更日志），用于新开对话时同步完整信息与进度
- 沉淀所有关键规格、设计决策、已知问题与竞争态势判断

**关键决策记录**
- 在 `CONTEXT.md` 中写入特别约定：**拿到第 1 个付费用户之前不提议新功能开发**，防止继续过度工程化

**变更文件**：`CONTEXT.md`（新增）、`CHANGELOG.md`（新增）

**待下一步**：商业验证 —— 找到 ≥3 个愿意真付 99 元的人

---

## 2026-08-27 · 4 单成品交付 + 数码模切格式适配 · v3.0

**已完成**
- 4 张照片全部跑通产线，产出成品：
  - 生日聚会 / 演出现场 / 故宫 —— 第 1 轮通过
  - 志愿活动 —— 第 1 轮桌腿过细被拦，第 2 轮通过
- 全部输出 2331×3307px（A5 @400dpi），6 枚/版
- 演出现场检出 Fender/Orange/Dixon logo 与舞台屏幕内容，已剔除；标记疑似未成年人
- 故宫检出知乎水印与匾额文字，已剔除
- 新增 `export_for_digital_cut.py`：输出单枚透明 PNG、`整版_印刷图_A5.pdf`、`整版_图加刀线.pdf`、`整版_纯刀线.pdf`、尺寸清单
- 新增 `embed_svg.py`：把 SVG 引用的外部位图 base64 内嵌
- 8 个核心文件（4×SVG + 4×A5 原图 PNG）上传云盘

**关键决策记录**
- 用户澄清对方是**数码模切店**而非传统印刷厂 → 交付物从"专色刀线 SVG + 厂家须知"改为**双方案**：打印切割一体给 PDF，来图定制给单枚透明 PNG
- 交付形式从"工作区本地路径"改为**云盘可点击链接**（用户反馈本地路径打不开）

**Bug 修复**
- 🔴 交付级：`export_for_digital_cut.py` 初版用 CairoSVG 直转外链 SVG，生成的"图加刀线 PDF"只有 3KB，实际**只有刀线没有图**。修复：转 PDF 前先内嵌位图，修复后 5.6–6.9MB，渲染目检通过
- 🔴 交付级：`cutline.svg` 引用 `../FINAL.png`，单独发店家会丢图 → `embed_svg.py` 解决
- 云盘上传取 folder token 用错字段（`.data.token` 返回 null），正确字段是 `.data.folder_token`

**变更文件**：`export_for_digital_cut.py`（新增）、`embed_svg.py`（新增）、`厂家须知.txt`、`交付说明.md`

**待下一步**：首单必须打样确认色差；若店家要 AI/CDR/DXF 需补导出

---

## 2026-08-26 · gpt-image-2 适配 · v2.3

**已完成**
- `providers.py` 默认模型改为 `gpt-image-2`
- 新增尺寸预设：`a5-300` → 1760×2480，`a5-400` → 2336×3312
- `_oai_size()` 本地校验：宽高须为 16 倍数、最长边 ≤3840、总像素 ≤8.29M、比例 ≤3:1
- 支持 `OPENAI_IMAGE_QUALITY` / `OPENAI_IMAGE_BACKGROUND` / `OPENAI_INPUT_FIDELITY`

**关键决策记录**
- 用户决定**不用火山，只用 image2**，火山路径保留但不再主推
- 核实结论：Codex Plus 订阅**不含** Image API 额度，两者需分别付费

**Bug 修复**
- 🔴 `size` 变量算出 1760×2480 但调用时仍写死 `1024x1536` → 改为 `size=size`。若不修，输出会被分辨率门禁全部拦死

**未完成 / 阻塞**
- ❗ 当前环境 `OPENAI_API_KEY` 实为会话 ID，`OPENAI_BASE_URL` 指向内部代理，调 `gpt-image-2` 返回 404 → **真实调用从未验证过**

**变更文件**：`providers.py`

**待下一步**：用户用自己的 key 跑 `selftest_provider.py --all`

---

## 2026-08-25 · Codex / 本地迁移改造 · v2.2

**已完成**
- 新增 `providers.py` 适配层，隔离全部平台相关依赖
- `forge.py` 清理残留内部路径常量（`VISION`、`IMGEDIT_DIR`）
- 工具路径支持环境变量覆盖与多级自动查找
- 新增 `tools/selftest_provider.py`：三级自检，退出码 0/1/2/3 分别对应通过/配置缺失/调用失败/输出不达标
- `requirements.txt` 补 `openai>=1.40`
- `impose_a3.py` 字体三平台候选 + `CJK_FONT_PATH` + 无中文字体自动英文降级

**关键决策记录**
- Provider 统一为两个接口：`generate_image()` 与 `analyze_images()`，生图与视觉可分开配置

**变更文件**：`providers.py`（新增）、`selftest_provider.py`（新增）、`forge.py`、`impose_a3.py`、`requirements.txt`

---

## 2026-08-25 · 印前工具链完善 · v2.1

**已完成**
- `impose_a3.py`：A3 四宫格拼版 + 裁切标记 + kiss cut 半切线 + A5 全切线
- 输出 `A3_print.png` / `A3_production.svg` / `A3_production_embedded.svg` / `A3_preview.png` / `imposition_report.md` / `厂家须知.txt`
- `relayout.py` 新增 `--min-input-dpi 300`，低分辨率输入退出码 4

**Bug 修复**
- 低分辨率门禁定位修正：起初误判为"物理尺寸变小"，实测是 `relayout` 插值放大导致**假 400dpi**，细节糊但数值合格 → 门禁必须加在输入端

---

## 2026-08-24 · 出图质量问题修复 · v2.0

**已完成**
- 人物画法新增 `collage` 并设为默认（无脸，但保留头发/肤色/衣服色块）
- `relayout.py` 默认 margin 从 8mm 提到 10mm

**Bug 修复**
- 🔴 人物变棕色：prompt 写死单色深棕剪影且禁止肤色 → 新增 `FIGURE_COLLAGE`，`silhouette` 降为可选
- 🔴 物品丢失：演出图只剩人物剪影，吉他/鼓/麦克风全丢 → G0 显式提取物品清单写进 prompt，强制 5 物品 + 1 人物，禁止删除物品本体
- 贴纸贴边：盘子靠近左边缘 → margin 提至 10mm

---

## 2026-08-23 · 出图产线搭建 · v1.0

**已完成**
- `memory-sticker-forge/forge.py` 主产线：G0 体检 → 出图 → 重排 → 双重质检 → 定向重试
- `print-ready-doctor/print_ready_doctor.py` 量化质检
- `print-ready-doctor/relayout.py` 程序化 A5 重排
- 自研风格 prompt v1.2 定版（水粉 + 手切纸拼贴 + 暖白描边 + 哑光纸纹）

**关键决策记录**
- 每版固定 **6 枚 = 5 物品 + 1 人物**；无人照片不造人
- 第三方 IP / logo / 文字 / 水印必须剔除
- 重试有限次（默认 4 轮），因 AI 出图有 token 成本

---

## 2026-08-22 · 产品定位调整 · v0.3

**已完成**
- PRD v2.0：删除冰箱贴，转向「现场记忆手账 / 定制贴纸套装」
- 定价测算脚本 `pricing.py`
- 全流程 SOP 文档

**关键决策记录**
- ❌ 砍掉冰箱贴：样品不理想、周期 8 天、卡纸磁铁供应不确定
- ❌ 砍掉数字素材包：skill 已开源，且很少人自打印，只作需求探测器
- ✅ 核心价值重新定义为"**把照片里的物件抠出来做成能贴的贴纸**"，而非"生成一张艺术图"
- ✅ 定价：1/2/3/4 张 = 59/99/129/149 元
- ✅ 验证线：**≥3 人真付 99 元才继续**

**成本修正**
- 打样估算从 200–500 元修正为几十元：传统开版打样贵，但数码 POD 单件路线便宜。实测 A3 不干胶模切 20 元

---

## 2026-08-21 · 项目启动 · v0.1

**已完成**
- 小红书「学习，成长，女性」Top10 舆情分析
- 18–34 岁女性内容偏好与高赞需求分析
- PRD v1.0（「现场记忆卡」礼盒方案）

**关键决策记录**
- PRD v1.0 自评为**过度工程化、商业验证不足**，触发后续重写
- 小红书采集需遵守防风控规范（用户账号曾被警告"AI 浏览"）

## v3.3 —— 补齐卡纸打印图 + 修复三处画质回退（2026-08-28）

### 新增
- `memory-sticker-forge/forge_scene.py`：生成主视觉场景图（模块 O1），与贴纸版共用色板段。
- `print-ready-doctor/make_memory_card.py`：卡纸打印图排版。程序化手撕边、统一缩放的右栏贴纸、
  逐字排版标题、镜像出血。产出 PNG / 含出血 PNG / PDF / 元素编号预览。
- 3 张实拍卡纸图：生日、鸟巢、银杏。

### 修复（都是实拍复现出来的）
1. **G0 可选字段为 null 时保护逻辑静默失效**。`setdefault` 只在键不存在时生效，
   而视觉模型会把 `keepsake_objects` / `container_pairs` 显式写成 `null`。
   结果：容器剔除和纪念物置顶两条保护全程没跑，生日单出现「空盘子单独一枚贴纸」。
   改为显式判 `None` + 内置词库兜底推断。
2. **容器规则把纪念物本身删了**。G0 把生日单标成 `["candle","birthday cake"]`
   （蜡烛插在蛋糕上），旧逻辑无脑丢外层 → 蛋糕没了。改为按价值判定：
   容器词最低、纪念物最高，两者同级时丢里层。
3. **色板漂移成灰绿**。`PALETTE_DAY` 把 olive/forest green 列在最前，
   加上 HEAD 里那句 "do not copy its colors"，模型把整张图洗成灰绿，
   蛋糕、蜡烛全变橄榄色。改为：暖色列前且明确是主色，红色强调色必须出现，
   HEAD 改成「把真实颜色映射到最近的色板色」。
4. **视觉质检把好看的那版判废**。容器检查一刀切，把「蛋糕连着自己的小盘子」
   也判成不合格 —— 而参考稿里最好看的恰恰是这一版。改为只拦大面积承载物
   （桌面/地面/舞台），真重复交给 `duplicate_pairs` 和选品阶段兜。

### 测试
- 回归测试 8 → 11 项，新增：纪念物不得被容器规则删除、无提示时空容器仍应剔除、null 字段兜底。

