# coder-category-streak-sweep

## ADDED Requirements

### Requirement: 连续同 category 达阈值触发强制穷举落点清单

渲染某一轮 fix prompt 时，系统 MUST 针对每个在本轮待修复 blocking findings 中出现的 category，从该 change 已落盘的逐轮 review 结果中现场重算其"连续出现轮数"（streak）：从最近一轮 review 起逐轮向前追溯，只要该 category 在该轮 review 判定为 blocking 就计数递增，一旦某轮该 category 未出现即停止追溯（清零重计）。计数 MUST 只使用该 change 已落盘的逐轮确定性数据，MUST NOT 依赖任何自然语言理解或语义归并（category 名以精确字符串匹配判定相同）。

当某 category 的连续出现轮数达到或超过配置阈值时，该 category 对应的 fix prompt 段落 MUST 采用强制穷举格式：逐条列出该 category 涉及的每个已知落点，并标注三态之一——已覆盖 / 新增覆盖 / 确认不可达（附理由）。未达阈值的 category 不受此约束，其 fix prompt 段落格式与本 change 之前的行为一致。

#### Scenario: 连续两轮同 category 达默认阈值

- **WHEN** 某 category 在连续两轮 review 中均被判定为 blocking，且未显式配置阈值（默认阈值为 2）
- **THEN** 渲染下一轮 fix prompt 时，该 category 对应段落要求提供逐条落点清单，且每条须标注"已覆盖/新增覆盖/确认不可达+理由"三态之一

#### Scenario: 中断后清零不触发

- **WHEN** 某 category 在第 1 轮 review 出现、第 2 轮未出现、第 3 轮又出现
- **THEN** 第 3 轮结束后该 category 的连续出现轮数计为 1（不是 3），不达默认阈值 2，不触发强制穷举格式

#### Scenario: 非连续重复仍受既有任意重复规则约束

- **GIVEN** 某 category 在第 1 轮 review 出现、第 2 轮未出现、第 3 轮又出现（连续计数为 1，未达阈值）
- **WHEN** 渲染第 4 轮 fix prompt
- **THEN** prompt 中既有的"category 此前任意轮次出现过即强制枚举全部落点"规则文字保持逐字不变、继续生效
- **AND** 不出现本 change 新增的强制穷举清单格式段落（阈值未达，升级层不触发）

#### Scenario: 未达阈值时 prompt 与现状逐字等价

- **WHEN** 某 change 的所有 category 均未达到配置阈值（含首轮、无历史轮次的情形）
- **THEN** 渲染出的 fix prompt 内容与本 change引入之前的行为逐字等价，不出现强制穷举格式段落

### Requirement: 连续计数与复现判定均只读该 change 自身已落盘的逐轮数据

计算某 category 的连续出现轮数、以及判定某轮自报之后是否出现同 category 复现（见下一条 Requirement），系统 MUST 只读取该 change 在 state 中已落盘的逐轮 review 结果与逐轮 fix 自报字段，MUST NOT 为此目的重新打开或解析任何 `round-*.review.json` 文件。传递给 fix prompt 的信息 MUST 只包含 category 名、连续出现轮数、是否存在复现信号三类结构化事实，MUST NOT 包含任何 review finding 的原始描述、评分依据或评审措辞。

#### Scenario: 计算过程不读取 review.json 原始文件

- **WHEN** 系统计算某轮 fix prompt 的连续计数与复现结果
- **THEN** 该计算过程的输入完全来自该 change 已落盘的逐轮结构化数据，不涉及对任何 `round-*.review.json` 文件的读取

### Requirement: 阈值可配置，默认值为 2

连续出现轮数的触发阈值 MUST 可通过项目级 TOML 配置的 `[coder]` 节配置；未显式配置时默认阈值为 2。系统 MUST NOT 为此新增命令行参数，MUST NOT 将该配置项写入 `openspec/project.md` 一类的项目级约定文档。

#### Scenario: 未配置时使用默认阈值

- **WHEN** 项目未在 `[coder]` 节配置连续阈值
- **THEN** 系统按阈值 2 判定是否触发强制穷举格式

#### Scenario: 显式配置阈值改变触发点

- **WHEN** 项目在 `[coder]` 节显式配置连续阈值为 3
- **THEN** 某 category 连续出现 2 轮时不触发强制穷举格式，连续出现 3 轮时触发

### Requirement: 自报扫描声明的确定性复现检测

