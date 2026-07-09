## 1. spine-coder 契约

- [ ] 1.1 在 `plugins/agent-spine/agents/spine-coder.md` 的 Guardrails 段新增一条纪律：只 `git add` 自己明确改动的文件（逐文件枚举）；禁止 `git add -A` / `git add .`；禁止 `git stash` / `git reset --hard` / 任何丢弃未提交改动的破坏性 git 操作。
- [ ] 1.2 在同一处（或紧邻位置）补充：commit 的文件清单必须与 summary.md 里逐文件改动清单一致（自报口径，reviewer 可核验）。
- [ ] 1.3 在同一处补充错误路径：遇到无法归因给本次任务的改动或冲突文件时，MUST NOT 继续提交或静默忽略，必须停止提交流程并在 summary.md / RESULT 中报告阻塞状态，交由 reviewer / 编排者 / 人工处理；失败态 RESULT 一律使用 `spine-coder.md` 中该阶段已定义的失败 schema，不新增/修改 key。
- [ ] 1.5 在同一处补充：若目标文件本身已存在无法归因的未暂存 hunk，MUST NOT 整文件 `git add`；只能在能精确核验的前提下做 hunk 级暂存，否则按 1.3 的失败路径停止并上报。
- [ ] 1.4 Guardrails 增加：commit 前 MUST 核验 index（`git diff --cached --name-only`），无法归因的 staged 条目只许非破坏性 unstage（`git restore --staged`）后重新核验；不可达成则停止并按失败态 RESULT 上报

## 2. templates.py 渲染同步

- [ ] 2.1 在 `src/npc/templates.py` 的 `render_implementer` 正文中注入与 1.1/1.2/1.3 语义一致的纪律文案（可复用或新增一个共享的 Markdown 片段常量，参考 `SELFCHECK_RUBRIC_MD` 的组织方式）。
- [ ] 2.2 在 `render_fixer` 正文中注入同一纪律文案，确保 fix 轮次（每轮独立 commit）同样受约束。
- [ ] 2.3 若引入新的共享常量，确认其不与 `SELFCHECK_RUBRIC_MD` 或既有"修复规则 A-D"段落重复/冲突，且不泄漏 review focus/rubric（不变量 1）。

## 3. 测试

- [ ] 3.1 在 `tests/test_templates.py` 新增断言：`render_implementer` 输出包含"禁止 `git add -A`"或等价关键文案。
- [ ] 3.2 在 `tests/test_templates.py` 新增断言：`render_fixer` 输出包含同一纪律关键文案。
- [ ] 3.3 跑 `uv run pytest tests/test_templates.py -q` 确认新增用例通过、既有用例不回归。

## 4. 收尾

- [ ] 4.1 跑 `uv run pytest -q` 全量确认无回归。
- [ ] 4.2 `openspec validate coder-atomic-add-discipline --type change --strict` 通过。
