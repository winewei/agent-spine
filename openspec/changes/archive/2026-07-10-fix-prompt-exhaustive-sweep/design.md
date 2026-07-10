# fix-prompt-exhaustive-sweep — Design

## Context

`templates.py::render_fixer` 渲染的"修复规则 A"（Root-cause 全落点扫描）已经在生产中运行了一段时间，靠的是纯散文约束 + 纯自报（`categories_scanned=<list>` + `fix.summary.md` 的 "Locations Scanned" 段）。npc 侧从未确定性核实过这句自报是否属实——`spec_report.py::_verify_categories_scanned` 只能比对"自报了哪些 category 名"与"实际出现过哪些 category 名"两个集合，验证不了"扫描是否真的覆盖了所有落点"。

实证案例（`run-lessons-feedforward` 的 `error-handling` category 在 round 1/2/3 逐轮复现为 blocking）暴露了这条散文规则的两个弱点：触发条件太宽松（"任意重复"而非"连续复现"）、真实性无人核验（自报即真相）。本 change 的本质不是新增一个机制，而是给这条已存在但毫无强制力的规则加上确定性的"同 category 复现"检测，把复现信号反映进下一轮的证据强度，而不是声称能证明自报为假。

## Goals / Non-Goals

**Goals:**

- 把"category 重复即全扫"这条散文规则收紧为"连续 N 轮同 category 才强制触发穷举清单"（默认 N=2，可配置），降低误报（偶发的非连续重复不再触发最严格格式）同时提高信号质量（连续复现是更强的"上一轮没修到根因"信号）。
- 给"自报是否可信"这件事加确定性的复现检测：某 category 被 fixer 自报扫过之后，若在后续轮次的 review 中再次以 blocking 身份出现，机械地标记该自报为 `unsubstantiated`（未被证实），不依赖任何语义理解，也不声称已经证明该自报为假。
- 存在复现信号的 category 在下一轮无条件强制穷举清单，形成"自报—复现—升级"的闭环，而不是让"自报不可信"这个根因在更严格的格式下重演一遍。
- 阈值可配置，但配置面不扩张到 CLI/项目级约定之外。

**Non-Goals:**

- 不收紧 `schema.py` 的 category 枚举（Non-Goal，见 pattern-interrogation.md OQ6 的用户裁决）——接受"精确字符串匹配、允许漏报"作为已知限制。
- 不新增任何 LLM 调用去判断"两个 category 名是否语义等价"。
- 不改变 review 判定权：npc 只把计数事实和复现事实摆给 coder；穷举清单是否真的做全，仍由下一轮独立 review 判定，npc 不自行核实清单条目的真实性（这点在 Risks 段展开）。
- 不新增 CLI flag，不写入 `openspec/project.md`。

## Pattern Mapping

### Analog 对齐说明（基于 pattern-interrogation.md 的 Analogs）

本次改动定位为**在一条已验证安全的既有管道上加一层更严格的判定**，而不是引入新的数据流向：

