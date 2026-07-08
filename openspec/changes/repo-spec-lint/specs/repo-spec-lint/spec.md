## ADDED Requirements

### Requirement: 仓库本地脚本与结构化输出
仓库 MUST 提供 `scripts/check_spec.py`，以 `uv run scripts/check_spec.py --change <id>` 调用。其 stdout MUST 为单行合法 JSON，含键 `ok`、`change`、`errors`、`warnings`、`rule_hits`。`errors` 与 `warnings` MUST 为数组，每项含 `rule`、`file`、`line`、`detail`。`rule_hits` MUST 为映射，键集合恒等于本脚本实现的全部规则名集合（含零命中项），值为该规则本次命中次数。当且仅当 `errors` 为空时 `ok` 为 `true`。退出码 MUST 为：存在 `errors` → `1`；仅有 `warnings` 或全部干净 → `0`。

#### Scenario: 干净的 change 输出 ok 且退出 0
- **GIVEN** 一个不触发任何规则的 change
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** stdout 为合法 JSON 且 `.ok == true`
- **AND** `.errors` 为空数组
- **AND** 进程退出码为 `0`

#### Scenario: 仅有 warning 时退出 0
- **GIVEN** 一个 change，其某个 Scenario 正文缺 WHEN/THEN
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.ok == true`
- **AND** `.warnings` 长度至少为 `1`
- **AND** 进程退出码为 `0`

#### Scenario: errors 通道保留但本版本无 error 级规则
- **GIVEN** 一个 change 触发了本脚本实现的全部规则
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.errors` 为空数组（本版本交付的四条规则 severity 均为 `warning`）
- **AND** `.ok == true` 且进程退出码为 `0`

#### Scenario: errors 非空时的退出码语义（供未来升级）
- **GIVEN** 脚本内部产生了一条 severity 为 `error` 的 finding
- **WHEN** 该 finding 被写入 `.errors`
- **THEN** `.ok == false`
- **AND** 进程退出码为 `1`

#### Scenario: rule_hits 含全部规则名（含零命中）
- **GIVEN** 一个不触发任何规则的 change
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.rule_hits` 的键集合等于本脚本实现的全部规则名集合
- **AND** 其全部值为 `0`

#### Scenario: 不存在的 change 报结构化错误
- **GIVEN** `--change` 指向 `openspec/changes/` 下不存在的目录
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** stdout 为合法 JSON 且 `.ok == false`
- **AND** 输出含稳定错误标识 `change_not_found`
- **AND** 进程退出码为非零

#### Scenario: --change 只接受单段 id，不接受路径分隔符
- **GIVEN** `--change` 取值为 `archive/2026-07-03-parallel-dag-scheduling`（含 `/`）
- **WHEN** 执行脚本
- **THEN** `.ok == false`
- **AND** 输出含稳定错误标识 `invalid_change_id`
- **AND** 进程退出码为非零

#### Scenario: --change 拒绝路径穿越
- **GIVEN** `--change` 取值为 `../../etc`
- **WHEN** 执行脚本
- **THEN** `.ok == false`
- **AND** 输出含稳定错误标识 `invalid_change_id`

#### Scenario: --dir 用于直接检查一个 change 目录（供 fixture 与 archive 使用）
- **GIVEN** `--dir` 指向任意一个含 `design.md` 的目录
- **WHEN** 执行 `uv run scripts/check_spec.py --dir <path>`
- **THEN** stdout 为合法 JSON
- **AND** 该模式下 spec delta 相关规则（`scenario_missing_when_then`、`vague_adverb`）MUST 被跳过并在 `rule_hits` 中记为 `0`（`openspec show` 只认 active change id）

### Requirement: 全部规则以 warning 交付（shadow mode）
本脚本交付的四条规则（`deferred_decision_outside_open_questions`、`scenario_missing_when_then`、`vague_adverb`、`proposal_missing_non_goals`）的 severity MUST 均为 `warning`。任一规则命中 MUST NOT 使 `.ok` 变为 `false`，MUST NOT 使退出码非零。

理由：`deferred_decision_outside_open_questions` 的方差证据为正类 N=1 且因果未证；且已知反面事实——引发该长尾 change r0 blocking 的那句留白本就正确声明在 `## Open Questions` 段内，本规则会放行它。按不变量 3「新硬轨须被真实方差打出来」，该证据不足以支撑一个默认阻断门。

