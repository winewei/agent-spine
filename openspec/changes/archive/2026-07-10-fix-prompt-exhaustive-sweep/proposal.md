# fix-prompt-exhaustive-sweep — Proposal

## Why

`templates.py::render_fixer` 已经渲染一条"修复规则 A"：*"当某条 finding 的 `category` 此前任意轮次（含本轮）出现过，强制枚举并修复该 category 不变量在整个 change 范围内的所有落点"*，并要求 fixer 在 RESULT 行自报 `categories_scanned` + 在 `fix.summary.md` 的 "Locations Scanned" 段列出已检查位置。

这条规则目前是**纯散文 + 纯自报**：

1. 触发条件是"任意重复"（哪怕两轮之间隔了好几轮不出现），没有"连续"这个更强的、真正标志"上一轮修复没有触及根因"的信号。
2. 有没有真的做到"全落点扫描"，完全由 fixer 自己一句 `categories_scanned=<list>` 说了算——`spec_report.py::_verify_categories_scanned` 目前只能对比"自报的 category 名集合"与"实际出现过的 category 名集合"，验证不了"扫描是否真的覆盖了所有落点"，只能验证"有没有报"。

实证案例（`run-lessons-feedforward` 的 error-handling category 在 round 1/2/3 连续三轮复现为 blocking）说明：即使 fixer 每轮都在 RESULT 行填了 `categories_scanned`，规则 A 依然没有真正防住"同一根因反复冒头"——因为自报本身可以是假的，而 npc 从不核实。

## What Changes

- **连续计数叠加在既有"任意重复即全扫"规则之上（不替代、不删除该规则）**：既有 prompt 中"category 此前任意轮次出现过即强制全扫"的文字规则保持逐字不变（它是基线指引）；本 change 新增的是其上的**确定性升级层**——fix prompt 的"修复历史"渲染新增按 category 现场重算的连续出现次数（逐轮不中断，缺席清零），达到可配置阈值（默认 2，TOML `[coder]` 新键）时，该 category 段落的自报格式从"列出即可"升级为"强制穷举清单"（逐条标注：已覆盖 / 新增覆盖 / 确认不可达 + 理由）。计数 MUST 从 state 中已落盘的逐轮 `categories`（`entry["phases"]["review-rN"]["categories"]`）现场重算，MUST NOT 打开任何 `round-*.review.json`。
- **确定性检测同 category 复现，标记扫描声明为未被证实**：某 category 在某轮 `fix-rN` 的自报 `categories_scanned` 中出现，但该 category 在其后任一轮 `review-rM`（M ≥ N，即 `fix-rN` 完成之后才发生的 review；实际 phase 时序为 `review-r(N-1) → fix-rN → review-rN → …`，故 M ≥ N 恒等价于"chronologically 在 fix-rN 之后"）中再次以 blocking 身份出现——这是"上一轮全落点扫描自报未被后续轮次证实"的确定性复现信号，但不构成对该轮自报的证伪证明（同 category 复现既可能是原根因未修透，也可能是修复引入的新问题，二者在不做语义归因的前提下无法机械区分，npc 不做归因判断，只记录复现事实）。npc MUST 确定性检测该复现条件；复现结果 MUST NOT 落盘为新的 state 字段（与"连续计数"一样，只从 `entry["phases"]` 现场重算，spec_report 侧同样现场重算，不读取任何持久化证据字段），并在复现发生的当轮通过"重算前后差集"即时发一条 telemetry 事件（`category_recurrence_after_sweep_claim`）。
- **`spec_report.py::_verify_categories_scanned` 新增 `unsubstantiated` verdict**：区别于现有 `ok`/`warn`/`unverifiable`——现有实现只能对比 category 名集合是否覆盖，验证不了"覆盖是否属实"；`unsubstantiated` 表示该轮扫描自报存在后续复现信号、未被证实，是一个更强的负面结论，但不等同于"被证明为假"。
- **存在复现信号的 category 在下一轮 fix prompt 强制穷举清单**：无论其当时的连续计数是否达到阈值，只要被标记为 `unsubstantiated`，下一轮 fix prompt MUST 对该 category 强制要求穷举落点清单。
- 阈值可配置：TOML `[coder]` 节新增键，默认 2；不新增 CLI flag，不进 `openspec/project.md`。

## Impact

- `src/npc/templates.py`（`render_fixer` 渲染逻辑与修复规则 A 文案）
- `src/npc/coder.py` / `src/npc/agent.py`（两处独立的 `render_fixer` 调用点，均需接入新的连续计数 + unsubstantiated category 派生）
- `src/npc/pipeline.py`（review phase-exit 的 mutate 闭包：新增复现检测（重算前后差集）与 telemetry 事件发出；MUST NOT 新增任何持久化复现字段，判定结果一律现场重算，不落盘）
- `src/npc/config.py`（`CoderConfig` 新增可配置阈值键）
- `src/npc/spec_report.py`（`_verify_categories_scanned` 新增 `unsubstantiated` verdict）
- `src/npc/telemetry.py`（`EMIT_FIELD_CONTRACT` 登记新 telemetry kind）
- 新增 openspec capability `coder-category-streak-sweep`；`spec-report` capability 增补一条 MODIFIED Requirement

## Non-Goals

- 不收紧 `schema.py` 的 category 枚举为封闭集（保持自由文本 + 精确字符串匹配，接受措辞漂移导致的漏报作为已知限制）。
- 不新增任何 LLM 调用去"理解"或"归并"category 语义。
- 不改变 review 判定权——npc 仍只把计数事实和复现事实摆给 coder，清单是否真的做全，仍由下一轮独立 review 判定。
- 不新增 CLI flag、不写入 `openspec/project.md`。