- `categories_seen` / `blocking_trend`（`pipeline.py` 的 review phase-exit mutate 闭包 → state → `coder.py`/`agent.py` 两处独立读取 → `templates.py::render_fixer`）是"reviewer 派生的结构化统计（category 名 + 计数）流入同一 change 的 fix prompt"这件事的既有先例，且只搬运 category 名与计数，从不搬运 findings 原文/rubric/评分依据。本次新增的"连续计数"与"`unsubstantiated` 标记"严格延续这个约束：仍然只是 category 名 + 一个整数（streak 计数）+ 一个布尔（是否存在复现信号），不新增任何面向 coder 的自然语言派生。
- `rounds_since_strict_decrease`（`trend.py`，`STALE_THRESHOLD = 3`）是"确定性计数器达阈值触发强约束"的先例，尽管触发对象（整体 blocking 是否递减）与本次（单个 category 连续出现）粒度不同，但"清零重计"的心智模型直接复用：某轮该 category 未出现即计数清零重新累计。
- `review.py::parse_review` 从已过 schema 校验的 `round-*.review.json` 抽取 `categories` 字段的做法本身是安全的，但本次改动**不复用这条读取路径**——用户裁决（OQ1）明确要求连续计数只能从 state 中 `entry["phases"]["review-rN"]["categories"]` 现场重算（该字段本就是 `pipeline.py` review phase-exit 时从 `metrics.get("categories")` 写入的，源头就是 `parse_review` 的输出，只是已经落盘成 state 的确定性字段，不需要再打开 `.review.json` 文件本身）。
- `spec_report.py::_verify_categories_scanned` 是"自报 vs 观测源"交叉校验的既有先例，但目前的判定粒度太粗（只能判"报没报全部 category 名"）。本次新增的 `unsubstantiated` verdict 是在同一函数内新增一档更强的负面结论，复用其既有的 `ok`/`warn`/`unverifiable` 三态判定框架，不另起校验入口。
- `lessons.py` 的"绝不打开 review.json、只信 fixer 自报三字段"边界，是**跨 change**场景的更保守边界，与本次**同 change 内**的场景不冲突（见下方 User Decisions OQ1）。
- `focus.py::extract_fixed_history` 已确认是反方向先例（coder→reviewer），本次改动不参照。
- `openspec/specs/blocking-category-aggregation`（`telemetry.py` 的 `aggregate()`/`hotspots()`）是同一"按 category 计数"思路在**跨 run 遥测层**的独立实现，作用域仅止于 `/spine-analyze` 复盘、不进入任何 prompt。本次改动落在**单 change 执行路径**（fix prompt 生成 + spec-report 校验），与遥测层是两个独立层，不复用也不重复造轮子——只在检测到同 category 复现时新发一条 telemetry 事件供未来遥测消费，不改动 `blocking-category-aggregation` 本身的聚合逻辑。

### Open Questions（原样保留自 pattern-interrogation.md）

- **同一 category 连续出现次数这类"reviewer 产出的派生统计"传给 coder，是否已经越过不变量 1 的红线？** 依据 Analogs 第 1、2 条，`categories_seen`/`blocking_trend`（同样是 reviewer findings 派生的结构化统计：category 名 + 计数）已经在生产中流入同一 change 的 fix prompt，且 `render_fixer` 现有规则 A 已经要求"category 重复出现即全扫"。本次改动是在**同一条已被验证不越界的管道**上加一层"连续计数达阈值→格式更严格"的判定，倾向判定**未越界**——但请用户确认这个判断本身，因为 `lessons.py` 在跨 change 场景下选择了更保守的边界（连字段都不许直接读 review.json，只信 fixer 自报），两条先例给出的"安全半径"不完全一致，需要用户一锤定音：本次改动应该对齐 `categories_seen` 的"同 change 内可读 review.json 抠 category 字段"路线，还是对齐 `lessons.py` 的"绝不打开 review.json，只信自报"路线（后者会让"npc 确定性统计连续次数"这个核心诉求无法实现，因为自报的 `categories_scanned` 正是当前失效的那个字段）？
- **"连续"计数的判定粒度需要用户拍板**：是严格"逐轮不中断"（round N-2、N-1、N 三轮都出现同 category 才算连续 3）？还是"任意两轮出现即算一次重复，不要求相邻"（更宽松，误报率更高）？用户原文举的实证案例（round 1/2/3 均为 error-handling）恰好是逐轮不中断的情况，未澄清中断后是否清零。
- **阈值达到后触发的"强制穷举清单"，其真实性由谁校验？** 若仍只是 prompt 里加一段更严格的自报格式要求（Locations Scanned 段列 covered/new/unreachable），而 npc 不做任何確定性核对（如 `spec_report.py::_verify_categories_scanned` 那样交叉比对"自报 vs 观测"），是否只是把"自报不可信"这个根因换了个更精细的自报格式重演一次？是否需要在本次 change 里同步扩展 `spec_report.py` 或新增一个校验点，去核对"穷举清单条数是否 ≥ 某个下限"或"清单里提到的文件路径是否确实出现在 diff 中"？（这决定本次 change 的范围是否只限 prompt 生成，还是要扩到验证侧。）
- **连续计数的存储位置**：新增 state 字段落盘（如 `entry["category_streaks"]`），还是每次渲染 fix prompt 时从 `entry["phases"]` 里已有的逐轮 `categories` 现场重算（无新字段、无迁移成本，但每次都要遍历全部历史轮次）？两者对现有 `state.json` schema 的兼容性影响不同，需要用户/design 阶段定夺。
- **阈值的配置入口**：走项目级 `openspec/project.md` 约定、走 `[coder]`/`[coder.phase]` 一类 TOML 配置新增键、还是 `npc fix run --category-streak-threshold N` CLI flag？三者对既有配置面的侵入程度不同。
- **category 字符串不稳定的风险是否需要在本次 change 内处理**：`schema.py` 对 `category` 只给建议枚举、"必要时可新增"（自由文本），reviewer 在不同轮次可能用不同措辞描述同一根因类别（如 "error-handling" vs "exception-handling"），导致精确字符串匹配下连续计数被错误打断、阈值永远不触发。是否要求本次 change 顺带收紧 `schema.py` 的 category 枚举为封闭集，还是接受"精确匹配、允许漏报"作为已知限制留在 design 的 Non-Goals？