一次 review-fix 循环的真实 phase 时序恒为 `review-r(N-1) → fix-rN → review-rN → fix-r(N+1) → review-r(N+1) → …`：`fix-rN` 的直接前驱恒为 `review-r(N-1)`，直接后继恒为 `review-rN`。据此定义"声明之后的复现证据"：若某一轮 `fix-rN` 的自报（`categories_scanned`）中包含某 category，而该 category 在其后**时序上晚于 `fix-rN`** 的 review（即 `review-rM`，M ≥ N；等价于 `review-rN`、`review-r(N+1)`……）中再次以 blocking 身份出现，系统 MUST 确定性地判定该 category 对该轮自报构成"复现"（recurrence），并将该轮自报标记为 `unsubstantiated`（未被证实），MUST NOT 依赖任何 LLM 判断或语义理解。`review-r(N-1)`（时序上早于 `fix-rN`、正是触发该自报的那一轮 review）MUST NOT 被计入复现证据——它是自报产生的原因，不是自报之后的观察结果。

**认识论边界（MUST 在实现与文案中保持一致）**：复现是"上一轮自报未被证实"的强信号，但不是对该轮自报的确定性证伪（proof of falsity）。同 category 的后续 blocking 既可能是同一根因未被真正修透，也可能是修复过程中新引入的、位于不同落点的同 category 问题——在不打开 review 原文、不做语义归因判断的前提下，系统 MUST NOT 机械地区分这两种情况，也 MUST NOT 声称已经"证明"该轮自报为假。系统只确定性记录"复现"这一事实本身，不对复现的根本原因做归因。

系统 MUST 在复现发生的当轮，通过一条可被独立消费的确定性 telemetry 事件（`category_recurrence_after_sweep_claim`）携带完整证据（声明轮次、再次出现的轮次、category 名），供遥测/审计消费方单向消费；复现判定的结果 MUST NOT 落盘为任何新增的持久化字段（与"连续出现轮数"一样，只从该 change 已落盘的逐轮数据现场重算）。该 telemetry 事件 MUST NOT 被视为 `spec_report.py` 判定 `unsubstantiated` verdict 的数据源——`spec_report` 侧的 `unsubstantiated` verdict 一律对 `entry["phases"]` 现场重算得出（见 `spec-report` capability），不依赖、不读取任何 telemetry 已发出的历史事件。

#### Scenario: 后续轮次同 category 再现即标记复现

- **WHEN** 某 category 在第 N 轮 fix（`fix-rN`）的自报 `categories_scanned` 中被列出，且在其后第 M 轮（M ≥ N，即时序上晚于 `fix-rN` 的某轮 `review-rM`）review 中再次被判定为 blocking
- **THEN** 系统判定该 category 对第 N 轮自报构成复现，将该轮自报标记为 `unsubstantiated`，并通过 telemetry 事件记录第 N 轮（声明轮）与第 M 轮（复现轮）两个可追溯的轮次信息

#### Scenario: 触发轮次本身不算复现证据

- **WHEN** 某 category 在 `review-r(N-1)` 中被判定为 blocking（因而促使 `fix-rN` 对该 category 做扫描并自报 `categories_scanned` 包含它），且此后没有任何 `review-rM`（M ≥ N）再次判定该 category 为 blocking
- **THEN** 系统不判定 `fix-rN` 的自报存在复现信号——`review-r(N-1)` 的出现是自报的成因，不构成"声明之后的证据"

#### Scenario: 后续轮次未再现不判定复现

- **WHEN** 某 category 在第 N 轮 fix 的自报 `categories_scanned` 中被列出，且此后所有 `review-rM`（M ≥ N）均未再判定该 category 为 blocking
- **THEN** 系统不判定该轮自报存在复现信号，该轮自报维持既有 verdict（不标记为 `unsubstantiated`）

#### Scenario: 自报缺失不产生复现判定

- **WHEN** 某一轮 fix 未提供 `categories_scanned` 自报（缺失或为空）
- **THEN** 该轮不产生任何复现判定（复现判定的前提是存在一条可被检验的自报声明）

### Requirement: 存在复现信号的 category 在下一轮 fix prompt 无条件强制穷举清单

某 category 一旦被标记为复现（`unsubstantiated`），无论其当前的连续出现轮数是否达到配置阈值，下一轮渲染的 fix prompt MUST 对该 category 强制要求提供穷举落点清单（三态格式，同「连续同 category 达阈值触发强制穷举落点清单」Requirement 的格式要求）。

#### Scenario: 复现的 category 即使未达阈值仍强制穷举

- **WHEN** 某 category 被标记为复现（`unsubstantiated`），但其连续出现轮数（按最新一轮向前追溯）未达到配置阈值
- **THEN** 下一轮 fix prompt 仍对该 category 强制要求提供穷举落点清单
