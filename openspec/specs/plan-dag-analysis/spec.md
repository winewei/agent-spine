# plan-dag-analysis Specification

## Purpose
TBD - created by archiving change parallel-dag-scheduling. Update Purpose after archive.
## Requirements
### Requirement: npc plan dag 产出确定性 DAG 分层
`npc plan dag` SHALL 读取当前 run 的 plan_order，基于两类确定性输入产出分层结果并以单行 JSON 输出 `{ok, layers: [[change_id...], ...]}`：① change 间显式依赖声明（proposal/tasks 中声明的前置 change）；② 各 change 的 tasks.md 与 specs 文件中静态提取的 touched 文件路径。同层内任意两个 change 的路径集合 MUST 无交集且互无依赖。相同输入 MUST 产出相同分层（不调用任何 LLM）。

#### Scenario: 两个不重叠 change 分入同层
- **WHEN** plan_order 含 change-a（touched: src/a.py）与 change-b（touched: src/b.py），互无依赖声明
- **THEN** `npc plan dag` 输出 `layers` 中 a、b 位于同一层

#### Scenario: 有依赖的 change 分入后置层
- **WHEN** change-b 声明依赖 change-a
- **THEN** b 所在层的索引严格大于 a 所在层

#### Scenario: 路径重叠的 change 不同层
- **WHEN** change-a 与 change-b 的 touched 路径集合有交集
- **THEN** 二者 MUST 不在同一层

### Requirement: 信息不足时保守退化为串行
当某个 change 无法提取到任何 touched 路径时，`npc plan dag` SHALL 将该 change 单独成层（不与任何 change 并行）。当全部 change 均如此时，输出 MUST 等价于原 plan_order 的逐个单元素层（完全串行）。

#### Scenario: 无路径信息的 change 单独成层
- **WHEN** change-c 的 tasks/specs 中提取不到任何文件路径
- **THEN** change-c 在 `layers` 中独占一层

### Requirement: 依赖成环或指向未知 change 时退化为串行并报告
当显式依赖声明存在环，或依赖指向不在 plan_order 中的 change 时，`npc plan dag` SHALL 输出完全串行分层（等价原 plan_order 顺序），`ok` 仍为 true，并在输出中报告 `degraded_reason`（cycle / unknown-dep 及涉事 change）。MUST NOT 报错中断 run。

#### Scenario: 依赖环退化串行
- **WHEN** change-a 声明依赖 b、b 声明依赖 a
- **THEN** 输出为完全串行分层，`degraded_reason` 含 cycle 与 [a, b]

#### Scenario: 未知依赖退化串行
- **WHEN** change-a 声明依赖不在 plan_order 中的 change-x
- **THEN** 输出为完全串行分层，`degraded_reason` 含 unknown-dep 与 a→x

### Requirement: 串行判定可解释
`npc plan dag` 的输出 SHALL 为每个未能与他人同层的 change 附 `serialization_reason`（结构化：hotspot 路径清单 / no-paths / cycle / unknown-dep / max-parallel-slice），并输出 `parallelizable_fraction`（size>1 层覆盖的 change 占比）。该诊断 MUST 进 telemetry，供 /spine-analyze 回答"为什么没并行"。

#### Scenario: 热点文件被点名
- **WHEN** change-a 与 change-b 仅因共同触碰 plugins/agent-spine/commands/spine-run.md 而不能同层
- **THEN** 输出中二者的 `serialization_reason` 含 hotspot=spine-run.md

### Requirement: 层大小受 max_parallel 约束
`npc plan dag` SHALL 读取配置 `[scheduler].max_parallel`（默认 3），当某层 change 数超过上限时 MUST 将该层切片为多个不超过上限的连续层。`max_parallel=1` 时输出 MUST 为完全串行分层。

#### Scenario: 超限层被切片
- **WHEN** 某层含 5 个互不重叠的 change 且 max_parallel=3
- **THEN** 输出中该组被切为一层 3 个、一层 2 个

#### Scenario: max_parallel=1 等价串行
- **WHEN** 配置 `[scheduler].max_parallel = 1`
- **THEN** `layers` 为 N 个单元素层，顺序与 plan_order 一致

