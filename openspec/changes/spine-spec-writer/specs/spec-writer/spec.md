## ADDED Requirements

### Requirement: spec 评审输出契约
系统 MUST 定义 `SPEC_REVIEW_SCHEMA`，作为 spec 语义评审输出的单一事实源。其顶层 MUST 含 `verdict` 与 `findings`，`additionalProperties` MUST 为 `false`。`verdict` 的 `enum` MUST 恰为 `["approve", "passed-with-advisory", "changes-requested"]`。每条 finding MUST 必需含 `id`、`severity`、`category`、`title`、`file`、`line_range`、`detail`、`recommendation`。`line_range` 语义与既有 `REVIEW_SCHEMA` 一致（如 `42-58` 或单行 `42`；不适用时填 `-`），使 finding 可被定位。`severity` 的 `enum` MUST 恰为 `["critical","high","medium","low"]`。`category` 的 `enum` MUST 恰为 `["ambiguity","missing-scenario","implementation-leak","untestable","deferred-decision","contradiction","scope-creep"]`。

#### Scenario: schema 枚举完备
- **WHEN** 读取 `SPEC_REVIEW_SCHEMA`
- **THEN** 其 `properties.findings.items.properties.category.enum` 等于 `["ambiguity","missing-scenario","implementation-leak","untestable","deferred-decision","contradiction","scope-creep"]`
- **AND** 其 `properties.findings.items.additionalProperties` 为 `false`

#### Scenario: 非法 category 被拒
- **GIVEN** 一条 finding，其 `category` 取值为 `"style"`
- **WHEN** 用 `SPEC_REVIEW_SCHEMA` 校验
- **THEN** 校验失败

#### Scenario: finding 可定位
- **WHEN** 读取 `SPEC_REVIEW_SCHEMA["properties"]["findings"]["items"]["required"]`
- **THEN** 该列表含 `line_range`

#### Scenario: spec review schema 与 code review schema 相互独立
- **WHEN** 比较 `SPEC_REVIEW_SCHEMA` 与既有 `REVIEW_SCHEMA`
- **THEN** 两者为不同对象
- **AND** `SPEC_REVIEW_SCHEMA` 的 finding 必需键中不含 `in_scope`
- **AND** `SPEC_REVIEW_SCHEMA` 的 finding 必需键中不含 `spec_attribution`

### Requirement: 质量门按成本递增顺序执行
`npc spec review run` MUST 在调用 LLM 引擎之前，先执行 `openspec validate <change> --type change --strict`，再执行 `.npc/config.toml` 中 `[spec_review] gate_cmd` 配置的确定性 gate 命令。

`gate_cmd` MUST 为 **argv 数组**（如 `["uv", "run", "scripts/check_spec.py"]`），MUST NOT 为需要 shell 解析的字符串。npc MUST 在其后追加 `--change <id>` 两个 argv 元素，并以 `shell=False` 执行——此形态杜绝参数注入。gate 命令的 stdout MUST 为合法 JSON；npc MUST 只读其 `ok`（布尔）与 `rule_hits`（映射）两个键，MUST NOT 解读任何规则名或规则语义。

`ok == false` 时 MUST NOT 调用 LLM 引擎，MUST 返回结构化失败结果，`gate_failed` 为 `"gate_cmd"`。`openspec validate` 失败时 `gate_failed` 为 `"openspec_validate"`。`gate_cmd` 未配置时 MUST 跳过该门、`gate_skipped` 为 `true` 并继续；gate 命令不可执行或 stdout 非合法 JSON 时 MUST 视为门失败（`gate_failed` 为 `"gate_cmd"`，附 `gate_error` 稳定标识），MUST NOT 静默放行。

#### Scenario: openspec validate 失败则不烧 LLM
- **GIVEN** 某 change 的 Requirement 正文不含 `SHALL` 也不含 `MUST`
- **WHEN** 执行 `npc spec review run --change <id> --round 0`
- **THEN** 返回 JSON 满足 `.ok == false` 且 `.gate_failed == "openspec_validate"`
- **AND** 未产生任何 LLM 引擎子进程调用
- **AND** 未写出 `round-0.spec-review.json`

