## Context

`docs/optimization-proposals/2026-07-09-bun-migration-lessons.md` 提案 4 观察到：Bun 迁移的第一个 false start 是并行 worker 互踩 git 状态，`git add -A` 是根因之一。agent-spine 已有并行层物理隔离（per-change worktree），但 `spine-coder` 在单元素层直接跑在 run worktree 里，该 worktree 内可能并存 npc 自身写入的 telemetry/state 文件；`git add -A` / `git add .` 会把这些无关文件一并卷入 commit。同时，`spine-coder` 的双产物契约（commit + summary.md）目前没有要求两者对"改了什么"给出一致的口径——commit 的实际文件集合可以和 summary.md 声称的"Files Modified"清单不一致而不被任何机制察觉。

## Goals / Non-Goals

**Goals**：
- 让 `spine-coder` 在 implement 与 fix 两个阶段都遵守"只 add 明确改动的文件"的纪律，且该纪律在 in-session 分发（读 `spine-coder.md`）与 headless 分发（读渲染后的 prompt 文件）下一致生效。
- 让 commit 文件清单与 summary.md 的改动清单可被人工或 reviewer 事后核对，形成一条可审计的自报线索。

**Non-Goals**：
- 不追求"commit 文件清单 = summary.md 清单"的确定性自动校验——这需要在 `npc implement record` / `npc fix record` 里解析 commit diff 并与 summary.md 文本做语义比对，属于更重的确定性 gate 工程，超出本 change 范围。
- 不改变并行层的 worktree 隔离机制本身。

## Decisions

**决策 1：文本纪律下沉到两个渲染入口，而非只改一处。**

`spine-coder` 的行为受两条路径影响：(a) in-session 分发时，编排者用 Task 工具 spawn `spine-coder` subagent，该 subagent 的 system prompt 来自 `plugins/agent-spine/agents/spine-coder.md`；(b) 无论 in-session 还是 headless，任务本身的具体指令都来自 `src/npc/templates.py` 渲染出的 `implement.prompt.md` / `round-N.fix.prompt.md`（`spine-coder.md` 自身也要求"先 Read 那个已落盘的 prompt 文件，严格按它执行，优先级高于默认习惯"）。若只改 `spine-coder.md` 不改 `templates.py`，headless 分发（如 mimo 后端）读到的渲染 prompt 里不会出现这条纪律；若只改 `templates.py` 不改 `spine-coder.md`，agent 的默认 Guardrails 层缺这条约束。两处必须同步，且文案语义一致（不要求逐字相同，允许各自融入所在文档的行文风格）。

备选方案：只在 `templates.py` 里加，理由是"prompt 文件优先级更高、agent 契约是兜底"。否决原因：`spine-coder.md` 明确把自己的 Guardrails 列为"绝不 XX"级别的硬约束清单（如"绝不 git commit"这类既有条目），是任何人快速审阅 agent 能力边界时的第一入口；只改 prompt 会让 Guardrails 清单本身失真（读 `spine-coder.md` 看不出这条限制存在）。

**决策 2：commit 文件清单 ↔ summary.md 一致性采用自报口径，不引入确定性比对 gate。**

理由：确定性比对需要（a）从 commit 里提取"实际改动文件集合"（`git show --stat` 或 `git diff --name-only`），（b）从 summary.md 的自由文本"Files Modified"段解析出文件路径列表，（c）两者做集合比对并处理合理的口径差异（如 summary.md 用相对路径、commit diff 用仓库根相对路径；新建文件 vs 修改文件的措辞差异）。(b) 步骤本质上是解析 LLM 生成的散文，容易脆弱且误报率高——这正是 harness 的核心不变量之一（不信任 LLM 散文作为确定性信号源）要规避的模式。因此本 change 只把"清单必须一致"写成 agent MUST 遵守的自报纪律，交由 reviewer（`spine-code-reviewer` 的语义评审，或人工复盘）核验，不伪装成确定性门。

若未来 telemetry / code-review 归因显示"commit 与 summary 不一致"是被反复观测到的真实问题（呼应不变量 3：新硬轨须被真实方差打出来），应作为独立 change 引入确定性比对，而不是在本 change 里顺手加一个未经验证必要性的新 gate。

## Risks / Trade-offs

- [风险] 自报口径不是确定性强制，`spine-coder` 仍可能违反纪律而不被拦截 → [缓解] 交由既有 `spine-code-reviewer` 语义评审覆盖（其评审范围本就包含"diff 是否符合 change 范围"一类判断），且本 change 的 Non-Goals 已明确标注升级路径（若观测到真实问题再引入确定性 gate）。
- [权衡] 两处文案（`spine-coder.md` 与 `templates.py`）需要人工保持语义同步，未来若一方修改另一方忘改，会出现"agent 默认 Guardrails 与实际收到的 prompt 不一致"的漂移 → 通过 `tests/test_templates.py` 新增断言锁定 `templates.py` 侧的关键文案存在，降低漂移风险；`spine-coder.md` 侧无自动化锁定手段，依赖 review。

## Migration Plan

纯文本改动，无需迁移步骤或回滚特殊处理；对既有 run 无影响（新纪律只影响后续新 spawn 的 `spine-coder` 任务）。

## Open Questions

无。
