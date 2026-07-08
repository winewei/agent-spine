# structural-invariant-checks Specification

## Purpose

以确定性结构检查（静态/契约驱动，非 coder 自报、非 reviewer 主观判据）拦截 agent-spine 自身代码中最高频复发的 validation 缺陷：telemetry 字段"在调用点被丢、未透传到 emit"、record 必需键"收了却不校验"、hook fixture 绑错 matcher。检查落 agent-spine 自己的测试套件，**不进 `npc verify`**（守 CLAUDE.md：npc verify 只放路由不变量）。不违反核心不变量 1「生成⊥验证」——检查是确定性守卫，不是 coder 给自己盖章。

## ADDED Requirements

### Requirement: 检查落项目测试套件，不进 npc verify

结构不变量检查 MUST 实现为 agent-spine 自己的测试（跑在既有 `uv run pytest` 内），MUST NOT 加入 `npc verify` 的任何子命令。检查 MUST 为确定性（静态常量比对 / monkeypatch 捕获真实输出 / AST），不得依赖 LLM 判断或运行时随机性。契约的单一事实源 MUST 为代码常量（就近于被约束的实现），不得只依赖对外 JSON schema 表达调用点 handoff。

#### Scenario: 检查在既有测试套件内运行

- **WHEN** 跑 `uv run pytest`
- **THEN** 结构不变量检查作为普通测试执行，违反即 fail
- **AND** `npc verify routing` / `npc verify tests` 的契约与输出不受影响、未新增子命令

### Requirement: telemetry emit 字段契约 —— 两层（emit 输出 + 调用点 handoff）

MUST 以代码常量声明两层契约，覆盖"字段在调用点被丢"这一最高频形态（如 `record_implement` 算出 `tests_verified` 写入 phase extra，但 `_do_phase_exit` 只把 `engine` 透传给 `emit_phase_exit`，字段在调用点丢失）：

1. **emit 输出契约** `EMIT_FIELD_CONTRACT: dict[kind, set[field]]`：每个 telemetry kind 的事件必须携带的字段。
2. **调用点 handoff 契约**（如 `PHASE_EXIT_EXTRA_CONTRACT`）：声明哪些"已算出并写入 state phase/record 的字段" MUST 被对应调用点（如 `_do_phase_exit` 的 telemetry 透传）传给 emit，而非只透一个子集。

MUST 有确定性测试：通过 monkeypatch `telemetry.emit_event` 捕获**真实 emit 调用产出的事件**，断言其含该 kind `EMIT_FIELD_CONTRACT` 要求的全部字段；并断言经由调用点（跑一个最小确定性 record/phase-exit 流程或对调用点透传逻辑做 AST 检查）产出的事件含 handoff 契约要求的、已在上游算出的字段。仅断言 emit 函数签名或样本 dict 不足以满足本要求——MUST 覆盖调用点过滤导致的字段丢失。

#### Scenario: 调用点丢字段被拦截（核心）

- **WHEN** 上游（record/phase）已算出某个 handoff 契约字段，但调用点（如 `_do_phase_exit` 的 telemetry 透传）未将其传给 emit
- **THEN** 结构测试 fail，指明被丢字段与调用点

#### Scenario: emit 自身丢字段被拦截

- **WHEN** 某 `emit_*` 产出的事件缺少其 kind `EMIT_FIELD_CONTRACT` 要求的字段
- **THEN** 通过 monkeypatch 捕获的真实事件断言 fail，指明 kind 与缺失字段

#### Scenario: 新增 emit kind / handoff 字段必须登记

- **WHEN** 新增一个 `emit_*` kind，或调用点新增一个已算出字段却未登记进两层契约
- **THEN** 结构测试 fail（要求登记），防止绕过

### Requirement: record RESULT 必需键 —— 单一事实源 + 解析器强制校验

implement / fix / failure 各自的 RESULT 必需键 MUST 来自单一事实源代码常量（如 `RESULT_REQUIRED_KEYS: dict[phase, set[key]]`）。RESULT 解析/record 逻辑 MUST 引用该常量，并在缺任一必需键时**返回失败并指明缺失键**（而非当前 `_parse_result_line` 收 `keys` 却不校验、直接返回的形态）。

MUST 有确定性测试：对每个必需键，构造"恰好缺该键"的 RESULT 行，断言 parser/record 返回失败且报告缺失键。测试 MUST 用此负例验证解析器行为，MUST NOT 仅 AST 断言常量被引用。

#### Scenario: 缺必需键的 RESULT 被拒

- **WHEN** implement RESULT 行缺 `RESULT_REQUIRED_KEYS["implement"]` 中的某一键
- **THEN** record 返回 `ok:false`，error 指明缺失的键

#### Scenario: implement 与 fix 各自必需键分别强制

- **WHEN** 分别用 implement 与 fix 的缺键负例测试
- **THEN** 两者各自的全部必需键均被强制校验，且必需键集合取自同一事实源常量的不同 phase 条目

### Requirement: hook fixture 静态回归（收窄，不做通用语义引擎）

MUST 有确定性静态回归，断言仓库内静态 hook 配置 `plugins/agent-spine/hooks/hooks.json` 的 `SubagentStop` matcher 等于既定值（`spine-coder`），并用一个 realistic 的 SubagentStop payload 证明该 hook 会真正触发校验路径。本要求 MUST 以仓库内 fixture 为语义来源，MUST NOT 实现随外部 Claude Code 语义漂移的通用 matcher 语义引擎。

#### Scenario: matcher 被改错即拦截

- **WHEN** `hooks.json` 的 SubagentStop matcher 被改成非 `spine-coder` 值
- **THEN** 静态回归 fail

#### Scenario: realistic payload 证明 hook 触发

- **WHEN** 用一个真实形态的 SubagentStop payload 走该 hook
- **THEN** 校验路径被触发（证明 matcher 绑定的是真实触发字段、非永不匹配）