### User Decisions (Interactive)（原样保留自 pattern-interrogation.md）

- **OQ1（不变量 1 边界）→ 未越界，且本 change MUST NOT 读 review.json**：`categories_seen`/`blocking_trend` 已由 state 流入同一 change 的 fix prompt（`templates.py::render_fixer` 现存参数），是已验证的先例。本 change 的连续计数 MUST 从 `entry["phases"]` 中已落盘的逐轮 `categories` 现场重算，**MUST NOT 打开任何 `round-*.review.json`**——这样既沿用 `categories_seen` 先例，又不触碰 `lessons.py` 划下的更保守边界，两条先例不冲突。传给 coder 的只有 category 名与连续次数，MUST NOT 传 findings 原文 / rubric / 评分细则。

- **OQ2（连续粒度）→ 逐轮不中断，缺席清零**：round N-2/N-1/N 三轮均出现同 category 才算 streak=3；某轮该 category 未出现即清零。匹配实证案例（run-lessons-feedforward 的 error-handling 在 r0/r1/r2 逐轮出现）。

- **OQ3（真实性由谁校验）→ 关键决策：加确定性的复现检测，本 change 范围扩到验证侧。** 只升级 prompt 自报格式等于把"自报不可信"这个根因换个更精细的格式重演一遍。核心洞察：**某 category 在被 fixer 声称扫过（出现在某轮 `categories_scanned`）之后，若在后续轮次的 review 中再次出现为 blocking，即确定性地构成上一轮"全落点扫描"自报的复现信号，标记该自报为未被证实（`unsubstantiated`）**——这是无需理解语义、可纯机械判定的信号，但不是对该轮自报的证伪证明：同 category 复现既可能是原根因未修透，也可能是修复引入的新问题，npc 不做语义归因，只记录复现事实。本 change MUST：(a) npc 确定性检测该复现条件；(b) 发 telemetry 事件/字段标记同 category 复现（`category_recurrence_after_sweep_claim`）；(c) `spec_report.py` 的 `_verify_categories_scanned` 增加 `unsubstantiated` verdict（区别于现有 `ok`/`warn`/`unverifiable`——现有实现只比对 category 名集合，验证不了落点覆盖度）；(d) 下一轮 fix prompt 对存在复现信号的 category 强制要求穷举落点清单（逐条标注已覆盖/新增覆盖/确认不可达及理由）。

- **OQ4（连续计数存储）→ 现场重算**：从 `entry["phases"]` 已有的逐轮 `categories` 重算，不新增 state 字段、无 schema 迁移成本。

- **OQ5（阈值配置入口）→ TOML `[coder]` 新增键**（默认 2），与既有配置面惯例一致；不加 CLI flag、不进 `openspec/project.md`。

- **OQ6（category 字符串不稳定）→ 列入 Non-Goals，接受"精确匹配、允许漏报"作为已知限制**：本 change MUST NOT 收紧 `schema.py` 的 category 枚举为封闭集（那是独立的 schema 变更，影响面波及所有既有 fixture）。design.md 的 Risks 段 MUST 明确记录：reviewer 措辞漂移（如 error-handling vs exception-handling）会打断连续计数、导致阈值漏触发；这是刻意取舍——宁可漏报，不引入 schema 破坏性变更。

