# Pattern Interrogation — fix-prompt-exhaustive-sweep

## Analogs

- **`src/npc/templates.py::render_fixer`（行 179-299，尤其 186-234）**：Fix prompt 渲染函数**已经**把 reviewer 派生的结构化统计注入 coder 视野——`categories_seen: list[str]`（去重后的历史 category 名集合）与 `blocking_trend: list[int]`（各轮 blocking 计数序列），渲染成"## 修复历史"段落，且已有文本规则 A「Root-cause 全落点扫描」：*"当某条 finding 的 category 此前任意轮次（含本轮）出现过，强制枚举并修复该 category 不变量在整个 change 范围内的所有落点"*，并要求 RESULT 行 `categories_scanned=<list>` + `fix.summary.md` 的 "Locations Scanned" 段逐条列位置。**这是本次 change 最直接的先例**：本次改动本质是把这条已存在的"任意重复即触发全扫"规则，收紧成"连续 N 轮同 category 才强制触发穷举清单（覆盖/新增覆盖/不可达+理由三态）"，而不是从零引入"reviewer 统计流向 coder"这件事。
- **`categories_seen` / `blocking_trend` 的数据管线**：`src/npc/pipeline.py`（约 395-468 行，`review record` 的 mutate 闭包）从 `metrics.get("categories")`（即 `src/npc/review.py::parse_review` 从 `round-N.review.json` 的 `findings[].category` 字段确定性派生的去重列表）累积写入 `entry["categories_seen"]`；`entry["blocking_trend"]` 同轮累积 blocking 计数。`src/npc/coder.py`（约 262-301 行）在渲染 fix prompt 时从 state 读出这两个字段传给 `render_fixer`。整条链路只搬运**category 名 + 计数**，从不搬运 `findings[].detail`/`recommendation`/`spec_attribution` 等 reviewer 文本或评分依据。
- **`src/npc/trend.py`（`STALE_THRESHOLD = 3`，行 16）+ `rounds_since_strict_decrease`（`pipeline.py` 420-437 行）**：已有的"连续未严格下降轮数达阈值 → 触发 stale 标记"机制，是"确定性计数器达阈值触发强约束"这个模式的先例（尽管触发对象是"整体 blocking 是否递减"而非"单个 category 连续出现"，粒度不同，但阈值判定与升级路径的形态一致）。`docs/principles.md` 不变量 3 也明确认可"硬轨应被真实方差点位打出来才加"——`STALE_THRESHOLD` 正是这类先例。
- **`src/npc/review.py::parse_review`（29-69 行）**：`categories` 列表是从 `round-N.review.json`（已过 schema 校验的确定性 JSON）的 `findings[].category` 字段派生，属**纯函数、确定性读取**，不涉及对 reviewer 自然语言的二次摘要或解读——这为"读 round-*.review.json 统计 category 出现"提供了现成、已验证过不变量安全性的读取路径，本次改动应复用而非另起数据源。
- **`src/npc/spec_report.py::_collect_categories_scanned` / `_verify_categories_scanned`（200-244 行）**：现有的"自报 vs 观测源"交叉校验——拿 fixer 自报的 `categories_scanned`（各轮 RESULT 行）与 `categories_seen`（review 派生的观测源）对照，`missing` 非空则 `verdict=warn`。这是**验证侧**已有的"不裸信自报"机制，与本次改动"prompt 侧强制穷举清单"是同一问题（自报不可信）的两端，值得在 design 里说明是否要把新的穷举清单也纳入这条校验链，而不是只加 prompt 端压力。
- **`src/npc/lessons.py`（1-20 行，模块 docstring）**：`npc lessons record`（run 级、跨 change 前馈）明文规定"只读 `categories_scanned`/`regressions_added`/`notes` 三个 fixer 自报字段，MUST NOT 打开或引用任何 reviewer 产出（`round-N.review.json`/`round-N.focus.md`/`*.spec-review.json`）——守核心不变量 1"。这条边界与 `render_fixer` 的 `categories_seen` 先例**看似矛盾**，实则是**同一条边界在不同粒度上的两种落地**：`categories_seen`/`blocking_trend` 是**同一 change 内、由 npc 确定性计算的结构化字段**（category 名 + 计数，不含 reviewer 措辞），流向该 change 自己的下一轮 fix；`lessons.py` 限制的是**跨 change**、且明确排除直接打开 review.json 文件（即使只想抠出 category 字段也不行），只信任 fixer 自己已经在 RESULT 行确认过的字段。本次改动的作用域是"同一 change 内连续轮次"，形态更贴近 `categories_seen` 先例而非 `lessons.py` 场景；但两条先例对"是否允许打开 round-N.review.json 抠字段"给出了不同答案，是本次设计必须显式对齐的分歧点（见 Open Questions）。
- **`src/npc/focus.py::extract_fixed_history` / `render_fixed_history_section`（47-96 行）**：常被误认成"reviewer→coder"的先例，但读代码后确认方向相反——它是把**fixer 自己上几轮 `fix.summary.md` 的 "Per-Finding Resolution" 段**（coder 自报）注入**下一轮 review 的 focus.md**（即 coder→reviewer，不是 reviewer→coder）。不能作为"reviewer 统计传给 coder"的先例使用；已排除，仅存档说明避免误用。
- **`openspec/specs/blocking-category-aggregation/spec.md`**：`aggregate()`/`hotspots()` 已经在**跨 run 遥测层**（`src/npc/telemetry.py`）实现"按 category 计数、输出 top_blocking_categories"，但作用域是`/spine-analyze` 的复盘分析，不进入任何 prompt、不影响执行路径。与本次改动（进入 fix prompt、影响 coder 行为）是同一"按 category 计数"的思路在不同层的两次独立实现，提示本次改动应放在 npc 的哪一层（`review record` mutate？还是新增 `focus`/`agent` 级派生函数？）需要在 design 阶段定位，不与 telemetry 层重复造轮子。

