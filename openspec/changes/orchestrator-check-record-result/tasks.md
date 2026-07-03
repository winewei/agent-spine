## 1. skill 契约修改

- [x] 1.1 spine-run.md 3a：`npc implement record` 后捕获返回 JSON，检查 `.ok` 与 `.status`，失败/needs-user-decision → 进 3d（trigger=implementer-failed）
- [x] 1.2 spine-run.md 3b：`npc fix record` 后同样检查，失败 → 进 3d（trigger=fixer-failed），不再进入下一轮 review
- [x] 1.3 3a 说明文字修正：明确 deferred=true 时 `.ok` 只代表渲染成功，coder 成败以 record 返回为准

## 2. Guardrails 与文档

- [x] 2.1 Guardrails 增补："record 返回值是 coder 成败唯一真相；record 失败绝不继续 review/archive"
- [x] 2.2 docs 主循环描述（design.md/README 相关段落）同步该检查点

## 3. 验证

- [x] 3.1 复核 skill 全文：所有 `npc implement record` / `npc fix record` 出现处均带检查分支
- [x] 3.2 守卫测试（若已有 skill 契约测试基建）：断言 spine-run.md 中 record 调用后紧跟 `.ok` 检查模式