## Decisions

### D1: 连续计数与复现判定都只从 `entry["phases"]` 现场重算，不新增 state 字段

`entry["phases"]["review-rN"]["categories"]`（`pipeline.py` 的 review phase-exit mutate 闭包早已写入）和 `entry["phases"]["fix-rN"]["categories_scanned"]`（fix phase-exit 时写入的自报字符串）已经是逐轮落盘的确定性数据。新增一个纯函数（提案位置：`trend.py`，与 `rounds_since_strict_decrease` 同模块，复用其"清零重计"心智模型），输入 `entry["phases"]`，输出：

- `category_streaks: dict[str, int]`——每个在最近一轮 review 中出现的 category，其连续出现轮数（逐轮不中断，缺席清零，OQ2）。
- `recurred_categories: list[dict]`——对每个 `fix-rN` 自报的 `categories_scanned` 中的 category，检查是否在任一 `review-rM`（M ≥ N）中再次以 blocking 身份出现；命中即判定为复现（该轮自报标记为 `unsubstantiated`），记录 `{category, claimed_at_round: N, recurred_at_round: M}`（取满足条件的最小 M，即最早出现复现的那一轮）。这是一个复现信号，不是对 `claimed_at_round` 那次自报的证伪证明（见下方 Risks 段的认识论边界说明）。

**轮次时序（消除 F2 歧义，据 `pipeline.py` 实际调度逻辑）**：一次 review-fix 循环的真实 phase 顺序是 `review-r0 → fix-r1 → review-r1 → fix-r2 → review-r2 → …`——即 `fix-rN` 的直接前驱恒为 `review-r(N-1)`（`pipeline.py:1558` `prev_phase = f"review-r{round_n - 1}"`），直接后继恒为 `review-rN`。因此：

- `review-r(N-1)` 是触发 `fix-rN` 该 category 自报的**原因**（该 category 正是因为在 `review-r(N-1)` 中被判 blocking，`fix-rN` 才会去扫描并自报 `categories_scanned` 包含它）——它不是、也 MUST NOT 被当作对 `fix-rN` 自报的复现证据。
- `review-rM`（M ≥ N，即 `review-rN`、`review-r(N+1)`、……）是 `fix-rN` 完成之后才发生的 review，是唯一合法的"声明之后的证据"来源。`M ≥ N` 这个判据在时序上等价于"`review-rM` 发生在 `fix-rN` 之后"，二者是同一件事的两种写法，不存在需要另行澄清的时序错位。

两个输出都是 `entry["phases"]` 的纯函数，不落盘新 state 字段（OQ4，且 D5 明确将复现判定纳入同一"不落盘"边界，见下），每次渲染 fix prompt、渲染 spec-report、或 review phase-exit 判定是否需要发 telemetry 事件时均现场重算——`entry["phases"]` 历史轮次数量有限（review-fix 循环有 `max_rounds`/`max_rounds_large` 上限），遍历成本可忽略。

**Why**：避免 state schema 迁移成本；`entry["phases"]` 已经是唯一真相源，现场重算保证不会出现"缓存字段与源数据不同步"的漂移风险。**Alternative rejected**：落盘 `entry["category_streaks"]`——需要在每次 review phase-exit 时同步维护，且 resume 场景下要处理"字段缺失走兜底重算"的双路径，复杂度不低于直接现场重算。

### D2: `render_fixer` 与两处调用点（`coder.py`/`agent.py`）共享同一份纯函数，不各自重算

`render_fixer` 目前有两个独立调用点：`coder.py::_render_prompt_file`（headless dispatch 路径）与 `agent.py::prompt_render`（in-session dispatch 路径），二者各自从 state 读 `categories_seen`/`blocking_trend` 再传给 `templates.render_fixer`。新增的 `category_streaks`/`recurred_categories` 计算逻辑必须落在 D1 的共享纯函数里，两处调用点都改为"读 state → 调用该纯函数 → 传参"，不允许任一方各自实现一份计算逻辑——这正是本 change 要修的"同类问题在多处调用点独立漂移"的模式，若计算逻辑本身也重复实现两份，则重蹈覆辙。

