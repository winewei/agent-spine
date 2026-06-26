## Why

调研结论（2026-06-23）：Anthropic 已宣布要把 **Agent SDK / `claude -p` headless** 用量切出订阅、按 API 费率走单独信用池（5/14 宣布、6/15 当天暂停、声明将重做后重推）；而 **in-session Task 工具 subagent 属于"交互式 Claude Code"，被官方明确豁免、仍走订阅**。因此 premium coder 应走 in-session 以对冲该计费碎出，并省掉每次 headless `claude -p` 冷启动独立 session 的开销。

但 npc 是 CLI，**无法调用 Claude Code 的 Task 工具**——in-session 分发只能交回编排者（主 session）执行。为防编排者误用 headless 付费路径跑 premium coder，需要 npc 在**代码层固化路由**：premium（claude）默认 in-session，廉价层（mimo）维持 headless。

## What Changes

- `config`：新增 `[coder].dispatch`（`headless` | `in-session`），支持 per-phase 覆盖。resolve 优先级：CLI override → per-phase → 全局 → 默认（**claude ⇒ in-session，mimo ⇒ headless**）。
- `npc implement run` / `npc fix run`：解析出的 dispatch 为 `in-session` 时**不 spawn 子进程**，改返回结构化**分发指令**（含 spawn 引导语 + prompt_file + record 提示，`deferred=true`），由编排者 spawn `spine-coder` subagent 后再 `npc … record`；dispatch 为 `headless` 时维持现有 spawn→抽 RESULT→record 行为。
- `npc verify routing` 扩展：in-session 分发**绝不**与 mimo 同源（mimo 只许 headless）；premium coder 默认 in-session。
- 补单测。

## Capabilities

### New Capabilities
- `coder-dispatch`: coder 分发机制路由契约——按后端决定 headless 子进程 vs in-session subagent，及 in-session 的指令交接格式。

### Modified Capabilities

## Impact

- `src/npc/config.py`：`[coder].dispatch` 解析 + per-phase resolve。
- `src/npc/coder.py`：`run_implement` / `run_fix` 增加 in-session 分支（返回指令、跳过子进程）。
- `src/npc/verify.py`：routing 校验扩展。
- `tests/`：dispatch resolve + in-session 指令 + headless 回归 + verify routing。
