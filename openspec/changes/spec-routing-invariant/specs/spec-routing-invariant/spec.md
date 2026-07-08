## ADDED Requirements

### Requirement: spec 侧路由配置与安全默认值
配置层 MUST 提供 `[spec_writer]` 与 `[spec_review]` 两个段。`spec_writer` MUST 暴露与 `coder` 同构的 `backend` 及其 `effective_backend` 解析语义；`spec_review` MUST 暴露与 `review` 同构的 `engine`。当 `.npc/config.toml` 未声明这两个段时，系统 MUST 解析为 `spec_writer.effective_backend == "claude"` 与 `spec_review.engine == "codex"`，且 MUST NOT 产生任何 violation。

#### Scenario: 未配置时取安全默认值且零 violation
- **GIVEN** `.npc/config.toml` 中不存在 `[spec_writer]` 与 `[spec_review]` 段
- **WHEN** 加载配置并调用 `check_routing(cfg)`
- **THEN** `cfg.spec_writer.effective_backend` 等于 `"claude"`
- **AND** `cfg.spec_review.engine` 等于 `"codex"`
- **AND** 返回的 violations 列表中不含任何 `rule` 以 `spec_` 开头的项

#### Scenario: 显式配置被正确解析
- **GIVEN** `.npc/config.toml` 含 `[spec_writer] backend = "mimo"` 与 `[spec_review] engine = "codex"`
- **WHEN** 加载配置
- **THEN** `cfg.spec_writer.effective_backend` 等于 `"mimo"`
- **AND** `cfg.spec_review.engine` 等于 `"codex"`

### Requirement: spec 侧后端有效性
`check_routing` MUST 校验 `spec_writer.effective_backend` 属于既有的受支持 coder 后端集合，`spec_review.engine` 属于既有的受支持 engine 集合。越界 MUST 分别产出 `rule` 为 `spec_backend_unsupported` 与 `spec_engine_unsupported` 的 violation，且 `detail` MUST 含越界的实际取值。

#### Scenario: 非法 spec_writer.backend 被拒
- **GIVEN** `spec_writer.backend` 取值为 `"gpt-9"`（不在受支持后端集合内）
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在一项 `rule == "spec_backend_unsupported"`
- **AND** 该项 `detail` 含子串 `gpt-9`

#### Scenario: 非法 spec_review.engine 被拒
- **GIVEN** `spec_review.engine` 取值为 `"bard"`（不在受支持 engine 集合内）
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在一项 `rule == "spec_engine_unsupported"`
- **AND** 该项 `detail` 含子串 `bard`

### Requirement: spec 生成方与验证方不得同源
`check_routing` MUST 检测 spec 生成方与 spec 验证方解析到同一执行身份的情形，并产出 `rule == "spec_gen_not_orthogonal"` 的 violation。判定 MUST 覆盖三种形态：(a) 双方均为 `claude` 且 `bin` 与 `model` 均相同；(b) 双方均为 `mimo`；(c) 双方均为 `codex`。形态 (c) 是必需的——`SUPPORTED_CODER_BACKENDS` 含 `codex` 且 `SUPPORTED_ENGINES` 亦含 `codex`。

#### Scenario: 双方同为 claude 且同 bin 同 model
- **GIVEN** `spec_writer.effective_backend == "claude"`、`spec_review.engine == "claude"`
- **AND** `spec_writer.bin` 与 `spec_review.claude_bin` 相同，`spec_writer.model` 与 `spec_review.claude_model` 相同
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中恰有一项 `rule == "spec_gen_not_orthogonal"`

#### Scenario: 双方同为 mimo
- **GIVEN** `spec_writer.effective_backend == "mimo"` 且 `spec_review.engine == "mimo"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在 `rule == "spec_gen_not_orthogonal"`

#### Scenario: 双方同为 codex
- **GIVEN** `spec_writer.effective_backend == "codex"` 且 `spec_review.engine == "codex"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在 `rule == "spec_gen_not_orthogonal"`

#### Scenario: 同为 claude 但 model 不同 —— 合法，不报 violation
- **GIVEN** `spec_writer.effective_backend == "claude"` 且 `spec_review.engine == "claude"`
- **AND** `spec_writer.model` 与 `spec_review.claude_model` 不同
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中不存在 `rule == "spec_gen_not_orthogonal"` 的项