## Assumptions

- 数据源恒为落盘的 `round-*.review.json`（经 `parse_review` 确定性抽出 `category` 字段），不新增任何 LLM 调用去"理解"或"归并"category 语义；category 相同与否按**字符串精确匹配**判定，不做同义词/语义聚类。
- "连续"的语义 = 该 category 在 blocking findings 中，从某一轮起**逐轮不中断**地出现到当前轮（含当前轮即将渲染的这轮之前一轮）；中间哪怕漏一轮未出现即计数清零重新累计（复用 `rounds_since_strict_decrease` 那种"清零重计"的既有心智模型，而非全历史出现总次数）。
- 新增的"连续计数"不复用 `categories_seen`（那是去重后的 ever-seen 集合，不含次数/连续性），需要在 state 里新增字段（或在渲染 fix prompt 时从 `entry["phases"]["review-*"]["categories"]` 逐轮现算，不落盘新字段）——两种实现路径都满足"确定性读取、不读 reviewer 文本"的约束，具体选哪个留给 write 轮 design 决策，本次盘问不预设。
- 阈值默认 2、可配置：默认值来源沿用用户原始目标原文；配置入口预期类似现有 `[coder.phase]` 一类 TOML 配置或 CLI flag（类比 `STALE_THRESHOLD` 目前是硬编码常量，而非可配置——本次要求"可配置"意味着不能照抄 `STALE_THRESHOLD` 的实现方式，需要新的配置读取路径，这是设计上的增量而非纯复用）。
- 穷举清单的强制格式（覆盖/新增覆盖/不可达+理由）只加在**触发阈值的那些 category** 对应的 fix prompt 段落里，不影响未触发阈值的常规 fix prompt 结构（即向后兼容，round 1/2 且未达阈值时 prompt 与今日一致）。
- 本次改动只动 prompt 生成侧（`templates.py::render_fixer` + 其调用链的数据准备），不改 `review.py`/schema（不新增/收紧 category 枚举），不触碰不变量 1 里"review 判定权"的部分——npc 仍然只是把**计数事实**摆给 coder，穷举清单是否真的做全，最终仍由下一轮独立 review 判定，不由 npc 自己验证清单真实性（这点留 Open Questions 追问是否足够）。

## Open Questions

- **同一 category 连续出现次数这类"reviewer 产出的派生统计"传给 coder，是否已经越过不变量 1 的红线？** 依据 Analogs 第 1、2 条，`categories_seen`/`blocking_trend`（同样是 reviewer findings 派生的结构化统计：category 名 + 计数）已经在生产中流入同一 change 的 fix prompt，且 `render_fixer` 现有规则 A 已经要求"category 重复出现即全扫"。本次改动是在**同一条已被验证不越界的管道**上加一层"连续计数达阈值→格式更严格"的判定，倾向判定**未越界**——但请用户确认这个判断本身，因为 `lessons.py` 在跨 change 场景下选择了更保守的边界（连字段都不许直接读 review.json，只信 fixer 自报），两条先例给出的"安全半径"不完全一致，需要用户一锤定音：本次改动应该对齐 `categories_seen` 的"同 change 内可读 review.json 抠 category 字段"路线，还是对齐 `lessons.py` 的"绝不打开 review.json，只信自报"路线（后者会让"npc 确定性统计连续次数"这个核心诉求无法实现，因为自报的 `categories_scanned` 正是当前失效的那个字段）？
- **"连续"计数的判定粒度需要用户拍板**：是严格"逐轮不中断"（round N-2、N-1、N 三轮都出现同 category 才算连续 3）？还是"任意两轮出现即算一次重复，不要求相邻"（更宽松，误报率更高）？用户原文举的实证案例（round 1/2/3 均为 error-handling）恰好是逐轮不中断的情况，未澄清中断后是否清零。
- **阈值达到后触发的"强制穷举清单"，其真实性由谁校验？** 若仍只是 prompt 里加一段更严格的自报格式要求（Locations Scanned 段列 covered/new/unreachable），而 npc 不做任何確定性核对（如 `spec_report.py::_verify_categories_scanned` 那样交叉比对"自报 vs 观测"），是否只是把"自报不可信"这个根因换了个更精细的自报格式重演一次？是否需要在本次 change 里同步扩展 `spec_report.py` 或新增一个校验点，去核对"穷举清单条数是否 ≥ 某个下限"或"清单里提到的文件路径是否确实出现在 diff 中"？（这决定本次 change 的范围是否只限 prompt 生成，还是要扩到验证侧。）
- **连续计数的存储位置**：新增 state 字段落盘（如 `entry["category_streaks"]`），还是每次渲染 fix prompt 时从 `entry["phases"]` 里已有的逐轮 `categories` 现场重算（无新字段、无迁移成本，但每次都要遍历全部历史轮次）？两者对现有 `state.json` schema 的兼容性影响不同，需要用户/design 阶段定夺。
- **阈值的配置入口**：走项目级 `openspec/project.md` 约定、走 `[coder]`/`[coder.phase]` 一类 TOML 配置新增键、还是 `npc fix run --category-streak-threshold N` CLI flag？三者对既有配置面的侵入程度不同。
- **category 字符串不稳定的风险是否需要在本次 change 内处理**：`schema.py` 对 `category` 只给建议枚举、"必要时可新增"（自由文本），reviewer 在不同轮次可能用不同措辞描述同一根因类别（如 "error-handling" vs "exception-handling"），导致精确字符串匹配下连续计数被错误打断、阈值永远不触发。是否要求本次 change 顺带收紧 `schema.py` 的 category 枚举为封闭集，还是接受"精确匹配、允许漏报"作为已知限制留在 design 的 Non-Goals？


