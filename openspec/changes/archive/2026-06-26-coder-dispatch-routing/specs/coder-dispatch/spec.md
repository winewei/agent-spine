## ADDED Requirements

### Requirement: coder 分发机制按后端路由

系统 MUST 支持 `[coder].dispatch` 配置（取值 `headless` | `in-session`），并按"CLI override → per-phase → 全局 → 内置默认"解析。内置默认 MUST 为：claude 后端 ⇒ `in-session`，mimo 后端 ⇒ `headless`。

#### Scenario: claude 默认走 in-session

- **WHEN** 未配置 `[coder].dispatch`，后端解析为 claude
- **THEN** dispatch 解析为 `in-session`

#### Scenario: mimo 默认走 headless

- **WHEN** 未配置 `[coder].dispatch`，后端解析为 mimo
- **THEN** dispatch 解析为 `headless`

#### Scenario: per-phase 与全局覆盖生效

- **WHEN** 配置了 `[coder].dispatch` 全局值或 `[coder.phase].<phase>` 覆盖
- **THEN** 解析按"CLI → per-phase → 全局 → 默认"优先级返回最高优先级的非空值

### Requirement: in-session 分发返回指令而非 spawn 子进程

当某 phase 的 dispatch 解析为 `in-session` 时，`npc implement run` / `npc fix run` MUST NOT spawn `claude -p` 子进程，而是渲染 prompt 后返回一个结构化分发指令，让编排者用 Task 工具 spawn `spine-coder` subagent，随后由编排者调用对应 `record` 子命令装订结果。

#### Scenario: implement in-session 返回 deferred 指令

- **WHEN** `npc implement run --seq N` 且 implement 的 dispatch 为 `in-session`
- **THEN** 返回 JSON 含 `dispatch="in-session"`、`deferred=true`、`spawn_prompt`（spine-coder 引导语）、`prompt_file`（已渲染的 prompt 绝对路径）
- **AND** 不启动任何 coder 子进程
- **AND** 该 phase 的结果尚未 record（留待编排者拿到 RESULT 后调 `npc implement record`）

#### Scenario: headless 分发维持原行为

- **WHEN** dispatch 为 `headless`（如 mimo 后端，或显式配置）
- **THEN** `implement/fix run` 维持现有行为：spawn 子进程 → 抽 RESULT 行 → record，一行跑完

### Requirement: in-session 分发绝不与廉价层同源

`npc verify routing` MUST 保证 in-session 分发只用于 premium 后端（claude/codex），mimo 后端 MUST 始终 headless；任何把 mimo 与 in-session 绑定的配置即 violation。

#### Scenario: mimo + in-session 判为 violation

- **WHEN** 配置使 mimo 后端的某 phase 解析出 `in-session`
- **THEN** `npc verify routing` 报告 violation（非零退出 / `ok=false`）
