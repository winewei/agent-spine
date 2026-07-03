# implement-selfcheck-rubric Specification

## Purpose
TBD - created by archiving change reduce-review-fix-cost. Update Purpose after archive.
## Requirements
### Requirement: implement/fix prompt 注入静态通用自检 checklist

`npc agent` 渲染 implement 与 fix prompt 时 SHALL 注入一份**change 无关的静态**提交前自检类目清单（反复出现的 blocking 维度：validation / partial-failure / locking / test-coverage / edge-case / telemetry / concurrency 等），要求 coder 提交前逐条自查。该清单 MUST 来自单一事实源常量，implement 与 fix 两处引用同一份。

#### Scenario: implement prompt 含通用 checklist

- **WHEN** `npc implement run` 渲染 spine-coder 的 implement prompt
- **THEN** prompt 含静态自检类目清单，coder 被要求提交前逐条自查

#### Scenario: fix prompt 同样含通用 checklist

- **WHEN** `npc fix run` 渲染某一轮 fix prompt
- **THEN** prompt 含同一份静态自检类目清单

### Requirement: 严守生成 ⊥ 验证边界，不注入 per-change review 判据

implement/fix prompt 的自检 checklist MUST NOT 包含 `npc focus` 为当次 change 渲染的 review focus 文本、上一轮 review 的具体 findings 原文、或 reviewer 的评分 rubric 细则。coder 侧只见通用类目层级，reviewer 侧见当次 change 的具体判据；二者不得共享 per-change 文本（守核心不变量 1「生成 ⊥ 验证」，防 coder 应试与 reviewer 独立性丧失）。

#### Scenario: prompt 不含当次 change 的 review focus

- **WHEN** 渲染 implement 或 fix prompt
- **THEN** prompt 不含 `npc focus` 的 per-change 渲染文本，也不含上轮 review 的具体 findings 原文

#### Scenario: 类目命名同源但内容层级分离

- **WHEN** 对比 coder 自检 checklist 与 reviewer 的 review focus
- **THEN** 二者类目名可同源，但 coder 侧仅通用提醒、reviewer 侧为当次具体判据，不共享 per-change 文本

