## Context

coder 当前只有一种分发：`coder.py` 经 `_default_runner` spawn `claude -p` 子进程（headless）。premium（claude）与廉价（mimo）走同一条 headless 路。调研表明 headless `claude -p` 面临被切出订阅的计费风险，而 in-session Task 工具 subagent 豁免。npc 是 CLI，不能调 Task 工具，所以 in-session 必须由编排者执行——npc 只能"渲染 + 给指令 + 事后 record"。

## Goals / Non-Goals

**Goals:**
- 用配置把"premium ⇒ in-session、mimo ⇒ headless"固化为默认，并允许覆盖。
- in-session 时 npc 把分发交回编排者，给出 spawn 所需的一切（引导语 + prompt 文件），事后用既有 `record` 装订。
- `verify routing` 守住"in-session 绝不与 mimo 同源"。

**Non-Goals:**
- 不在 npc 内实现 Task 工具调用（不可能）。
- 不改 review 路由（review 恒 premium，已有不变量 1）。
- 不动 spine-run.md 编排默认（那是 change `spine-run-default-in-session-coder` 的事）。

## Decisions

- **dispatch resolve** 复用 `resolve_backend` 同款优先级（CLI → per-phase → 全局 → 默认）。默认表由后端决定：`{claude: in-session, mimo: headless, codex: headless}`。
- **in-session 指令契约**（`implement run` / `fix run` 返回 JSON 关键字段）：
  - `ok=true`、`deferred=true`、`dispatch="in-session"`
  - `seq`、`change_id`、`phase`（fix 含 `round`）
  - `spawn_prompt`：等价于 `npc agent spawn-prompt` 的引导语（含 prompt 文件绝对路径）
  - `prompt_file`：已渲染 prompt 的绝对路径
  - 编排者据此：`Agent(subagent_type=spine-coder, prompt=spawn_prompt)` → 抽末尾 `RESULT:` 行 → `npc implement record --seq N --result "<LINE>"`（fix 用 `npc fix record … --round R`）
- **phase bookkeeping**：in-session 分支只做 phase_enter + render，不做 record；record 仍由编排者后续调用完成（与现有手动 render→spawn→record 路径一致），避免 phase 双重 enter。
- **headless 不变**：mimo / 显式 headless 维持 `_run_backend` 现有子进程路径。

## Risks / Trade-offs

- **无法自测端到端**：spine 自己的 coder 是 headless，in-session 分支的"真正 subagent spawn"只能靠 npc 侧单测（给定 config → 返回正确指令、不 spawn）+ 编排者手动验证。可接受。
- **契约扩散**：implement/fix run 现在有两种返回形态（一行跑完 vs deferred 指令）。编排者必须按 `deferred` 字段分支——这条由 change 3 的 spine-run.md 文档化。
- **兼容**：默认从"claude headless"变为"claude in-session"，会改变 `npc implement run` 对 claude 的既有行为。这是预期的破坏性默认变更，由 change 3 同步编排文档。
