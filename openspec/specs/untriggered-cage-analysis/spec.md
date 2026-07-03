# untriggered-cage-analysis Specification

## Purpose
TBD - created by archiving change analyze-untriggered-cages. Update Purpose after archive.
## Requirements
### Requirement: 硬轨触发次数可跨 run 统计且 0 触发笼子成为减法候选

`npc telemetry` MUST 提供 cages 统计：按硬轨维度（stale、max-rounds、timeout-budget、verify routing/tests、auto-decide 各 trigger）输出跨 run 触发次数的单行 JSON，并区分 `untriggered`（有数据源但 0 触发）与 `no_data`（事件从未 emit）。`/spine-analyze` MUST 把观察窗口足够的 untriggered 笼子列为候选删除项写入优化建议（只建议、不自动删）。

#### Scenario: 0 触发笼子被列为删除候选

- **WHEN** 近 90d 有 ≥ 阈值个 run，stale 闸门触发 12 次而 max-rounds 触发 0 次（且相关事件有数据源）
- **THEN** `npc telemetry cages` 输出 max-rounds ∈ `untriggered`
- **AND** `/spine-analyze` 在优化建议中把 max-rounds 列为候选删除项（附依据与验证方式），不动代码

#### Scenario: 缺数据不误判为可删

- **WHEN** 某笼子（如 routing violation）的 telemetry 事件尚未接线、事件流中从无该 kind
- **THEN** 该笼子归入 `no_data` 而非 `untriggered`，不进入删除候选

#### Scenario: 常触发笼子正常计数

- **WHEN** auto-decide 在窗口内做过多次 skip/force-archive 决策
- **THEN** cages 输出各 trigger 的准确计数，供 hotspot 分析交叉引用