#### Scenario: gate 命令返回 ok=false 则不烧 LLM
- **GIVEN** 某 change 通过 `openspec validate --strict`
- **AND** `gate_cmd` 配置为一个**桩命令**，其 stdout 恒为 `{"ok": false, "rule_hits": {}}`
- **WHEN** 执行 `npc spec review run --change <id> --round 0`
- **THEN** `.ok == false` 且 `.gate_failed == "gate_cmd"`
- **AND** 未产生任何 LLM 引擎子进程调用

> 此场景 MUST NOT 用 `scripts/check_spec.py` 构造。该脚本交付时四条规则全为 `warning`，恒返回 `ok == true`（见 `repo-spec-lint`）。用它构造 `ok == false` 会得到一个永远失败的测试。

#### Scenario: gate 命令仅有 warning 时继续进入语义门
- **GIVEN** 某 change 通过 `openspec validate --strict`，配置的 gate 命令返回 `ok == true`（仅 warning、无 error）
- **WHEN** 执行 `npc spec review run --change <id> --round 0`
- **THEN** LLM 引擎被调用
- **AND** `round-0.spec-review.json` 被写出

#### Scenario: gate_cmd 以 argv 数组 + 追加 --change 执行
- **GIVEN** `.npc/config.toml` 含 `[spec_review] gate_cmd = ["uv", "run", "scripts/check_spec.py"]`
- **WHEN** 执行 `npc spec review run --change my-change --round 0`
- **THEN** 实际发起的子进程 argv 等于 `["uv","run","scripts/check_spec.py","--change","my-change"]`
- **AND** 该子进程以 `shell=False` 启动

#### Scenario: gate_cmd 未配置时跳过而非静默放行
- **GIVEN** `.npc/config.toml` 的 `[spec_review]` 未含 `gate_cmd`
- **WHEN** 执行 `npc spec review run --change <id> --round 0`
- **THEN** 返回 JSON 的 `.gate_skipped == true`
- **AND** `.gate_failed` 为 `null`
- **AND** LLM 引擎被调用

#### Scenario: gate 命令输出非法 JSON 视为门失败
- **GIVEN** 配置的 gate 命令 stdout 为 `not-json`
- **WHEN** 执行 `npc spec review run --change <id> --round 0`
- **THEN** `.ok == false` 且 `.gate_failed == "gate_cmd"`
- **AND** 输出含稳定标识 `gate_output_invalid`
- **AND** 未产生任何 LLM 引擎子进程调用

#### Scenario: npc 不解读规则语义
- **WHEN** 检查 `npc spec review run` 的实现模块源码
- **THEN** 其中不含任何规则名字符串（`deferred_decision_outside_open_questions` / `vague_adverb` / `scenario_missing_when_then` / `proposal_missing_non_goals`）
- **AND** 其中不含任何延迟措辞词表或含糊副词词表常量

### Requirement: spec 评审的 blocking 判定与轮次记录
`npc spec review run` MUST 输出 JSON，含 `ok`、`change`、`round`、`verdict`、`blocking`、`advisory`、`blocking_categories`、`gate_failed`、`gate_skipped`、`pointer.spec_review_json`。评审结果 MUST 写入**轮次化路径** `round-{N}.spec-review.json`（`N` 为 `--round` 取值），MUST NOT 写入无轮次的 `spec-review.json`——同一路径无法同时保存多轮内容。`pointer.spec_review_json` MUST 指向该轮的绝对路径。`blocking` MUST 定义为 `severity ∈ {critical, high}` 的 finding 计数。`blocking_categories` MUST 为本轮出现过的 blocking finding 的 `category` 去重列表。

#### Scenario: blocking 计数只看 severity
- **GIVEN** 某轮 spec review 产出 3 条 finding，`severity` 分别为 `critical`、`high`、`medium`
- **WHEN** 解析该轮结果
- **THEN** `.blocking` 等于 `2`
- **AND** `.advisory` 等于 `1`

#### Scenario: blocking_categories 去重
- **GIVEN** 某轮 spec review 的 blocking findings 的 `category` 依次为 `ambiguity`、`ambiguity`、`untestable`
- **WHEN** 解析该轮结果
- **THEN** `.blocking_categories` 集合等于 `{"ambiguity","untestable"}`

