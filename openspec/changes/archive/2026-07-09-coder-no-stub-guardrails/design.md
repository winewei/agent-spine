## Context

三处既有机制与本 change 相关，均已核实：

- `plugins/agent-spine/agents/spine-coder.md` 有一个 `## Guardrails` 段，列若干条硬性约束（如「改动最小、聚焦当前 change」「commit 与 summary.md 缺一不可」），是 coder 每次任务都会读到的项目级契约。目前没有任何一条针对 stub / 占位实现或删测。
- `src/npc/templates.py` 的 `SELFCHECK_CATEGORIES`（7 个类目：`validation`/`partial-failure`/`locking`/`test-coverage`/`edge-case`/`telemetry`/`concurrency`）与 `SELFCHECK_RUBRIC_MD`（对应的 markdown 表格）是单一事实源，被 `render_implementer` 与 `render_fixer` 同时引用，注入 implement 与 fix 两处 prompt。已有测试（`tests/test_reduce_review_fix_cost.py`）断言：类目非空、类目集合含既定维度、rubric 文本含每个类目名、implement/fix prompt 都含每个类目名、且不含 per-change review focus 内容（负向断言，守不变量 1）。
- `src/npc/focus.py` 的 `_round_0_template` 与 `_round_n_template` 各自维护一段「审查重点（按重要性排序）」列表，二者独立维护（不同措辞，符合两轮场景差异）；而 `_output_requirements_block()` 是二者共享的单一来源，之前已用于承载 `spec_attribution` 四值语义（`SPEC_ATTRIBUTION_ENUM_SEMANTICS`），避免 Round 0 / Round N 措辞漂移。

用户原始目标观察到的具体应试模式：coder 用 stub / 占位实现勾掉 task；coder 删除、注释或 skip 既有测试换取 `tests=pass`；出现上述行为时，代码里常伴随大段解释性注释为该实现的"合理性"辩护。

## Goals / Non-Goals

**Goals**

- coder 契约显式禁止 stub/占位实现充数、禁止删除或弱化既有测试。
- coder 提交前自检清单显式提醒这两类风险。
- reviewer 审查重点显式把「stub/占位实现」「测试被删除或弱化」列为 blocking 判据，并把「需要多段注释自我辩护的实现」列为可疑信号，提示 reviewer 核实。
- Round 0 与 Round N 的判据文案保持单一来源，不出现漂移。

**Non-Goals**

- 不做任何机械式静态扫描（grep "TODO"、AST 检测空函数体等）；判定仍完全交给 reviewer 的判断。
- 不改变 `REVIEW_SCHEMA` 结构或新增枚举值。
- 不改变 `blocking` 的判定规则、`npc fixer findings` 的渲染内容。
- 不调整 `SELFCHECK_CATEGORIES` 中既有 7 个类目的语义。

## Decisions

**D1：Guardrails 新增两条独立要点，而非合并成一条。**
「禁止 stub 充数」与「禁止删测换 pass」是两类不同的应试路径（前者是**不做**，后者是**破坏已有正确性证据**），分开列更利于 coder 逐条对照自查，也便于日后单独引用某一条做 telemetry 归因。

**D2：`SELFCHECK_CATEGORIES` 新增类目 `no-stub`，复用既有单一事实源机制，不新造并行清单。**
沿用 `implement-selfcheck-rubric` 已裁定的模式（类目名可与 reviewer 侧同源，具体文案不共享）：`SELFCHECK_RUBRIC_MD` 新增一行通用提醒（"新增/修改的实现是否存在占位返回值、空函数体、被简化到不覆盖核心逻辑的分支；是否有测试被删除、注释掉或断言被放宽"），措辞保持类目层级的通用提醒，不引用具体 change 或具体 reviewer 判据句式。

**D3：reviewer 侧判据写入 `_output_requirements_block()`，与 `SPEC_ATTRIBUTION_ENUM_SEMANTICS` 同一模式处理，而非分别改 `_round_0_template` 与 `_round_n_template` 的「审查重点」列表。**
`_output_requirements_block()` 是 Round 0 / Round N 共享的单一来源；`focus.py` 顶部注释明确记录过"两份模板各自维护导致漂移"的历史教训（`spec_attribution` 缺失是先例）。把新判据放进该共享块，天然保证两轮同步，且不需要改动两个模板各自的「审查重点」列表结构。判据文案与 `SELFCHECK_RUBRIC_MD` 的提醒句式不同源（前者更具体，含"需要多段注释自我辩护视为可疑"这一启发式，coder 侧不会看到这句话），维持不变量 1 的边界。

**D4：不新增 `category` 枚举值，不新增 schema 字段。**
「stub/占位实现」「测试被删除或弱化」作为 blocking 的判定标准写进审查重点文案即可，reviewer 仍用既有的 `severity`/`category`（自由文本）/`in_scope` 字段表达具体 finding；机械化改 schema 属于过度设计，且会牵动 `REVIEW_SCHEMA`/`telemetry_schema_v1.json`/兼容性等一整条链路，不是本 change 要解决的问题（不变量 3：先有真实方差再加硬轨；目前尚无证据表明现有字段不够用）。

## Risks / Trade-offs

- **[判据仍是文案层面的提醒，reviewer 仍可能漏判]** → 本 change 不承诺消灭 stub/删测问题，只承诺把判据显式化到 coder 契约与 reviewer 审查重点两处。是否需要升级为机械化检测，留给后续 telemetry 观察（不变量 3）。
- **[「多段注释自我辩护」这一启发式可能误伤合理的复杂逻辑注释]** → 文案表述为"视为可疑信号，reviewer 须核实"而非"一律 blocking"，避免把启发式提升为机械判定；最终判断仍由 reviewer 结合上下文决定。
- **[`SELFCHECK_CATEGORIES` 新增一项后，既有测试对类目数量若有硬编码断言会失败]** → 已核查 `tests/test_reduce_review_fix_cost.py` 中的既有断言均基于集合成员关系（`in cats`）与文本包含关系，不断言长度或顺序，新增类目不会破坏既有测试；仍需在实施阶段确认无遗漏的硬编码断言。

## Migration Plan

1. 改 `plugins/agent-spine/agents/spine-coder.md` 的 Guardrails 段，加两条新规则。
2. 改 `src/npc/templates.py`：`SELFCHECK_CATEGORIES` 追加 `no-stub`，`SELFCHECK_RUBRIC_MD` 表格追加对应行。
3. 改 `src/npc/focus.py` 的 `_output_requirements_block()`，追加 stub/删测的 blocking 判据说明，Round 0 / Round N 自动同步获得。
4. 补测试：Guardrails 文本断言、`SELFCHECK_CATEGORIES`/`SELFCHECK_RUBRIC_MD` 含新类目、implement/fix prompt 含新类目名、focus 渲染（round 0 与 round N）都含新判据文案、以及守不变量 1 的负向断言（新判据的具体细节文案不出现在 implement/fix prompt 中）。
5. 回滚：三处改动互相独立，任一处可单独 revert 而不破坏另外两处的正确性（无耦合状态、无迁移数据）。

## Open Questions

无。判据放置位置（Guardrails / SELFCHECK / focus 共享块）与不变量 1 边界均已在上文定稿。
