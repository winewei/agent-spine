## 1. skill 契约修改

- [x] 1.1 spine-run.md 3b：round0 后、while 前检查 `R.ok`，false → 转 3d（trigger=codex-failed）
- [x] 1.2 循环体内每次 `npc review run` 后检查 `.ok`，false → break 并转 3d（trigger=codex-failed）
- [x] 1.3 while 条件注明只在 `.ok=true` 时读 blocking/stale，避免 null 整数比较
- [x] 1.4 3d 触发场景映射表补 codex-failed（review 自身失败）

## 2. 验证

- [x] 2.1 复核 skill 全文所有 `npc review run` 出现处均有 `.ok` 分支
- [x] 2.2 守卫测试：断言 spine-run.md 的 review 循环包含 `.ok` 检查与 codex-failed trigger
- [x] 2.3 与 fix-auto-decide-trigger-contract 的词表守卫测试联动核对（codex-failed ∈ VALID_TRIGGERS）