#### Scenario: 评审结果写入轮次化路径
- **GIVEN** 第 0 轮的 `round-0.spec-review.json` 已存在
- **WHEN** 执行 `npc spec review run --change <id> --round 1`
- **THEN** 新结果写入 `round-1.spec-review.json`
- **AND** `round-0.spec-review.json` 的内容未被覆盖
- **AND** `.pointer.spec_review_json` 以 `round-1.spec-review.json` 结尾

### Requirement: spec fix 循环使用固定轮次上限且不复用 stale 检测
spec fix 循环 MUST 以固定的最大 **fix 次数**上限终止。`[spec_review] max_rounds = N` 的语义 MUST 为「最多执行 `N` 次 spec fix」，因此 review 轮次索引取值范围为 `0..N`（共 `N+1` 次 review）。默认 `N = 3`。

系统 MUST NOT 在 spec 评审循环中调用 code review 的 stale 检测逻辑（`rounds_since_strict_decrease`）。执行完第 `N` 次 fix 后的第 `N` 轮 review 仍 `blocking > 0` 时，MUST 返回 `status == "needs-user-decision"`，MUST NOT 自动进入 archive。

#### Scenario: blocking 清零则循环终止
- **GIVEN** spec review 第 1 轮 `blocking == 0`
- **WHEN** 驱动 spec fix 循环
- **THEN** 循环终止，`status == "clean"`
- **AND** spec fix 未被调用

#### Scenario: 达到 fix 次数上限仍有 blocking 则交人
- **GIVEN** `[spec_review] max_rounds = 3`，且第 `0`/`1`/`2`/`3` 轮 review 的 `blocking` 均大于 `0`
- **WHEN** 驱动 spec fix 循环
- **THEN** spec fix 恰被调用 `3` 次
- **AND** `npc spec review run` 恰被调用 `4` 次（round `0..3`）
- **AND** 循环终止，`status == "needs-user-decision"`
- **AND** 未触发任何 archive 动作

#### Scenario: max_rounds=0 表示只审不修
- **GIVEN** `[spec_review] max_rounds = 0` 且第 `0` 轮 review 的 `blocking > 0`
- **WHEN** 驱动 spec fix 循环
- **THEN** spec fix **未**被调用
- **AND** `status == "needs-user-decision"`

#### Scenario: 负向断言——未引用 stale 检测
- **WHEN** 检查 spec 评审循环的实现模块
- **THEN** 其源码 MUST NOT 引用 `rounds_since_strict_decrease`
- **AND** 其源码 MUST NOT 从 code review 模块导入 stale 判定函数

#### Scenario: blocking 反弹不被误判为卡死
- **GIVEN** 第 `0..3` 轮 review 的 `blocking` 依次为 `2`、`4`、`1`、`3`（改写 spec 后长出新 finding）
- **AND** `max_rounds == 3`
- **WHEN** 驱动 spec fix 循环
- **THEN** 循环因**fix 次数上限**而非 stale 判定而终止
- **AND** `status == "needs-user-decision"`

### Requirement: spec writer 的 RESULT 契约
`RESULT_REQUIRED_KEYS` MUST 新增 phase `spec_write`，其必需键集合恰为 `{"change", "artifacts", "validate", "summary"}`；MUST 新增 phase `spec_fix`，其必需键集合恰为 `{"change", "fixed", "validate", "summary"}`。`npc spec write record` 与 `npc spec fix record` MUST 在 RESULT 行缺少任一必需键时返回 `ok == false` 与稳定错误标识，MUST NOT 静默接受。

#### Scenario: 缺必需键的 RESULT 被拒
- **GIVEN** spec writer 回报的 RESULT 行缺少 `validate` 键
- **WHEN** 执行 `npc spec write record --result "<该行>"`
- **THEN** 返回 JSON 满足 `.ok == false`
- **AND** 输出含稳定错误标识，且指明缺失的键名 `validate`

#### Scenario: 完整 RESULT 被接受
- **GIVEN** spec writer 回报的 RESULT 行含全部四个必需键
- **WHEN** 执行 `npc spec write record --result "<该行>"`
- **THEN** `.ok == true`

#### Scenario: 既有 phase 的必需键未被改动
- **WHEN** 读取 `RESULT_REQUIRED_KEYS`
- **THEN** `RESULT_REQUIRED_KEYS["implement"]` 等于 `{"commit","tasks","tests","summary"}`
- **AND** `RESULT_REQUIRED_KEYS["fix"]` 等于 `{"commit","fixed","tests","summary","categories_scanned","regressions_added"}`

