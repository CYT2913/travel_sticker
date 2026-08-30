# 变更日志 · 现场记忆手账

> 每次完成开发任务后追加记录。**只增不改，最新记录在最上方。**

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

