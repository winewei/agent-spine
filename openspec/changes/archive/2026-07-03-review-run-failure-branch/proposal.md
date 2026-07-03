## Why

审计 B9（中）：3b 的 while 条件直接读 `.blocking`/`.stale`（plugins/agent-spine/commands/spine-run.md:147-149），从不检查 `R.ok`。review 自身失败（codex-exec-failed，src/npc/pipeline.py:593-630，返回体无 blocking 字段）时 `jq` 得 `null` → bash 整数比较报错 → 循环行为未定义。auto-decide 的 `codex-failed` trigger（auto_decide.py:28）存在却在 skill 中无触发点。违反不变量 2（主 session 必须先看 `.ok` 再读业务字段）与 skill 自身 Guardrails「每个 npc 命令后检查 `.ok`」。

## What Changes

- `spine-run.md` 3b：进入 while 循环**前**检查 round0 的 `R.ok`，失败即转 3d（auto 档 `npc auto-decide --trigger codex-failed`）。
- 循环体内每次 `npc review run` 后同样检查 `.ok`：失败 break 出循环并转 3d（trigger=codex-failed），使该 trigger 可达。
- while 条件的字段读取顺序调整为「先 `.ok` 后 blocking/stale」，杜绝 null 参与整数比较。
- 纯 skill markdown 契约修改；补守卫测试（如已有 skill 契约测试基建）断言 review run 调用后存在 `.ok` 检查。

## Capabilities

### New Capabilities

- `review-failure-handling`: 主循环对 `npc review run` 自身失败的确定性处理契约（codex-failed 决策路径）。

### Modified Capabilities

## Impact

- `plugins/agent-spine/commands/spine-run.md`（3b 循环前/循环内 `.ok` 检查 + 3d codex-failed 映射）
- `tests/`（skill 契约守卫测试）
- 不改 `src/npc/` 代码