### Requirement: 生成侧不得预知本轮评判标准
`npc spec write run` 渲染给 `spine-spec-writer` 的 prompt 文件 MUST NOT 包含本轮 spec-review 的 focus 渲染文本、评分 rubric 细则、或任何 `SPEC_REVIEW_SCHEMA` 的 `category` 枚举列表。`npc spec fix run` 渲染的 prompt MAY 包含**上一轮已签发**的 blocking findings 原文。此边界为**时点**边界，与 `spine-coder` 的 implement/fix 结构同构。

#### Scenario: 负向断言——write 轮 prompt 不含 rubric
- **WHEN** 执行 `npc spec write run --change <id>` 并读取渲染出的 prompt 文件全文
- **THEN** 其文本 MUST NOT 含子串 `scope-creep`
- **AND** MUST NOT 含子串 `implementation-leak`
- **AND** MUST NOT 含任何 `spec-review.json` 的 findings 原文

#### Scenario: fix 轮 prompt 可含上一轮已签发 findings
- **GIVEN** `round-0.spec-review.json` 已产出，含一条 `category == "ambiguity"` 的 blocking finding
- **WHEN** 执行 `npc spec fix run --change <id> --round 1` 并读取渲染出的 prompt 文件
- **THEN** 其文本含该 finding 的 `detail` 原文

#### Scenario: fix 轮 prompt 不含当轮 review 内容
- **GIVEN** 磁盘上同时存在 `round-0.spec-review.json` 与 `round-1.spec-review.json`，两者 findings 的 `detail` 互不相同
- **WHEN** 执行 `npc spec fix run --change <id> --round 1` 并读取渲染出的 prompt 文件
- **THEN** 其文本含 `round-0.spec-review.json` 的 finding `detail` 原文
- **AND** MUST NOT 含 `round-1.spec-review.json` 的 finding `detail` 原文

#### Scenario: 上一轮 review 未落盘时 fix 拒绝渲染
- **GIVEN** `round-0.spec-review.json` 不存在
- **WHEN** 执行 `npc spec fix run --change <id> --round 1`
- **THEN** 返回 `.ok == false`
- **AND** 输出含稳定错误标识 `prev_spec_review_missing`

### Requirement: spec 评审结果不得回流 code 阶段
`round-{N}.spec-review.json` 的任何 findings、category 或 verdict MUST NOT 出现在 `npc implement run` 与 `npc fix run` 渲染的 prompt 文件中。反向地，`npc spec write run` 与 `npc spec fix run` 渲染的 prompt 文件 MUST NOT 包含 code review 的 findings、`spec_attribution` 字段名、其任一枚举值，或 `spec_attributable_blocking_rate`。

#### Scenario: 负向断言——implement prompt 不含 spec review 内容
- **GIVEN** 某 change 的 `round-0.spec-review.json` 存在且含一条 `category == "untestable"` 的 finding
- **WHEN** 执行 `npc implement run --seq <n>` 并读取渲染出的 prompt 文件
- **THEN** 其文本 MUST NOT 含子串 `untestable`
- **AND** MUST NOT 含该 finding 的 `detail` 原文

### Requirement: spec 评审轮次进 telemetry
每轮 `npc spec review run` MUST emit 一条 kind 为 `spec_review.round` 的 telemetry 事件。`EMIT_FIELD_CONTRACT["spec_review.round"]` MUST 恰为以下键集合（与既有 `review.round` 的公共字段对齐，再加 spec 侧专有字段）：

`proj_key`、`canonical_proj_key`、`run_ts`、`change_seq`、`change_id`、`phase`、`round`、`status`、`duration_ms`、`verdict`、`blocking_count`、`blocking_categories`、`engine`、`retry_count`、`outcome_reason`、`tokens`、`pointer`、`gate_failed`、`gate_skipped`、`gate_rule_hits`