**Why**：两个 dispatch 路径（headless / in-session）除了触发方式不同，语义必须完全一致；重复实现会导致 headless 档和 in-session 档在同一轮次可能算出不同的 streak/复现 结果。

### D3: `render_fixer` 新增两个可选形参，未触发阈值时 prompt 与现状逐字等价

`templates.render_fixer` 新增 `category_streaks: dict[str, int] = {}` 与 `recurred_categories: list[str] = []`（或等价的合并结构）两个形参。渲染逻辑：

- 计算 `escalated = {c for c, n in category_streaks.items() if n >= threshold} | set(recurred_categories)`。
- `escalated` 为空集时，"修复历史"段与规则 A 文案与本 change 之前逐字等价（向后兼容）。
- `escalated` 非空时，对应 category 的规则 A 文案追加强制穷举清单格式要求（覆盖 / 新增覆盖 / 确认不可达 + 理由 三态），并在"修复历史"段标注每个 category 的当前 streak 值 + 是否存在复现信号（`unsubstantiated`）。

**Why**：新形参默认值保证旧调用点（若存在）不破坏；`escalated` 集合的并集语义直接体现 OQ3(d)——存在复现信号的 category 无条件触发穷举清单，不要求先达到 streak 阈值。

### D4: 阈值走 `[coder]` 新键，复用 `max_rounds`/`max_rounds_large` 一类的字符串校验模式

`CoderConfig` 新增 `category_streak_threshold: int = 2` 字段，`__post_init__` 校验 `isinstance(..., int) and >= 1`；`config.py` 的 TOML 解析新增 `coder_raw.get("category_streak_threshold", 2)` 读取与类型校验，报错文案格式与 `[review].max_rounds_large`/`[spec_review].max_rounds` 一致（`f"[coder].category_streak_threshold 必须是整数 ≥1（{source}）"`）。

**Why**：与 OQ5 用户裁决一致；沿用既有 `int` 配置键的校验/报错惯例，不新造校验风格。**Alternative rejected**：`[coder.phase]` 下按 phase 细分——阈值语义与 phase 后端路由无关，混进 `phase_backends`/`phase_dispatches` 会污染那两个 tuple 结构的语义单一性。

### D5: 复现判定与 `category_streaks` 遵守同一条"现场重算、不落盘"边界；telemetry 通过"重算前后差集"在 phase-exit 当轮即时发出，不依赖任何持久化证据字段

**这条 Decision 是对上一版设计中 D1 与本条之间矛盾的修复（spec 语义评审 F1，round 2）**：上一版曾要求在 review phase-exit 时把复现证据落盘为 `entry["category_recurrence_evidence"]`，供 `spec_report.py` 读取而非重算——这与 D1、以及 OQ4 用户裁决"连续计数与复现判定都只从 `entry["phases"]` 现场重算，不新增 state 字段"直接矛盾。本条修正为：**复现判定 MUST NOT 落盘为任何新 state 字段（含 `entry["category_recurrence_evidence"]` 或任何等价字段），所有消费点一律调用 D1 的同一份共享纯函数现场重算**，与 `category_streaks` 完全对称。

时效性诉求（OQ3(b)：复现发生时就要能发出 telemetry 事件，而不是等到 archive 后 spec-report 渲染才第一次发现）不需要依赖持久化字段即可满足，做法是"重算前后差集"：

- `pipeline.py::_do_review_phase_exit_and_trend` 的 mutate 闭包（`categories_seen`/`blocking_trend` 已在此更新）在**写入本轮 `review-rN` 的 `categories` 之前**，先对 `entry["phases"]`（不含本轮）调用 D1 纯函数得到 `recurred_before`；写入本轮数据后，再次调用同一纯函数得到 `recurred_after`。
- `recurred_after - recurred_before`（按 `(category, claimed_at_round)` 去重比较）即为"本轮新增的复现条目"；对这个差集中的每一条，在该 phase-exit 的事件流程里发一条 telemetry 事件（`category_recurrence_after_sweep_claim`，含 category / claimed_at_round / recurred_at_round=本轮）。差集为空则不发事件。
- `entry["phases"]` 本身（已经落盘的逐轮 `categories`/`categories_scanned`）就是唯一真相源；`recurred_after` 集合此后任何时候都可以由任何消费点用同一份 D1 纯函数无损重算出来，因此**不需要**、也 MUST NOT 额外落盘一份"缓存"。

