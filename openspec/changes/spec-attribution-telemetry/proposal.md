## Why

要让 harness 的 logs 反过来迭代 spec 的写法，必须先有一把**尺子**：能回答「这条 code review blocking finding，是因为 spec 没说清，还是 spec 说清了实现没照做」。

当前 telemetry 回答不了。`review.round` 事件携带的 `blocking_categories` 是**代码缺陷类目**（`validation` / `error-handling` / `edge-case`…，见 `src/npc/schema.py` 的 `REVIEW_SCHEMA`），与 spec 质量正交。

一次人工归因（抽样 4 个 change 的 12 条 r0 blocking）得出「约 4 条可归因 spec 缺口、约 8 条是实现未照已明确的 spec 执行」，但该结论**不可用作基线**，原因是分母有检出偏差：spec 含糊导致的缺陷恰恰最难被 reviewer 抓到（无明文可比对），而「实现偏离 spec」最容易被抓到（有明文可比对）。人工归因既不可持续，也系统性低估 spec 的责任。

**先造尺子，再造被量的东西。** 本 change 只做度量，不做闸门。

同时必须修一个**潜伏 bug**：`ensure_schema()`（`src/npc/schema.py`）仅在 schema 文件缺失时写盘。`~/task_log/.new-plan-review-schema.json` 早已存在，因此**任何对 `REVIEW_SCHEMA` 的代码修改都不会传达给 codex**。不先修它，本 change 加的字段将静默失效。

## What Changes

- **BREAKING（修 bug）** `ensure_schema()` 由「仅缺失时写入」改为「内容与 `REVIEW_SCHEMA` 不一致时重写」，使 schema 演进可以真正生效。
- **MODIFIED** `REVIEW_SCHEMA` 的 finding 对象新增必需字段 `spec_attribution`，枚举为 `spec-silent` / `spec-ambiguous` / `spec-contradicted` / `impl-deviation`。
- **MODIFIED** `parse_review()` 派生 `spec_attribution_counts`，且对**缺失该字段的历史 review.json 保持向后兼容**（计入 `unknown`，不抛异常）。
- **MODIFIED** `review.round` telemetry 事件新增 `spec_attribution_counts` 字段（同步更新 `EMIT_FIELD_CONTRACT` 与 `telemetry_schema_v1.json`）。
- **MODIFIED** `npc telemetry agg` 聚合 `spec_attribution_counts`，输出 `spec_attributable_blocking_rate`。
- **新增** 测试覆盖：新枚举、向后兼容、schema 重写幂等性、聚合正确性、以及守不变量 1 的负向断言。

**非目标（Non-Goals）**：

- **不引入任何基于 `spec_attribution` 的闸门、阈值或阻断行为。** 本 change 是纯度量。按不变量 3，任何硬轨须先由本 change 产出的 telemetry 方差打出来。
- 不修改 `blocking` 的判定逻辑（仍为 `severity ∈ {critical, high}` 且 `in_scope`）；`spec_attribution` **不参与** blocking 判定。
- 不给 `category` 字段补 enum 约束（独立问题，另开 change）。
- 不改变 `npc fixer findings` 交给 coder 的 findings 内容。
- 不做跨 change 的 spec 质量评分或排名。

## Capabilities

- **New Capabilities**: `spec-attribution` —— 由验证方（reviewer）对每条 code review finding 给出结构化的 spec 归因枚举，并沿 telemetry 链路聚合，成为迭代 spec 写法的唯一可信信号源。

## Impact

- **受影响代码**：`src/npc/schema.py`（`REVIEW_SCHEMA`、`ensure_schema`）、`src/npc/review.py`（`parse_review`）、`src/npc/pipeline.py`（review.round emit）、`src/npc/telemetry.py`（`EMIT_FIELD_CONTRACT`、`aggregate`）、`src/npc/telemetry_schema_v1.json`、`tests/`。
- **兼容性**：
  - 历史 `review.json`（无 `spec_attribution`）MUST 继续可被 `parse_review` 解析。
  - 历史 telemetry 事件（无 `spec_attribution_counts`）MUST 继续可被 `aggregate` 消费。
  - `ensure_schema` 的行为变更会在首次运行时重写用户既有的 `~/task_log/.new-plan-review-schema.json`。这是**期望行为**（该文件是派生产物，非用户资产）。
- **不变量影响**：
  - 不变量 1（生成⊥验证）：`spec_attribution` 由**验证方**产出，**不得**回流到任何生成侧 prompt。`npc fixer findings`（fix 轮喂给 coder 的 findings 渲染）MUST NOT 包含该字段。**不违反**。
  - 不变量 2（不信 LLM 散文）：归因是**四值枚举**而非自由文本，且由 JSON Schema 强制。**满足**。
  - 不变量 3（新硬轨须被真实方差打出来）：本 change **不新增任何硬轨**，只新增度量。它正是为后续硬轨提供方差证据的前置条件。**满足**。
- **已知局限（必须记录，不得在后续被当作事实）**：`spec_attribution` 是 reviewer 的 LLM 判断，与人工归因共享同一检出偏差——它只能归因**已被检出的** finding，无法度量「因 spec 含糊而从未被任何人发现」的缺陷。因此 `spec_attributable_blocking_rate` 是**下界**，不是真值。
