# plan-complexity-gate

## ADDED Requirements

### Requirement: plan 前置软性复杂度告警

`npc plan` 前置门 SHALL 对每个 change 计算复杂度信号——**主信号**为跨领域广度（touched 路径覆盖的顶层模块/目录数，或 spec 中 requirement 条目数），**辅信号**为文件数——超过可配阈值时输出结构化 warning `{change_id, breadth, files, suggestion}`，`suggestion` ∈ {split, large}。该门 MUST NOT 自动拆分 change，MUST NOT 阻断 run。阈值定义于 `[review]` 配置，带默认值与整数校验。

#### Scenario: 大跨领域 change 触发告警

- **WHEN** 某 change 的 touched 路径跨越多个顶层模块且超阈值
- **THEN** `npc plan` 输出该 change 的 warning，含 breadth 与 split|large 建议，run 继续

#### Scenario: 大但单领域 change 不误伤

- **WHEN** 某 change 触及 11 个文件但全在同一模块（如 `src/npc/`），跨领域广度未超阈值
- **THEN** 不输出复杂度 warning

#### Scenario: 软门不阻断

- **WHEN** change 触发复杂度告警
- **THEN** run 不被阻断、change 不被自动拆分，后续 plan/implement 正常进行

### Requirement: large 标记抬高 review-fix 轮数预算

被标记 `large` 的 change SHALL 在 plan-state 中携带该标记，其 review-fix 循环上限读取可配的更高预算（`[review].max_rounds_large`）；非 large change 的上限不变。

#### Scenario: large change 获得更高轮数上限

- **WHEN** change 标记为 large 且配置了 `max_rounds_large`
- **THEN** 该 change 的 review-fix 循环上限为 `max_rounds_large` 而非默认值

#### Scenario: 非 large change 上限不变

- **WHEN** change 未标记 large
- **THEN** 其 review-fix 上限沿用默认值
