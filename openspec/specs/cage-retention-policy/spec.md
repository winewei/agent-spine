# cage-retention-policy Specification

## Purpose
TBD - created by archiving change reduce-review-fix-cost. Update Purpose after archive.
## Requirements
### Requirement: 保留低触发但守真实故障类的笼子

`verify-tests-rerun` 笼子（映射 `phase.exit` + `outcome_reason=="rerun-tests-failed"`）SHALL 保留，MUST NOT 因低/零触发率被删除。其 `_CAGE_DEFS` 条目 MUST 携带持久的 `retained` 标注与保留理由备注（守"coder 声称 tests pass、rerun 打脸"的真实故障类；成本≈0，仅在真失败时 fire）。`retained` 标注是人在回路裁决的产物，不是自动判定。

#### Scenario: 保留笼子不因零触发删除

- **WHEN** `verify-tests-rerun` 在 `runs_observed ≥ 阈值` 的窗口内触发 0 次
- **THEN** 该笼子仍保留，其定义处存在 `retained` 标注与理由备注

### Requirement: deletion_candidates 排除已裁决保留的笼子

`npc telemetry cages` 的 `deletion_candidates` 计算 SHALL 排除标注为 `retained` 的笼子，避免每次分析反复推荐删除同一个已裁决保留的守卫。该排除仅作用于 `retained` 笼子；`no_data`（事件未接线）笼子仍不可当删除候选，行为不变。

#### Scenario: retained 笼子不出现在 deletion_candidates

- **WHEN** 运行 `npc telemetry cages` 且 `verify-tests-rerun` 已标 `retained`
- **THEN** 输出的 `deletion_candidates` 不含 `verify-tests-rerun`

#### Scenario: no_data 笼子行为不变

- **WHEN** 其余 12 个 `no_data` 笼子（事件未接线）参与计算
- **THEN** 它们仍不被列为删除候选，与本策略无关