升级判据（MUST 写入脚本的模块级 docstring）：当 `spec_review.round` 或 code review 的 `spec_attribution` 聚合数据显示某规则的命中与 `spec-silent`/`spec-ambiguous`/`spec-contradicted` 类 blocking 存在跨 change 的稳定关联（正类样本 ≥ 3 个独立 change）时，方可将该规则升为 `error`。

#### Scenario: 延迟措辞命中时只警告不阻断
- **GIVEN** `design.md` 的 `## Decisions` 段落含一行 `per-change worktree 的 run 绑定用 CLI 参数还是 pointer 文件，实施时定`
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.warnings` 中存在 `rule == "deferred_decision_outside_open_questions"`
- **AND** `.errors` 为空数组
- **AND** `.ok == true` 且进程退出码为 `0`

#### Scenario: 脚本 docstring 载明升级判据
- **WHEN** 读取 `scripts/check_spec.py` 的模块级 docstring
- **THEN** 其文本含子串 `正类样本 ≥ 3 个独立 change`

### Requirement: 延迟决策规则的判定语义
脚本 MUST 实现规则 `deferred_decision_outside_open_questions`。该规则扫描 `openspec/changes/<id>/design.md`：若任一延迟决策措辞出现在 `## Open Questions` 段落**之外**，MUST 产出一条 finding，`detail` MUST 含命中的措辞原文，`line` MUST 为该行在 `design.md` 中的 1-based 行号。出现在 `## Open Questions` 段落**之内**时 MUST NOT 产出 finding。`design.md` 不存在时该规则 MUST 静默跳过（design 为可选 artifact）。

#### Scenario: Decisions 段内联留白被记为 warning
- **GIVEN** `design.md` 的 `## Decisions` 段落含一行 `per-change worktree 的 run 绑定用 CLI 参数还是 pointer 文件，实施时定`
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.warnings` 中存在 `rule == "deferred_decision_outside_open_questions"`
- **AND** 该项 `detail` 含子串 `实施时定`
- **AND** 该项 `line` 等于该行在 `design.md` 中的 1-based 行号

#### Scenario: Open Questions 段内的留白合法
- **GIVEN** `design.md` 含 `## Open Questions` 段落，其正文为 `pointer 文件 vs CLI 参数，实施时定`，且其余段落无任何延迟措辞
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.warnings` 中不含 `rule == "deferred_decision_outside_open_questions"`
- **AND** `.rule_hits` 中该规则的计数等于 `0`

#### Scenario: design.md 缺失时跳过而非报错
- **GIVEN** 一个 change 目录不含 `design.md`
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.warnings` 中不含 `rule == "deferred_decision_outside_open_questions"`
- **AND** 进程退出码为 `0`

### Requirement: 延迟决策匹配必须跳过代码 span 与围栏代码块
`deferred_decision_outside_open_questions` 的匹配 MUST 忽略 inline code span（一对反引号之间）与 fenced code block（三反引号围栏之间）中的文本。此约束防止「讨论该规则本身」的文档被误报。剥离 MUST 保持行号对应关系不变。