## User Decisions (Interactive)

- **OQ1（不变量 1 边界）→ 未越界，且本 change MUST NOT 读 review.json**：`categories_seen`/`blocking_trend` 已由 state 流入同一 change 的 fix prompt（`templates.py::render_fixer` 现存参数），是已验证的先例。本 change 的连续计数 MUST 从 `entry["phases"]` 中已落盘的逐轮 `categories` 现场重算，**MUST NOT 打开任何 `round-*.review.json`**——这样既沿用 `categories_seen` 先例，又不触碰 `lessons.py` 划下的更保守边界，两条先例不冲突。传给 coder 的只有 category 名与连续次数，MUST NOT 传 findings 原文 / rubric / 评分细则。

- **OQ2（连续粒度）→ 逐轮不中断，缺席清零**：round N-2/N-1/N 三轮均出现同 category 才算 streak=3；某轮该 category 未出现即清零。匹配实证案例（run-lessons-feedforward 的 error-handling 在 r0/r1/r2 逐轮出现）。

- **OQ3（真实性由谁校验）→ 关键决策：加确定性的复现检测，本 change 范围扩到验证侧。** 只升级 prompt 自报格式等于把"自报不可信"这个根因换个更精细的格式重演一遍。核心洞察：**某 category 在被 fixer 声称扫过（出现在某轮 `categories_scanned`）之后，若在后续轮次的 review 中再次出现为 blocking，即确定性地构成上一轮"全落点扫描"自报的复现信号，标记该自报为未被证实（`unsubstantiated`）**——这是无需理解语义、可纯机械判定的信号，但不是对该轮自报的证伪证明：同 category 复现既可能是原根因未修透，也可能是修复引入的新问题，npc 不做语义归因，只记录复现事实（round 3 spec 语义评审 F1 修正）。本 change MUST：(a) npc 确定性检测该复现条件；(b) 发 telemetry 事件/字段标记同 category 复现（`category_recurrence_after_sweep_claim`）；(c) `spec_report.py` 的 `_verify_categories_scanned` 增加 `unsubstantiated` verdict（区别于现有 `ok`/`warn`/`unverifiable`——现有实现只比对 category 名集合，验证不了落点覆盖度）；(d) 下一轮 fix prompt 对存在复现信号的 category 强制要求穷举落点清单（逐条标注已覆盖/新增覆盖/确认不可达及理由）。

- **OQ4（连续计数存储）→ 现场重算**：从 `entry["phases"]` 已有的逐轮 `categories` 重算，不新增 state 字段、无 schema 迁移成本。

- **OQ5（阈值配置入口）→ TOML `[coder]` 新增键**（默认 2），与既有配置面惯例一致；不加 CLI flag、不进 `openspec/project.md`。

- **OQ6（category 字符串不稳定）→ 列入 Non-Goals，接受"精确匹配、允许漏报"作为已知限制**：本 change MUST NOT 收紧 `schema.py` 的 category 枚举为封闭集（那是独立的 schema 变更，影响面波及所有既有 fixture）。design.md 的 Risks 段 MUST 明确记录：reviewer 措辞漂移（如 error-handling vs exception-handling）会打断连续计数、导致阈值漏触发；这是刻意取舍——宁可漏报，不引入 schema 破坏性变更。