`spec_report.py::_verify_categories_scanned` 渲染报告时，直接对该 change 完整的 `entry["phases"]` 调用 D1 纯函数现场重算 `recurred_categories`（与它读取已落盘的 `categories_seen` 字段不同——`categories_seen` 是本 change 范围之外的既有落盘字段，维持现状；`recurred_categories` 是本 change 新增的判定，统一走现场重算路径，不新起一个"部分落盘、部分重算"的不一致模式）。

**Why**：现场重算彻底消除"缓存字段是否与源数据同步"这一类漂移风险（尤其是 resume 场景下旧 run 缺该字段、或字段与实际 `entry["phases"]` 计算结果不一致时该信谁的问题），并严格遵守 OQ4 裁决；"重算前后差集"这个模式让 telemetry 事件依然能在复现发生的当轮即时发出，不必等到 archive 后才第一次计算，时效性诉求（OQ3(b)）与"不落盘"约束（OQ4）两者不冲突，可以同时满足。

**Alternative rejected（即上一版 D5，已被 F1(round 2) 判定为与 D1 矛盾）**：落盘 `entry["category_recurrence_evidence"]` 供 `spec_report.py` 读取——直接违反 OQ4"不新增 state 字段"裁决；且需要在每次 review phase-exit 时同步维护、并在 resume 场景处理"字段缺失走兜底重算"的双路径，复杂度与 D1 拒绝"落盘 `category_streaks`"的理由完全相同，没有理由对 `recurred_categories` 单独破例。

### D6: `_verify_categories_scanned` 的 `unsubstantiated` verdict 判定优先级高于 `warn`

汇总逻辑（`_aggregate_self_report_verdict`）新增 `unsubstantiated` 优先级最高（高于现有 `warn`）：只要该 change 现场重算出的 `recurred_categories`（D1 共享纯函数，输入 `entry["phases"]`，见 D5）非空，`categories_scanned` 这条核验的 verdict 即为 `unsubstantiated`，不论集合覆盖层面是否 `ok`。`missing`（集合层面缺失）与 `unsubstantiated`（曾自报但存在后续复现信号、未被证实）是两个独立信号，`unsubstantiated` 结果中同时保留 `recurred_categories` 列表这一证据字段（现场重算得到，非读取持久化字段），供报告呈现。

**Why**：`unsubstantiated` 是比"集合层面缺了某个 category 没报"更严重的信号（不是"漏报"而是"报了但后续复现、未被证实为真"），理应在汇总优先级里排在最前，避免被 `warn` 掩盖。

### D7: 复现是强信号，不是证伪证明——认识论边界在实现与文案中必须保持一致（spec 语义评审 F1，round 3）

**这条 Decision 是对本次 round 3 F1 的修复**：round 2 修复后的 spec 仍在多处使用"证伪""为假""refuted"等词，隐含"后续同 category 复现即确定性证明前一轮自报为假"的过强断言。该断言不成立：后续 fix 轮次可能在不同位置引入同 category 的新问题（不同落点、声明当时并不存在），也可能是同一根因未修透——npc 在不打开 review 原文、不做语义归因判断的前提下，**无法机械区分**这两种情况。

本 change 因此把所有"证伪/refuted/为假"语义统一改为"复现/recurrence/unsubstantiated"语义：

- 检测机制本身完全不变：`fix-rN` 自报 `categories_scanned` 之后，若在任一 `review-rM`（M ≥ N）中同 category 再次以 blocking 身份出现，即确定性记录一次"复现"。
- 这只是一个强复现信号，MUST NOT 被实现或文案表述为"证明该轮自报为假"；telemetry 事件名（`category_recurrence_after_sweep_claim`）、`spec_report.py` 的 verdict 名（`unsubstantiated`，即"未被证实"而非"被证伪"）、以及 fix prompt 文案，均 MUST 使用"复现/未被证实"而非"证伪/为假"的措辞。
- 下一轮 fix prompt 面向被标记 `unsubstantiated` 的 category 强制穷举清单这一行为本身不变——无论复现的根因是"未修透"还是"新引入"，穷举清单都是正确的下一步动作；npc 只需要机械记录"复现"这一事实，不需要、也不做归因判断就能驱动这个行为。