#### Scenario: 反引号内的措辞不算命中
- **GIVEN** `design.md` 的 `## Decisions` 段含一行，其中延迟措辞被一对反引号包裹
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.rule_hits` 中该规则的计数等于 `0`

#### Scenario: 围栏代码块内的措辞不算命中
- **GIVEN** `design.md` 的 `## Decisions` 段含一个三反引号围栏代码块，块内某行含裸露的 `实施时定`
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.rule_hits` 中该规则的计数等于 `0`

#### Scenario: 剥离后行号不偏移
- **GIVEN** `design.md` 的第 40 行位于 `## Decisions` 段内、含裸露的 `实施时定`，且其前方存在一个多行围栏代码块
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** 对应 `warning` 项的 `line` 等于 `40`

#### Scenario: 回归 fixture —— 讨论规则本身的真实语料零误报
- **GIVEN** 一个固定 fixture 目录，其内容为 `openspec/changes/spec-schema-hardening/` 在本 change 合入时的三个 artifact 文件快照（延迟措辞仅出现在反引号或引号列表内，用于陈述该规则本身）
- **WHEN** 对该 fixture 执行脚本
- **THEN** `.rule_hits` 中该规则的计数等于 `0`

#### Scenario: 回归 fixture —— 真实长尾语料被命中
- **GIVEN** 一个固定 fixture 目录，其内容为 `openspec/changes/archive/2026-07-03-parallel-dag-scheduling/design.md` 的快照（`## Decisions` 正文含 2 处裸露的延迟措辞，另有 2 处位于 `## Open Questions` 段内应被放行）
- **WHEN** 对该 fixture 执行脚本
- **THEN** `.rule_hits` 中该规则的计数等于 `2`
- **AND** `.ok == true`（warning 不阻断）

### Requirement: 延迟措辞词表只收决策谓语，不收时间副词
`deferred_decision_outside_open_questions` 使用的措辞词表 MUST 仅收录**自身即表达「决策未拍板」**的谓语短语。该词表 MUST NOT 收录裸的时间副词（如单独的「届时」「实现时」）或句子片段（如单独的「再定」），因为它们在普通时间状语中合法出现，会产生误报。

#### Scenario: 时间副词不触发规则
- **GIVEN** `design.md` 的 `## Decisions` 段含一行 `接口留到后续 change，届时会有独立的 spec 覆盖`
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.rule_hits` 中该规则的计数等于 `0`

#### Scenario: 决策谓语触发规则
- **GIVEN** `design.md` 的 `## Decisions` 段含一行 `用 CLI 参数还是 pointer 文件，届时决定`
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.warnings` 中存在 `rule == "deferred_decision_outside_open_questions"`

### Requirement: 另外三条规则的判定语义
脚本 MUST 实现另外三条规则：`scenario_missing_when_then`（某 Scenario 的 `rawText` 不同时含 `WHEN` 与 `THEN`）、`vague_adverb`（Requirement 正文或 Scenario 正文命中含糊副词表）、`proposal_missing_non_goals`（`proposal.md` 无 Non-Goals 段落）。

#### Scenario: 纯散文 Scenario 触发 warning 但不阻断
- **GIVEN** 一个 Requirement 的 Scenario 正文为 `It just works, trust me.`
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.warnings` 中存在 `rule == "scenario_missing_when_then"`
- **AND** `.ok == true` 且退出码为 `0`

#### Scenario: 含糊副词触发 warning
- **GIVEN** 某 Requirement 正文为 `The system SHALL handle input appropriately and quickly.`
- **AND** 含糊副词表含 `appropriately` 与 `quickly`
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.warnings` 中存在 `rule == "vague_adverb"`
- **AND** 退出码为 `0`

#### Scenario: proposal 缺 Non-Goals 触发 warning
- **GIVEN** `proposal.md` 不含 `Non-Goals`、`非目标` 任一段落标题
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.warnings` 中存在 `rule == "proposal_missing_non_goals"`
- **AND** 退出码为 `0`