`gate_rule_hits` MUST 为 gate 命令输出的 `rule_hits` 的原样透传（npc 不解读其键名）。该 kind MUST 同时出现在 `telemetry_schema_v1.json` 中。确定性门失败而未调用 LLM 时，事件 MUST 仍被 emit，且 `gate_failed` 非空、`verdict` 为 `null`（**MUST NOT** 为 `"changes-requested"`——未跑评审即无 verdict，缺数据不得伪装成判定结果）。

#### Scenario: emit 字段集合与契约一致
- **WHEN** 执行一轮 `npc spec review run` 并捕获实际 emit 的事件（monkeypatch `emit_event`）
- **THEN** 其 `kind == "spec_review.round"`
- **AND** 其键集合等于 `EMIT_FIELD_CONTRACT["spec_review.round"]`

#### Scenario: gate_rule_hits 原样透传
- **GIVEN** 配置的 gate 命令 stdout 为 `{"ok": true, "rule_hits": {"foo_rule": 2, "bar_rule": 0}}`
- **WHEN** 执行 `npc spec review run --change <id> --round 0` 并捕获实际 emit 的事件
- **THEN** 该事件的 `gate_rule_hits` 等于 `{"foo_rule": 2, "bar_rule": 0}`

#### Scenario: 门失败时仍 emit 且标明失败门
- **GIVEN** 配置的 gate 命令对该 change 返回 `ok == false`
- **WHEN** 执行 `npc spec review run --change <id> --round 0`
- **THEN** 被 emit 的 `spec_review.round` 事件的 `gate_failed` 等于 `"gate_cmd"`
- **AND** 该事件的 `verdict` 为 `null`

### Requirement: 用户入口与 subagent 注册
仓库 MUST 提供 `plugins/agent-spine/commands/spine-spec.md` 作为 `/spine-spec` 的入口，与 `plugins/agent-spine/agents/spine-spec-writer.md` 作为执行体契约。两者 MUST 在 `plugins/agent-spine/.claude-plugin/plugin.json` 的对应清单中注册。

**职责边界 MUST 由确定性校验强制，不得依赖 prompt 文案的口头约束**（`spine-spec-writer` 持有 `Bash`，仅靠文案无法阻止它 `git commit` 或改源码）。`npc spec write record` 与 `npc spec fix record` MUST 在装订 RESULT 前，用 `git status --porcelain` 检查工作区变更集：若存在任何位于 `openspec/changes/<id>/` 之外的路径变更，MUST 返回 `ok == false` 与稳定错误标识 `out_of_scope_changes`，并把越界路径列入输出，MUST NOT 装订该 RESULT。`spine-spec-writer` MUST NOT 产生任何 git commit——RESULT 契约中不存在 `commit` 键，其自报的 commit 无处安放。

#### Scenario: 越界修改被 record 拒绝
- **GIVEN** spec writer 除了写 `openspec/changes/my-change/` 下的 artifact，还修改了 `src/npc/cli.py`
- **WHEN** 执行 `npc spec write record --change my-change --result "<合法 RESULT 行>"`
- **THEN** 返回 `.ok == false`
- **AND** 输出含稳定错误标识 `out_of_scope_changes`
- **AND** 输出的越界路径列表含 `src/npc/cli.py`

#### Scenario: 仅改 change 目录时 record 通过
- **GIVEN** spec writer 只修改了 `openspec/changes/my-change/` 下的文件
- **WHEN** 执行 `npc spec write record --change my-change --result "<合法 RESULT 行>"`
- **THEN** `.ok == true`

#### Scenario: spec writer 产生 git commit 时被拒
- **GIVEN** spec writer 执行了 `git commit`，使 `HEAD` 相对 record 前发生变化
- **WHEN** 执行 `npc spec write record --change my-change --result "<合法 RESULT 行>"`
- **THEN** 返回 `.ok == false`
- **AND** 输出含稳定错误标识 `unexpected_commit`

#### Scenario: subagent 契约含 RESULT schema
- **WHEN** 读取 `plugins/agent-spine/agents/spine-spec-writer.md`
- **THEN** 其正文列出 `spec_write` 与 `spec_fix` 两个 phase 的 RESULT 必需键
- **AND** 其正文要求第一步 Read npc 渲染的 prompt 文件绝对路径
- **AND** 其正文明令 MUST NOT 运行 `git commit`、MUST NOT 修改 `openspec/changes/<id>/` 之外的任何文件

