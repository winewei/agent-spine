# spec-report

## MODIFIED Requirements

### Requirement: 自报核验（确定性）

报告 MUST 含「自报 vs 观测」小节，对 coder 每个 `fix-rN` 轮的自报做确定性对账：

- `regressions_added`：以该 fix 轮的 commit range 为准，判定 diff 是否触及测试文件（测试文件识别用**通用启发式**——路径/文件名含 `test`/`spec` 等；缺 commit range 时不判定）。声明了但该轮 diff 未触及测试文件 → `warn`；一致 → `ok`；缺 commit/range/自报数据 → `unverifiable`。
- `categories_scanned`：以整个 change 聚合，与该 change 实际出现的 review category（来源 MUST 明确为 `categories_seen`）对照。自报覆盖了全部实际出现 category → `ok`；有遗漏 → `warn`；自报缺失 → `unverifiable`；**若该 change 现场重算（输入该 change 已落盘的 `entry["phases"]`，MUST NOT 读取任何持久化的复现证据字段）后存在任一构成复现的自报扫描声明（即某 category 曾在某轮 `fix-rN` 的 `categories_scanned` 中被列出，但在其后时序上晚于 `fix-rN` 的某轮 `review-rM`（M ≥ N）中再次以 blocking 身份出现）→ `unsubstantiated`**，且 `unsubstantiated` 判定的优先级高于 `warn`——只要重算得到的复现证据非空，该核验条目即为 `unsubstantiated`，不因集合层面的覆盖度是 `ok` 而被掩盖。`unsubstantiated` 表示"该轮自报未被后续轮次证实"，是一个复现信号，MUST NOT 被呈现为对该轮自报的确定性证伪结论。`unsubstantiated` 时报告 MUST 呈现存在复现信号的 category 列表及其证据（声明轮次与复现轮次）。

核验 MUST 为纯确定性（不调 LLM、不 spawn agent）；数据不足以判定时 MUST 标 `unverifiable`，MUST NOT 误报为 `warn`；MUST NOT 扩展到任何项目特定测试规范。

#### Scenario: 声明加回归测试但该轮 diff 未动测试文件

- **WHEN** 某 fix 轮 RESULT 声明 `regressions_added=[X]`，但该轮 commit range 的 diff 未触及任何测试文件
- **THEN** 该条 verdict=warn（⚠）

#### Scenario: categories_scanned 覆盖实际出现 category

- **WHEN** 自报 `categories_scanned` 覆盖了该 change `categories_seen` 的全部项，且不存在任何复现证据
- **THEN** 该条 verdict=ok（✓）

#### Scenario: 缺数据不误报

- **WHEN** coder 未提供某项自报，或对应 commit range 不可得
- **THEN** 对应核验条目 verdict=unverifiable，MUST NOT 记为 warn

#### Scenario: 存在复现证据时 verdict 为 unsubstantiated

- **WHEN** 某 category 曾在第 N 轮 `fix-rN` 的自报 `categories_scanned` 中被列出，但在其后时序上晚于 `fix-rN` 的第 M 轮（M ≥ N）review 中再次以 blocking 身份出现
- **THEN** `categories_scanned` 这条核验的 verdict=unsubstantiated（现场重算得出，不依赖任何持久化证据字段），报告呈现存在复现信号的 category 及其声明轮次与复现轮次

#### Scenario: unsubstantiated 优先级高于 warn

- **WHEN** 某 change 同时存在「集合层面自报未覆盖某 category」（本应为 warn）与「存在复现证据」两种情况
- **THEN** 该核验条目最终 verdict=unsubstantiated，不呈现为 warn
