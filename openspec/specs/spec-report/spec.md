# spec-report Specification

## Purpose
TBD - created by archiving change spec-report-per-change. Update Purpose after archive.
## Requirements
### Requirement: archived 成功后派生 per-change 三产物

`npc spec-report render --seq <N>` MUST 从 `STATE_JSON` + `_telemetry` + git 派生单个 change 的报告，产出三份**同源**产物：`spec-report.json`（全量契约源）、`spec-report.md`（由派生对象渲染的人读视图）、一条 `kind=spec.report` 的 telemetry 事件（仅含 `common_metrics` 子集 + pointer，不复制全量报告）。spine-run 主循环 MUST 在该 change archived 成功后调用一次；对**非 archived 终态**（failed / skipped-auto / needs-user-decision）MUST NOT 生成报告。

命令对**非法输入**（`--seq` 不存在、目标 change 非 archived、state 损坏/缺失）MUST 返回单行 `ok:false` JSON 与稳定 error code，MUST NOT 产出半成品产物。

#### Scenario: 单 change archived 后三产物落盘

- **WHEN** change #2 archived 成功后主循环调用 `npc spec-report render --seq 2`
- **THEN** 该 change 的 log base 下新增 `spec-report.json` 与 `spec-report.md`
- **AND** `_telemetry` 中新增一条 `kind=spec.report` 事件
- **AND** stdout 返回单行 `ok:true` JSON，含三产物的 pointer 路径

#### Scenario: 非 archived 终态不生成

- **WHEN** 对一个 status=failed 或 skipped-auto 的 change 调用 `npc spec-report render --seq <N>`
- **THEN** 返回 `ok:false`，error 指明「非 archived 终态不生成 delivery receipt」
- **AND** 不写出任何 `spec-report.*` 产物

#### Scenario: 非法 seq 稳定失败

- **WHEN** `--seq` 指向不存在的 change
- **THEN** 返回单行 `ok:false` JSON 与稳定 error code，MUST NOT 抛栈、MUST NOT 产出半成品文件

#### Scenario: common_metrics 三视图一致

- **WHEN** 同一 change 渲染完成
- **THEN** 被列为 `common_metrics` 的字段（终态、review 轮数、fix 轮数、blocking_trend、total_duration_ms、estimated_tokens 合计、自报核验汇总结论）在 `spec-report.json`、`spec-report.md`、telemetry 事件三处取值一致
- **AND** md 与 telemetry 允许只呈现子集，但呈现的字段值不得与 json 矛盾

### Requirement: 报告内容维度

`spec-report.json` MUST 覆盖：交付结果（commit chain：implement + 各 fix 轮 + archive、终态 status）、收敛质量（review 轮数、blocking_trend 序列、`one_shot`）、返工画像（category 分布 + 每类 fix 轮数）、耗时（各 phase duration + 总时长）、资源（`estimated_tokens_by_backend`——沿用 `cost` 的 heuristic 口径，标注为估算；**不含**货币费用字段）、自报核验（见下）、叙事（headline + notes）。

`one_shot` MUST 依据 review 数据判定：存在首轮 review 且其 blocking=0（`blocking_trend` 首元素为 0）时为 `true`；有 fix 轮时为 `false`；缺 review 数据时为 `null`（不臆断）。

`spec-report.md` MUST 以重点优先的简要形式呈现上述维度，且 MUST 满足可测约束：不得包含任何 `*.summary.md` 的 phase 原始流水账原文；MUST 含固定的指标标题段（终态 / 收敛 / 返工 / 耗时 / 资源 / 自报核验 / 叙事）；总行数 MUST 不超过约定上限（实现设定并在测试固定，如 ≤ 80 行）。

#### Scenario: json 维度齐全

- **WHEN** 一个经历 2 轮 fix 后 archived 的 change 被渲染
- **THEN** `spec-report.json` 含 commit chain（implement + fix-r1 + fix-r2 + archive）、review 轮数、blocking_trend、各 category 及其 fix 轮数、各 phase 耗时与总时长、`estimated_tokens_by_backend`、`one_shot=false`、叙事字段

#### Scenario: md 简要且可测

- **WHEN** 校验 `spec-report.md`
- **THEN** 含全部固定指标标题段，总行数不超过上限
- **AND** 不含任何 phase summary 原文片段

#### Scenario: 一次过的 change

- **WHEN** 一个首轮 review blocking=0、0 轮 fix 直接 archived 的 change 被渲染
- **THEN** 报告 `one_shot=true`，返工画像为空，blocking_trend 首元素为 0

#### Scenario: 缺 review 数据不臆断 one_shot

- **WHEN** 一个 change 无可用首轮 review blocking 数据
- **THEN** `one_shot=null`，MUST NOT 记为 true

### Requirement: 自报核验（确定性）

报告 MUST 含「自报 vs 观测」小节，对 coder 每个 `fix-rN` 轮的自报做确定性对账：

- `regressions_added`：以该 fix 轮的 commit range 为准，判定 diff 是否触及测试文件（测试文件识别用**通用启发式**——路径/文件名含 `test`/`spec` 等；缺 commit range 时不判定）。声明了但该轮 diff 未触及测试文件 → `warn`；一致 → `ok`；缺 commit/range/自报数据 → `unverifiable`。
- `categories_scanned`：以整个 change 聚合，与该 change 实际出现的 review category（来源 MUST 明确为 `categories_seen`）对照。自报覆盖了全部实际出现 category → `ok`；有遗漏 → `warn`；自报缺失 → `unverifiable`。

核验 MUST 为纯确定性（不调 LLM、不 spawn agent）；数据不足以判定时 MUST 标 `unverifiable`，MUST NOT 误报为 `warn`；MUST NOT 扩展到任何项目特定测试规范。

#### Scenario: 声明加回归测试但该轮 diff 未动测试文件

- **WHEN** 某 fix 轮 RESULT 声明 `regressions_added=[X]`，但该轮 commit range 的 diff 未触及任何测试文件
- **THEN** 该条 verdict=warn（⚠）

#### Scenario: categories_scanned 覆盖实际出现 category

- **WHEN** 自报 `categories_scanned` 覆盖了该 change `categories_seen` 的全部项
- **THEN** 该条 verdict=ok（✓）

#### Scenario: 缺数据不误报

- **WHEN** coder 未提供某项自报，或对应 commit range 不可得
- **THEN** 对应核验条目 verdict=unverifiable，MUST NOT 记为 warn

### Requirement: 渲染容错不阻塞主流程

`spec-report render` 的**产物落盘失败或 telemetry 写入失败**（磁盘/目录不可写等 best-effort 环节）MUST NOT 阻塞 archive 或主循环，MUST NOT 污染主流程 stdout JSON 契约。主循环对本命令的调用 MUST 以非阻塞 wrapper 方式进行，即使命令返回 `ok:false` 也 MUST NOT 回滚已提交的 archive。

（注：非法输入的错误反馈见「archived 成功后派生」需求——那属于命令自身的 `ok:false` 契约，与此处 best-effort 落盘容错是两类。）

#### Scenario: 产物目录不可写

- **WHEN** `spec-report render` 时 log base 或 `_telemetry` 不可写
- **THEN** archive 已提交的结果不受影响，主循环继续
- **AND** render 以非阻塞方式报告失败（stdout 单行 JSON 标记未产出），不抛栈中断主流程

#### Scenario: state 中 base 缺失走既有兜底

- **WHEN** 目标 change 的 state 记录未显式带 `base`
- **THEN** 复用既有 `_paths.base_for(...)` 兜底推导 log base，产物落到与 per-change `events.jsonl`/`*.summary.md` 相同位置