#### Scenario: 入口命令不接管 spine-run Step 2B
- **WHEN** 读取 `plugins/agent-spine/commands/spine-run.md`
- **THEN** 其 Step 2B 的文本未被本 change 修改
- **AND** 其中不含 `spine-spec-writer` 字样

### Requirement: v1 只支持 in-session 分发，路由合法性单一真相源
`npc spec write run` 与 `npc spec fix run` MUST 恒返回 `deferred == true`（in-session，由编排者 spawn `spine-spec-writer` subagent）。本版本 MUST NOT 支持 headless 分发。

路由合法性 MUST NOT 由本 change 独立判定。`npc spec write run` 与 `npc spec fix run` MUST 在渲染 prompt 之前调用 `check_routing(cfg)`（`spec-routing-invariant` 提供），若返回的 violations 中存在任一 `rule` 以 `spec_` 开头的项，MUST 返回 `ok == false` 与稳定错误标识 `spec_routing_violation`，并把命中的 `rule` 与 `detail` 原样列入输出。本 change MUST NOT 引入任何独立的后端白名单常量或独立的错误标识（如 `spec_writer_backend_unsupported`）——那会制造与 `check_routing` 漂移的第二真相源。

因此 `spec_writer.backend = "mimo"` 被拒的路径是：`check_routing` 产出 `spec_mimo_in_session` → `npc spec write run` 以 `spec_routing_violation` 拒绝执行。`spec_mimo_in_session` 是规则名（`npc verify routing` 的输出），`spec_routing_violation` 是命令级错误标识（`npc spec write run` 的输出），二者层级不同、不重复。

超时预算 MUST 复用既有的 `npc agent timeout-budget` / `npc agent record-timeout`，phase 名为 `spec_write` 与 `spec_fix-r{N}`。

#### Scenario: 恒为 in-session
- **WHEN** 执行 `npc spec write run --change <id>`
- **THEN** 返回 JSON 的 `.deferred == true`
- **AND** 返回 JSON 含 `.spawn_prompt` 与 `.prompt_file`

#### Scenario: mimo 后端经由 check_routing 被拒
- **GIVEN** `.npc/config.toml` 含 `[spec_writer] backend = "mimo"`
- **WHEN** 执行 `npc spec write run --change <id>`
- **THEN** `.ok == false`
- **AND** 输出含稳定错误标识 `spec_routing_violation`
- **AND** 输出的 violations 列表含 `rule == "spec_mimo_in_session"`
- **AND** 未渲染任何 prompt 文件

#### Scenario: 路由检查先于 prompt 渲染
- **GIVEN** `.npc/config.toml` 使 `spec_writer` 与 `spec_review` 解析到同一执行身份
- **WHEN** 执行 `npc spec write run --change <id>`
- **THEN** `.ok == false` 且输出含 `spec_routing_violation`
- **AND** 输出的 violations 列表含 `rule == "spec_gen_not_orthogonal"`
- **AND** 未产生任何 LLM 引擎子进程调用

#### Scenario: 本 change 不持有独立的后端白名单
- **WHEN** 检查 `src/npc/spec_pipeline.py` 的源码
- **THEN** 其中不含任何名称以 `SUPPORTED_SPEC_` 开头的常量
- **AND** 其中不含字符串 `spec_writer_backend_unsupported`

#### Scenario: 超时预算复用既有四件套
- **WHEN** 执行 `npc agent timeout-budget --change <id> --phase spec_write`
- **THEN** 返回 JSON 的 `.ok == true` 且 `.timeout_sec` 为正整数

### Requirement: 既有 code 流水线行为不变
本 change MUST NOT 改变 `npc implement run|record`、`npc fix run|record`、`npc review run`、`npc archive run` 的任何输出字段、退出码或 telemetry 事件形态。

#### Scenario: 既有 review.round 契约未变
- **WHEN** 读取 `telemetry.EMIT_FIELD_CONTRACT["review.round"]`
- **THEN** 其不含 `gate_failed`
- **AND** 其仍含 `blocking_categories` 与 `spec_attribution_counts`

#### Scenario: 既有 auto-decide trigger 集合未扩张
- **WHEN** 读取 `npc auto-decide` 的 `VALID_TRIGGERS`
- **THEN** 其不含任何以 `spec-` 开头的 trigger
