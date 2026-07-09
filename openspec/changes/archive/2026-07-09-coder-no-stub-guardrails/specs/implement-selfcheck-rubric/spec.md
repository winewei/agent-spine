## MODIFIED Requirements

### Requirement: implement/fix prompt 注入静态通用自检 checklist

`npc agent` 渲染 implement 与 fix prompt 时 SHALL 注入一份**change 无关的静态**提交前自检类目清单（反复出现的 blocking 维度：validation / partial-failure / locking / test-coverage / edge-case / telemetry / concurrency / no-stub 等），要求 coder 提交前逐条自查。该清单 MUST 来自单一事实源常量，implement 与 fix 两处引用同一份。`no-stub` 类目 MUST 提醒 coder 自查是否存在占位/未实现的返回值或分支、以及是否有既有测试被删除、注释掉或断言被弱化。

#### Scenario: implement prompt 含通用 checklist

- **WHEN** `npc implement run` 渲染 spine-coder 的 implement prompt
- **THEN** prompt 含静态自检类目清单，coder 被要求提交前逐条自查

#### Scenario: fix prompt 同样含通用 checklist

- **WHEN** `npc fix run` 渲染某一轮 fix prompt
- **THEN** prompt 含同一份静态自检类目清单

#### Scenario: 自检清单含反 stub / 反删测类目

- **WHEN** 渲染 implement 或 fix prompt
- **THEN** prompt 的自检类目清单中包含 `no-stub` 类目
- **AND** 该类目的自查要点提示占位实现与被删除/弱化的测试两类风险

### Requirement: 严守生成 ⊥ 验证边界，不注入 per-change review 判据

implement/fix prompt 中**静态自检 checklist 本体**（即本 change 新增/修改的通用类目清单文案，含各类目的自查要点措辞）MUST NOT 包含 `npc focus` 为当次 change 渲染的 review focus 文本、或 reviewer 的评分 rubric 细则。coder 侧的 checklist 只见通用类目层级，reviewer 侧见当次 change 的具体判据；二者不得共享 per-change 文本（守核心不变量 1「生成 ⊥ 验证」，防 coder 应试与 reviewer 独立性丧失）。这一边界同样适用于 `no-stub` 类目：checklist 中该类目的自查要点 MUST 保持通用提醒层级，MUST NOT 包含 reviewer 侧用于判定 blocking 的具体启发式措辞（如「需要多段注释自我辩护视为可疑」）。

本边界仅约束静态自检 checklist 本体的文案来源，不改变 `npc fixer findings` 渲染给 coder 的既有内容或结构——fix prompt 中展示上一轮已签发 findings 原文（供 coder 定位待修复问题）的既有流程不受影响，也不属于本 Requirement 禁止的范围。

#### Scenario: 静态自检 checklist 本体不含当次 change 的 review focus 或 rubric 细则

- **WHEN** 渲染 implement 或 fix prompt
- **THEN** prompt 中的静态自检 checklist 本体（类目清单及其自查要点文案）不含 `npc focus` 的 per-change 渲染文本，也不含 reviewer 评分 rubric 的细则措辞
- **AND** fix prompt 中既有的、供 coder 定位待修复问题的上一轮 findings 原文展示不受本 Requirement 约束，MUST 保留

#### Scenario: 类目命名同源但内容层级分离

- **WHEN** 对比 coder 自检 checklist 与 reviewer 的 review focus
- **THEN** 二者类目名可同源，但 coder 侧仅通用提醒、reviewer 侧为当次具体判据，不共享 per-change 文本

#### Scenario: no-stub 类目不泄漏 reviewer 侧的具体启发式

- **WHEN** 渲染 implement 或 fix prompt
- **THEN** prompt 中的 `no-stub` 自查要点文本 MUST NOT 包含 reviewer 审查重点中用于识别可疑注释的具体措辞