#### Scenario: 四条规则同时命中仍不阻断
- **GIVEN** 一个 change 同时触发本脚本实现的全部四条规则
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.ok == true`
- **AND** 退出码为 `0`

### Requirement: spec delta 解析必须复用 openspec 的结构化产物
脚本在检查 Requirement 与 Scenario 时 MUST 消费 `openspec show <change> --json --deltas-only` 的 stdout（结构为 `deltas[].requirement.text` 与 `deltas[].requirement.scenarios[].rawText`）。脚本 MUST NOT 自行解析 spec delta 的 markdown 结构。`openspec` 不在 `PATH` 时 MUST 输出结构化错误而非抛出未捕获异常。

#### Scenario: Scenario 结构取自 openspec 解析产物
- **GIVEN** 一个 change 的 spec delta 含 2 个 Scenario，其一含 WHEN/THEN、其一为纯散文
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.rule_hits["scenario_missing_when_then"]` 等于 `1`

#### Scenario: openspec 缺失时结构化报错
- **GIVEN** `openspec` 不在 `PATH` 中
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** stdout 为合法 JSON 且 `.ok == false`
- **AND** 输出含稳定错误标识 `openspec_missing`
- **AND** 进程 MUST NOT 抛出未捕获异常

#### Scenario: 只读 stdout，不被 stderr 警告污染
- **GIVEN** `openspec show` 向 stderr 打印 deprecation 警告
- **WHEN** 脚本解析其输出
- **THEN** 解析成功，`.ok` 为布尔值，MUST NOT 报 JSON 解析失败

### Requirement: 扫描范围限定于 change 目录
脚本 MUST 仅读取 `openspec/changes/<id>/` 下的文件。脚本 MUST NOT 读取或校验 `openspec/specs/` 下的任何文件。

#### Scenario: 已归档 specs 目录不被扫描
- **GIVEN** `openspec/specs/` 下 33 个 spec 文件的 Purpose 段均含 openspec archive 工具插入的桩文本
- **WHEN** 对任一 change 执行 `uv run scripts/check_spec.py --change <id>`
- **THEN** `.errors` 与 `.warnings` 中不含任何 `file` 位于 `openspec/specs/` 下的项

### Requirement: 不重复实现 openspec 已强制的校验
脚本 MUST NOT 校验 Requirement 正文是否含 `SHALL`/`MUST`，MUST NOT 校验 Requirement 是否至少含一个 Scenario，MUST NOT 校验 artifact 文件是否存在。这三类分别由 `openspec validate` 与项目 schema 的 `apply.requires` 强制，重复实现会制造两套可能漂移的真相源。

#### Scenario: 负向断言——脚本不含 SHALL/MUST 规则
- **WHEN** 执行 `uv run scripts/check_spec.py --change <id>` 并读取 `.rule_hits` 的键集合
- **THEN** 该集合不含任何名称包含 `shall`、`must_keyword`、`normative` 的规则
- **AND** 该集合不含任何名称包含 `missing_scenario`、`artifact_exists` 的规则

#### Scenario: 缺 SHALL 的 Requirement 由 openspec 拦而非本脚本
- **GIVEN** 某 Requirement 正文不含 `SHALL` 也不含 `MUST`
- **WHEN** 执行 `openspec validate <id> --type change`
- **THEN** 其退出码为非零且输出含 `must contain SHALL or MUST`

### Requirement: 不触碰 npc 源码
本 change MUST NOT 修改 `src/npc/` 下的任何文件，MUST NOT 新增任何 npc 子命令，MUST NOT 引入对 `npc` 包的 import。规则内容（延迟措辞词表、含糊副词表）MUST 留在仓库脚本内，MUST NOT 进入 npc。

#### Scenario: 负向断言——脚本不 import npc
- **WHEN** 解析 `scripts/check_spec.py` 的 import 语句
- **THEN** 其中不含任何以 `npc` 开头的模块
- **AND** 该脚本可在未安装 `npc` 的环境中独立运行

#### Scenario: 负向断言——npc 子命令面未变
- **WHEN** 执行 `npc spec --help`
- **THEN** 其子命令列表中不存在名为 `lint` 的子命令
