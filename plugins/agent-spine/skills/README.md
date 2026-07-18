# new-plan-changes 系列 skill 索引

面向人的版本说明与选择指南。SKILL.md 是纯 agent 执行契约，不含解释性内容；背景、差异、取舍都在本文件。

## 版本索引

| 版本 | 位置 | 执行模型 | npc 要求 | 状态 |
|---|---|---|---|---|
| v2 | `../commands/new-plan-changes-v2.md` | 串行：DAG 拓扑排序后逐个 implement→review→fix→archive | ≥ 1.4 | 稳定 |
| v3 | `new-plan-changes-v3/SKILL.md` | 波次并行：worktree 并行 implement，主 session 手工编排整合与内环 | ≥ 1.4 | 稳定，被 v4 取代中 |
| v4 | `new-plan-changes-v4/SKILL.md` | 波次并行 + 上下文预算：整合与内环各下沉为一条 npc 命令 | ≥ 1.5 | 推荐 |

三者共享同一 npc 底座（状态/事件/续跑/telemetry，全部落 `~/task_log/<PROJ_KEY>/`），run 之间可互相续接的前提是 state schema 一致。

## 版本差异

**v2 → v3**：把最贵的 implement 环节并行掉。同一 DAG 拓扑层级、文件不相交、且经双架构师 sub-agent 裁定无语义耦合的 changes，在各自 git worktree 内并行 implement；写完串行 cherry-pick 回 main，review/fix/archive 留在主分支线性链上逐字复用 v2。DAG 抽取（dag-analyst）与语义裁定（senior-system-architect + senior-code-developer）都在 sub-agent 内闭环，主 session 不读 N×4 份 change 文档。

**v3 → v4**：上下文预算重构（设计文档：`docs/optimization-proposals/2026-07-05-orchestration-context-budget.md`）。v3 的主 session 仍要手工编排两大段流程——manifest 核验→cherry-pick→hash 翻译→record→verify tests，以及逐轮搬 review JSON 的 fix 循环；长 run（10+ changes）下主 context 线性膨胀，compaction 后易丢盘面。v4 把这两段下沉进 npc 1.5：

- `npc integrate`：整合一条命令，verify-tests 失败自动 revert，main 始终绿；
- `npc change run`：单 change 的 review→fix 循环→archive 一条命令，交互档决策点以 exit 5 冒泡；
- `npc status --brief` + `npc state note`：compaction 后单命令重建盘面 + 人经 note 下发转向指令；
- 波间 re-plan 检查点：cherry-pick 冲突 / 下游依赖被 skip 时对剩余集合重新切波。

效果：主 session 每推进一个 change 只消耗 O(1) token（~400），只在真正的决策分叉点出场。

## 选择建议

- change 数量少（≤3）或依赖链强 → v2，并行收益小、编排最简单。
- change 多且弱依赖 → v4。v3 仅在 npc < 1.5 的环境下作为回退。
- 并行收益集中在 implement 阶段；review/fix/archive 波内串行，总墙钟不随并行度等比缩短。
- 波次切分质量依赖各 proposal 的 Affected Code 段完整度；写不全时靠架构师裁定与 cherry-pick 冲突兜底（代价是该 change 退回串行重做）。

## 共同前置

- `npc doctor` 通过（git 必需；codex 缺失走降级：跳 review）
- `openspec` 对本系列 skill 为必需：计划入口是 `openspec list --json`，缺失时在 Step 2 即失败，走不到"只 commit 不 archive"的降级路径
- `npc verify routing` 无 violation（coder 与 review 引擎不同源；MiMo 不承担 review）
- v3/v4 额外要求：git 工作树 clean；`worktree.baseRef=head`（`.claude/settings.json` 或 `~/.claude/settings.json`），未设置即报错退出，绝不静默以 fresh 语义分叉（会丢前序波次成果）

## 维护约定

- skill 零脚本：机械逻辑全部在 npc（`src/npc`）。skill 行为与 npc 不符时修 npc 并 `uv tool install --force --from . npc` 重装，不在 skill 里补 bash。
- 命令契约源：`docs/cli.md`；架构不变量：`docs/principles.md`。
