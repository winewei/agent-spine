## Why

线上观察到 coder 侧存在「用 stub / 占位实现勾掉 task」「删除或弱化既有测试来换取 tests=pass」两类应试行为，且当出现这类实现时，coder 常在代码里堆砌大段注释自我辩护（试图说服 reviewer 该简化是合理的）。当前 harness 的三处防线都没有专门堵这个口子：spine-coder 的 agent 契约 Guardrails 未显式禁止；coder 提交前的通用自检类目（`SELFCHECK_CATEGORIES`）没有对应类目；reviewer 侧的审查重点也没有把「stub/占位实现」「测试被删除或弱化」列为明确的 blocking 判据。三处若只补一处，另外两处仍是漏洞——coder 侧不知道这是红线，reviewer 侧也没有被显式要求把它当 blocking 处理。

## What Changes

- **MODIFIED** spine-coder agent 契约的 Guardrails 段新增两条硬性禁止：禁止以 stub / 占位实现充数勾选 task；禁止删除、注释掉或 skip 任何既有测试来换取 `tests=pass`。
- **MODIFIED** coder 提交前自检类目清单新增 `no-stub` 类目及对应自查要点（类目名可与 reviewer 侧同源，但自查文案与 reviewer 判据文案不共享，遵守不变量 1）。
- **MODIFIED** review focus 模板（Round 0 与 Round N 共享同一来源）的审查重点新增显式 blocking 判据：stub / 占位实现视为 blocking；被删除、注释掉或弱化（如断言被放宽、关键分支被 skip）的既有测试视为 blocking；需要多段注释为实现合理性自我辩护，视为可疑信号，reviewer 须核实其是否掩盖了 stub 或删测。

## Capabilities

### New Capabilities

- `coder-no-stub-guardrails`：coder 生成侧的 Guardrails 契约与 reviewer 验证侧的 blocking 判据，共同构成「反 stub / 反删测」的双端约束。

### Modified Capabilities

- `implement-selfcheck-rubric`：现有的静态通用自检类目清单新增 `no-stub` 类目，要求 coder 提交前自查是否存在占位实现或被削弱的测试。

## Impact

- **受影响代码**：`plugins/agent-spine/agents/spine-coder.md`（Guardrails 段）、`src/npc/templates.py`（`SELFCHECK_CATEGORIES`、`SELFCHECK_RUBRIC_MD`）、`src/npc/focus.py`（Round 0 / Round N 共享的审查重点文案）、`tests/`。
- **兼容性**：`SELFCHECK_CATEGORIES` 新增一项不改变既有类目的语义或顺序；`SELFCHECK_RUBRIC_MD` 追加一行不影响既有类目的匹配（沿用既有测试对「每个类目名出现在 rubric 文本中」的断言模式）。review focus 文案追加内容不改变现有 JSON 输出契约（`REVIEW_SCHEMA` 不变，仍是 `category` 自由文本，`stub`/`test-deletion` 不作为新枚举值强推）。
- **不变量影响**：
  - 不变量 1（生成⊥验证）：coder 侧自检类目与 reviewer 侧 blocking 判据**类目层级可同源**（都叫「反 stub」），但**具体文案不共享**——coder 侧只见到"是否有占位实现/被削弱的测试"这类通用提醒，reviewer 侧的判据文案（含「需要多段注释自我辩护视为可疑」这类更细的启发式）不回流进 implement/fix prompt。延续本仓库既有的 `implement-selfcheck-rubric` 能力已裁定的同一边界。
  - 不变量 2（不信 LLM 散文）：本 change 不改变 `REVIEW_SCHEMA` 的结构化契约，findings 仍以既有字段（`severity`/`category`/`in_scope` 等）表达；「stub/占位」「测试被削弱」是审查重点文案里的**判定标准**，最终仍必须落在既有结构化字段上，不新增自由文本信道。
  - 不变量 3（新硬轨需被真实方差打出来）：本 change 由已发生的线上应试行为（用户原始目标中描述的观察）驱动，不是预先过度设计；不新增任何自动化阻断机制或退出码变化——它只是让 coder 契约与 reviewer 判据显式覆盖这一类问题，最终是否 blocking 仍由 reviewer 的 LLM 判断给出，不引入新的确定性 gate。

## Non-Goals

- 不新增任何自动化检测（如静态扫描找 "TODO"/"pass  # stub" 之类字符串）来机械识别 stub；判定仍完全依赖 reviewer 的人工/LLM 判断，本 change 只是显式化判据文案。
- 不改变 `REVIEW_SCHEMA` 的字段结构或新增枚举值（如不给 `category` 加 `stub` 专属枚举）。
- 不改变 `blocking` 的既有判定规则（仍为 `severity ∈ {critical, high}` 且 `in_scope`）。
- 不改变 `npc fixer findings` 渲染给 coder 的内容或结构。
- 不涉及 `SELFCHECK_CATEGORIES` 中已有类目（`validation`/`partial-failure`/`locking`/`test-coverage`/`edge-case`/`telemetry`/`concurrency`）的语义调整。
- 不针对 MiMo / codex 等具体后端做差异化处理；Guardrails 与判据对所有 coder/reviewer 后端一视同仁。