**Why**：宁可保留一个语义诚实的弱声明（"复现"）+ 强机制（确定性检测、telemetry、强制穷举），也不做一个语义上站不住脚的强声明（"证伪"）。归因判断（"是同一根因还是新引入的问题"）需要理解 review 语义、跨轮对比 finding 的具体位置和描述，这类语义判断超出 npc 的确定性执行层职责边界（不新增 LLM 调用），因此本 change 明确不做归因，只做复现检测。

## Risks / Trade-offs

- [reviewer 措辞漂移（如 "error-handling" vs "exception-handling"）会打断连续计数的精确字符串匹配，导致阈值应触发而未触发]（OQ6 用户裁决）→ 刻意接受为已知限制；不收紧 `schema.py` 的 category 枚举，宁可漏报也不引入 schema 破坏性变更。
- [复现判定依赖"之前某轮 `fix-rN` 确实自报了 `categories_scanned`"这个前提；若 fixer 干脆不填 `categories_scanned`（自报缺失），则该轮无法被判定为存在复现信号，只会退化为既有的 `unverifiable`]→ 这是"自报缺失"与"自报存在复现信号"两个不同问题，本 change 只解决后者；前者已有 `unverifiable` verdict 覆盖，不重复设计。
- [现场重算 `entry["phases"]` 的成本随轮次数增长线性上升]→ review-fix 循环受 `max_rounds`/`max_rounds_large` 硬上限约束（普通 change 默认个位数，large change ≤30），单次遍历成本可忽略，不引入缓存机制。
- [新增穷举清单格式要求本身仍是 prompt 侧的自报格式升级，`spec_report.py` 不核实清单条目（文件路径/行号）的真实性]→ 与 Non-Goals 一致：npc 不代替下一轮 review 判断"清单是否真的做全"，那是 review 的职责；npc 只确保"曾经的复现信号"这件事本身被机械地记录下来并反映进下一轮 prompt 强度，形成审计闭环。
- [**认识论边界（D7）**：同 category 的后续 blocking 是"强复现信号"而非"证伪证明"——可能是同一根因未修透，也可能是修复过程中在不同落点新引入的同 category 问题。npc 不打开 review 原文、不做语义归因，机械上无法区分这两种情况]→ 刻意接受为已知限制；本 change 的全部实现与文案 MUST 使用"复现/未被证实"（recurrence/unsubstantiated）语义，MUST NOT 使用"证伪/为假"（refuted/false）语义描述该判定的确定性程度。宁可弱声明+强机制，不做超出机械判定能力范围的语义归因。

## Migration Plan

1. `trend.py` 新增纯函数（`category_streaks` + `recurred_categories` 派生），`config.py`/`templates.py` 的改动均为新增可选参数/字段，未触发阈值或无历史轮次时行为与现状逐字等价。
2. `pipeline.py::_do_review_phase_exit_and_trend` 新增"重算前后差集触发 telemetry"逻辑（D5）与一条新 telemetry kind（`EMIT_FIELD_CONTRACT` 同步登记）；不新增任何 state 字段，旧 state（历史 `entry["phases"]`）无需任何迁移——重算函数天然兼容任意历史长度的 `entry["phases"]`。
3. `coder.py`/`agent.py` 两处 `render_fixer` 调用点同步接入 D1 共享纯函数。
4. `spec_report.py::_verify_categories_scanned` 新增 `unsubstantiated` verdict 分支，现场调用 D1 共享纯函数重算 `recurred_categories`（不读取任何持久化字段）；旧 change（`entry["phases"]` 历史轮次不含任何复现场景）重算结果自然为空集，等价于现状的 `ok`/`warn`/`unverifiable` 三态判定不变。
5. 回滚：整组改动均为新增可选路径，删除新增的形参/字段/telemetry kind 登记即可完全回到现状；不改变任何既有必填字段语义。
