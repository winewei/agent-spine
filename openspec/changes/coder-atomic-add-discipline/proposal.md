## Why

`docs/optimization-proposals/2026-07-09-bun-migration-lessons.md` 提案 4 记录了一个被观察到的失败模式（Bun 迁移 false start #1）：并行 worker 互踩 git 状态，根因之一是 `git add -A` 把无关文件一并卷入 commit。agent-spine 的单元素层（非并行层）`spine-coder` 直接在 run worktree 里跑，同一 worktree 内可能并存 telemetry/state 等 npc 写入的文件；若 `spine-coder` 用 `git add -A` / `git add .`，即便没有并发 worker，也可能把不属于本次 change 的文件误 add 进 commit，污染 diff、混淆 review 归因、且难以事后审计"这个 commit 到底改了什么"。并行层（per-change worktree）已物理隔离，但物理隔离不能替代提交纪律——两者是正交防线。

当前 `plugins/agent-spine/agents/spine-coder.md` 的 Guardrails 与 `src/npc/templates.py` 渲染的 implement/fix prompt 都没有对 `git add` 范围做任何约束，也没有要求 commit 的文件清单与 summary.md 的"改了什么"清单保持一致——commit 内容和过程日志可以互相矛盾而不被察觉。

## What Changes

- **MODIFIED** `plugins/agent-spine/agents/spine-coder.md` Guardrails：新增一条纪律——只 `git add` 自己明确改动的文件（逐文件枚举，不用通配 add）；禁止 `git add -A` / `git add .` / `git stash` / 任何破坏性 git 操作（`git reset --hard`、`git checkout --` 覆盖未提交改动等）；commit 的文件清单必须与 summary.md 里逐文件改动清单一致——这是自报口径，可被 reviewer（`spine-code-reviewer` / 人工复盘）核验，不是本 change 引入的确定性 gate。
- **MODIFIED** `src/npc/templates.py`：`render_implementer` 与 `render_fixer` 渲染的 prompt 正文同步注入同一条纪律（与 spine-coder.md 文案语义一致），确保无论走 in-session 分发（读 spine-coder.md）还是 headless 分发（读渲染后的 prompt 文件）都受同一约束。
- **不引入**任何新的 npc 确定性校验命令；本 change 是纯 prompt/agent 契约文本层面的约束加固。

**非目标（Non-Goals）**：

- **不新增确定性 gate**：不在 `npc implement record` / `npc fix record` 里新增"commit 文件清单 vs summary.md 清单"的自动比对校验。已有先例（`spine-spec-writer` 的 `out_of_scope_changes` 校验）证明这类校验是可行的，但本 change 的范围严格限定为 prompt/契约文本；若后续观测到自报口径不可靠（如 telemetry/code-review 归因显示该类问题反复出现），应作为独立 change 引入确定性比对，而不是在本 change 里顺手加。
- **不改变** `git commit` 的触发时机、commit message 格式、或既有 fix 阶段"每轮独立 commit"的规则。
- **不影响**并行层 per-change worktree 隔离机制，也不替代它——两者是正交防线，本 change 只加固单 worktree 内的提交纪律。
- **不约束** `spine-spec-writer`（该 agent 本就 MUST NOT commit，见其自身 Guardrails）。
- **不改变** `npc implement/fix` 任何 CLI 行为或 RESULT schema。

## Capabilities

- **New Capabilities**: `coder-atomic-add-discipline` —— spine-coder 的原子 `git add` 纪律与 commit 文件清单 ↔ summary.md 一致性的自报契约。

## Impact

- **受影响代码**：`plugins/agent-spine/agents/spine-coder.md`（Guardrails 段新增纪律）、`src/npc/templates.py`（`render_implementer` / `render_fixer` 正文同步注入）、`tests/test_templates.py`（补充断言：渲染出的 prompt 含新纪律关键文案）。
- **兼容性**：纯新增约束文本，不改变任何函数签名、CLI 参数或 RESULT schema；既有 implement/fix 流程行为不变。
- **不变量影响**：不涉及不变量 1（生成 ⊥ 验证，本 change 不动 review/routing 逻辑）；不涉及不变量 2（不引入需要信任 LLM 散文的新判定路径——一致性检查明确标注为自报，不是被当作确定性信号消费）；不涉及不变量 3（不新增基于历史数据标定的硬轨阈值）。
