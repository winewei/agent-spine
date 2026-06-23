## Why

`coder-dispatch-routing` 在 npc 代码层让 premium coder 默认走 in-session，但**编排 skill `spine-run.md` 仍把 `npc implement run` 一行跑完当默认**——它不知道 in-session 时该改走 render→spawn→record 三步。必须同步编排文档，否则编排者拿到 `deferred=true` 指令会不知所措。同时把今日计费调研的理由写进 skill 与不变量，供后人理解"为什么 premium coder 不走 headless"。

## What Changes

- 改 `plugins/agent-spine/commands/spine-run.md`：
  - Step 3a/3b：claude 后端**默认**走 `npc implement/fix run` → 收 `deferred` 指令 → `Agent(subagent_type=spine-coder, prompt=spawn_prompt)` → 抽 RESULT → `npc … record`；mimo 后端走 headless 一行跑完。
  - 成本路由表与 guardrails 增补 in-session/headless 的分发说明 + 计费理由。
- `docs/principles.md`：不变量 4 增补一句——premium coder 经 in-session subagent 对冲 headless `claude -p` 被切出订阅的风险（in-session 属交互式、官方豁免）。

## Capabilities

### New Capabilities
- `spine-run-coder-dispatch`: 编排者按 `deferred` 字段在 in-session 三步与 headless 一行之间分发 coder 的契约。

### Modified Capabilities

## Impact

- `plugins/agent-spine/commands/spine-run.md`（编排 skill 文档）。
- `docs/principles.md`（不变量 4 增补）。
- 纯文档/skill 变更；依赖 `coder-dispatch-routing` 已定义 `deferred` 指令契约。