#### Scenario: 默认配置正交 —— 不报 violation
- **GIVEN** `spec_writer.effective_backend == "claude"` 且 `spec_review.engine == "codex"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中不存在 `rule == "spec_gen_not_orthogonal"` 的项

### Requirement: MiMo 不得用于 spec 验证
`check_routing` MUST 在 spec 验证方路由到 MiMo 时产出 `rule == "spec_mimo_exec_only"` 的 violation。判定 MUST 与既有 `mimo_exec_only` 同构：`spec_review.engine` 含 `mimo`，或其 `claude_model` / `claude_bin` 含 `mimo`。多个触发条件同时成立时 MUST 合并为单条 violation。

#### Scenario: spec_review.engine 为 mimo
- **GIVEN** `spec_review.engine == "mimo"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在 `rule == "spec_mimo_exec_only"`

#### Scenario: spec_review 借道 claude_model 夹带 mimo
- **GIVEN** `spec_review.engine == "claude"` 且 `spec_review.claude_model` 含子串 `mimo`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中恰有一项 `rule == "spec_mimo_exec_only"`（多条件合并为单条）

#### Scenario: spec_writer 用 mimo 不触发 spec_mimo_exec_only
- **GIVEN** `spec_writer.effective_backend == "mimo"` 且 `spec_review.engine == "codex"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中不存在 `rule == "spec_mimo_exec_only"` 的项
- **AND** violations 中不存在 `rule == "spec_gen_not_orthogonal"` 的项

### Requirement: MiMo 不得用于 spec 生成（恒 in-session）
spec 生成的分发方式恒为 in-session（无 per-phase dispatch 配置）。因此 `check_routing` MUST 在 `spec_writer.effective_backend` 解析为 `mimo` 时产出 `rule == "spec_mimo_in_session"` 的 violation，语义与既有 `mimo_in_session`（MiMo 只许 headless）一致。

#### Scenario: spec_writer 配 mimo 触发 spec_mimo_in_session
- **GIVEN** `spec_writer.effective_backend == "mimo"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中恰有一项 `rule == "spec_mimo_in_session"`
- **AND** 该项 `detail` 含子串 `in-session`

#### Scenario: spec_writer 配 claude 不触发
- **GIVEN** `spec_writer.effective_backend == "claude"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中不存在 `rule == "spec_mimo_in_session"` 的项

#### Scenario: 既有 coder 侧 mimo_in_session 判定不受影响
- **GIVEN** `coder` 某 phase 后端为 `mimo` 且该 phase 的 dispatch 为 `in-session`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在 `rule == "mimo_in_session"`
- **AND** 其 `detail` 语义与本 change 之前完全一致

### Requirement: 既有 coder/review 规则的变更范围受限
本 change 对既有五条规则（`backend_unsupported`、`engine_unsupported`、`gen_not_orthogonal`、`mimo_exec_only`、`mimo_in_session`）的唯一允许改动是：为 `gen_not_orthogonal` 的同源判定补上「双方均为 `codex`」形态（修复既有漏洞）。其余规则的触发条件 MUST NOT 改变。全部五条规则的 `rule` 字符串与 `detail` 语义 MUST NOT 改变。`npc verify routing` 的退出码语义 MUST 保持不变。

#### Scenario: 修复既有 codex/codex 同源漏洞
- **GIVEN** `coder.effective_backend == "codex"` 且 `review.engine == "codex"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在 `rule == "gen_not_orthogonal"`

#### Scenario: 既有 claude/claude 同源判定不变
- **GIVEN** `coder.effective_backend == "claude"`、`review.engine == "claude"`，且 `bin` 与 `model` 均相同
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中恰有一项 `rule == "gen_not_orthogonal"`

#### Scenario: 既有 mimo_exec_only 判定不变
- **GIVEN** `review.engine == "mimo"`
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在 `rule == "mimo_exec_only"`

#### Scenario: 默认 spec 配置不误伤既有 violation
- **GIVEN** `coder.effective_backend == "claude"`、`review.engine == "claude"`，且 `bin` 与 `model` 均相同
- **AND** `[spec_writer]` 与 `[spec_review]` 未配置
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 中存在 `rule == "gen_not_orthogonal"`
- **AND** violations 中不含任何 `rule` 以 `spec_` 开头的项

#### Scenario: 两侧同时违规时各自独立报告
- **GIVEN** coder 与 review 同源，且 spec_writer 与 spec_review 亦同源
- **WHEN** 调用 `check_routing(cfg)`
- **THEN** violations 同时含 `rule == "gen_not_orthogonal"` 与 `rule == "spec_gen_not_orthogonal"` 两项
