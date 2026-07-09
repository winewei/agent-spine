## ADDED Requirements

### Requirement: coder 契约显式禁止 stub 充数与删测换 pass

coder 的 agent 契约 MUST 在其 Guardrails 约束中显式包含两条独立规则：(1) 禁止以 stub / 占位实现（如空函数体、恒定返回值、未覆盖核心逻辑的简化分支）充数勾选任务完成；(2) 禁止删除、注释掉或 skip 任何既有测试，也禁止以任何方式弱化既有测试（如放宽断言范围、移除关键覆盖点、跳过关键分支）以换取测试全部通过的结果。这两条 MUST 作为 coder 每次任务都会读取的项目级契约的一部分，而非仅出现在某次任务的临时 prompt 中。

#### Scenario: Guardrails 含禁止 stub 充数的规则

- **WHEN** 读取 coder 的 agent 契约文档
- **THEN** 其 Guardrails 段包含一条禁止用 stub / 占位实现充数勾选 task 的规则

#### Scenario: Guardrails 含禁止删测换 pass 的规则

- **WHEN** 读取 coder 的 agent 契约文档
- **THEN** 其 Guardrails 段包含一条禁止删除、注释或 skip 既有测试以换取测试通过结果的规则

#### Scenario: Guardrails 含禁止弱化既有测试的规则

- **WHEN** 读取 coder 的 agent 契约文档
- **THEN** 其 Guardrails 段包含一条禁止以放宽断言范围、移除关键覆盖点、跳过关键分支等方式弱化既有测试以换取测试通过结果的规则

### Requirement: reviewer 审查重点显式将 stub 与测试弱化列为 blocking 判据

reviewer 侧的审查重点 MUST 显式声明：出现 stub / 占位实现，或既有测试被删除、注释掉、skip、或断言被弱化（如范围被放宽、关键分支被跳过）导致测试通过，均 MUST 被视为 blocking 级别的问题。该判据 MUST 同时应用于 Round 0（首轮）与 Round N（后续轮次）的审查重点，且两者的判据文案 MUST 来自同一来源，不得出现不一致或遗漏。

#### Scenario: Round 0 审查重点含 stub / 删测判据

- **WHEN** 渲染 Round 0 的审查重点文本
- **THEN** 文本中包含"stub / 占位实现"与"测试被删除或弱化"均为 blocking 的判据说明

#### Scenario: Round N 审查重点同样含该判据

- **WHEN** 渲染 Round N（N ≥ 1）的审查重点文本
- **THEN** 文本中包含与 Round 0 一致的"stub / 占位实现"与"测试被删除或弱化"均为 blocking 的判据说明

#### Scenario: 两轮判据文案同源不漂移

- **WHEN** 分别渲染 Round 0 与 Round N 的审查重点文本
- **THEN** 两者关于 stub / 测试弱化的判据措辞完全一致（逐字相同），不存在两份独立维护、可能各自漂移的版本

### Requirement: 多段自我辩护式注释视为可疑信号

当实现中出现需要多段注释为该实现的合理性进行自我辩护的模式时，reviewer 审查重点 MUST 将其列为可疑信号，提示 reviewer 核实该实现是否用辩护性注释掩盖了 stub / 占位实现或被弱化的测试覆盖，而非直接认定为合规。

#### Scenario: 审查重点提示核实自我辩护式注释

- **WHEN** 渲染 Round 0 或 Round N 的审查重点文本
- **THEN** 文本中包含"需要多段注释自我辩护的实现视为可疑信号，需核实是否掩盖 stub 或测试弱化"这一提示

### Requirement: 判据不改变既有 blocking 结构化契约

本能力新增的判据 MUST NOT 引入新的 review 输出 schema 字段或新的 `category` 枚举值；stub / 占位实现与测试弱化相关的 finding MUST 仍通过既有的 `severity` / `category` / `in_scope` 字段表达。

#### Scenario: 输出契约保持不变

- **WHEN** reviewer 依据新增判据报告一条 stub 相关的 finding
- **THEN** 该 finding 的结构仍符合既有的 review 输出契约，未出现契约之外的新增字段
