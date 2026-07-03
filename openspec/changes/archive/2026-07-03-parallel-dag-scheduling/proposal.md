# parallel-dag-scheduling

## Why

当前编排是严格单编排者串行：`spine-run.md` Step 3 逐 SEQ 同步走 implement→review-fix→archive，主 session 全程同步等待。三个实测瓶颈：① review（codex 30-60s/轮）阻塞编排；② 文件不重叠、无依赖的 change 本可并行却只能排队；③ N 个 change 的总时长 ≈ Σ(每个 change 全流程)，run 越大越不可接受。2026-07 的多 agent 调研结论（docs/research/2026-07-02-anthropic-harness-best-practices.md）：并行只对可分解任务有效、协调成本随 agent 数线性涨——因此不做长活 agent team / 消息总线，只做**DAG 分层并行 + merge queue**：这是 worktree-per-run 隔离（已 merge）的自然升级，隔离基础设施已就位，缺的只是调度层与合并策略。

**收益边界（如实声明）**：对 harness 自举类 change 语料（如当前 13 个加固提案），热点文件（spine-run.md 被 7 个 change 触碰、pipeline.py 4 个、state.py 3 个）使实际并行度趋近于零——本 change 在此类语料上的直接收益是**零成本退化为串行且行为不变**；并行收益要在文件天然分散的业务仓库 change 上兑现。因此 telemetry 的热点诊断（serialization_reason）是本 change 的一等产物，不是附属品。

## What Changes

- **npc 侧新增 `npc plan dag`**：对 plan_order 里的 change 做确定性文件重叠分析（读各 change 的 tasks/specs 声明的 touched paths + openspec 依赖声明），输出 DAG 分层（layers）+ 每个串行判定的 `serialization_reason`（哪个热点路径/缺失信息导致）。同层 change 互不重叠可并行，跨层串行。分析失败、声明缺失、依赖成环或依赖不在 plan_order → 保守退化为串行，行为不变。
- **依赖失败传播**：显式依赖的前置 change 未达 archived 终态（failed/skipped）时，下游 change SHALL 自动 skip（`skipped_reason=dep-failed`，telemetry 记录）；仅路径重叠（非显式依赖）不传播。
- **worktree 粒度从 per-run 细化为 per-change**（仅并行层）：同层的每个 change 在 `spine/<run_ts>` 分支之上各建短命 worktree/分支 `spine/<run_ts>/<change-id>`；层内全部收敛后按 merge queue 逐个合回 run 分支。单 change 层沿用现行为（直接在 run worktree 干活），零额外开销。
- **merge queue + 驱逐回喂**：层内 change 完成后排队 rebase 到 run 分支 → 复跑测试 → ff 合入；rebase 冲突或测试失败即**驱逐**（evict），npc 把 rebase 重放进该 change 的 worktree（保留冲突标记）并把冲突 diff / 失败输出结构化写入 fix 上下文，coder 只负责解决冲突与修复（走既有 3b fix 循环）后重新排队；驱逐超上限（默认 2）→ 转 3d 决策点（`npc auto-decide --trigger merge-evicted`）。
- **archive 移到 merge queue 之后**：并行层的 change 收敛后先合回（merged），archive 由 queue 在 run 分支上**串行**执行（openspec/ 树是共享写，天然属于串行段）；`merged → archived` 是新的状态链，finalize 语义（只认 archived 终态）不变。单 change 层沿用现行为（review 收敛即 archive）。
- **state 并发安全**：npc 的全部 state 变更引入 run 级文件锁（flock + 重试），消除并行 review 子进程 / merge queue 并发 read-mutate-write 丢更新。
- **per-change worktree 显式绑定父 run**：per-change worktree 内的一切 npc 调用 MUST 显式携带 `--run-ts`（或等价 pointer），不依赖 cwd 推断 task_log（`load_paths()` 按 cwd 解析会指错 run）。
- **spine-run.md Step 3 改为按层调度**：`npc plan dag` 给出 layers 后，编排者对同层 change **并行 spawn**（多个 Task 调用同一消息发出），逐个收 RESULT 装订；review-fix 循环仍按 change 独立跑。并发上限默认 3（可配 `[scheduler].max_parallel`），遵循官方 3-5 agent 性价比拐点结论。**并行仅限 deferred（in-session Task）coder 后端**：headless 后端（MiMo/codex 子进程）的 `npc implement run` 是阻塞调用，本期明确降级为串行（dag 输出照记、telemetry 照打，不真并行），异步 batch runner 留待指标证明需要。
- **续跑/telemetry/finalize 感知并行**：`npc resume detect` 能报告层内各 change 的独立断点；telemetry 新增 `dag_layer`、`merge_evictions` 字段；finalize 的 ff-only 合回主 checkout 语义不变（run 分支 → base_branch 仍是单一 ff 判定）。
- **明确不做**（遵不变量 3，见 design.md）：长活身份 agent、agent 间直连通信、多 coder 同题竞争、多 reviewer 评审团、headless 异步 batch runner、跨层流水线。

## Capabilities

### New Capabilities

- `plan-dag-analysis`: 确定性的 change 文件重叠 + 依赖分析，产出可并行的 DAG 分层；信息不足时保守退化为串行。
- `parallel-layer-scheduling`: 编排层按 DAG 层并行 spawn coder 的调度契约——并发上限、worktree-per-change 隔离、层屏障（全层收敛才进下一层）。
- `merge-queue-eviction`: 层内 change 收敛后的排队合回契约——rebase→复测→ff 合入；失败驱逐并把冲突/失败上下文回喂 fix 循环，超限转 auto-decide。

### Modified Capabilities

<!-- 现有 specs 目录为空（13 提案尚未 archive），无已建立的 capability 需要 delta。
     与 worktree-per-run（已 merge 进 main，未落 spec）的行为交互在 design.md 说明。 -->

## Impact

- `plugins/agent-spine/commands/spine-run.md`（Step 2 后插 `npc plan dag`；Step 3 改按层调度；Guardrails 补并发上限与层屏障）
- `src/npc/plan.py`（新增 dag 子命令：重叠分析 + 分层 + serialization_reason）
- `src/npc/git_ops.py` / 新 `src/npc/merge_queue.py`（per-change worktree 生命周期、rebase→测试→ff、驱逐 rebase 重放、archive 串行段、驱逐上下文结构化输出）
- `src/npc/state.py` / `src/npc/resume.py`（progress[] 扩展 per-change 并行字段与状态映射；run 级文件锁；并行断点续跑）
- `src/npc/paths.py`（per-change worktree 下的显式 run 绑定，不依赖 cwd 推断）
- `src/npc/config.py`（`[scheduler]` 节：max_parallel / eviction 上限，含校验与 telemetry 记录生效值）
- `src/npc/auto_decide.py`（新 trigger `merge-evicted`）
- `src/npc/telemetry.py` + `telemetry_schema_v1.json`（`dag_layer`、`merge_evictions`）
- `tests/`（dag 分层、merge queue 驱逐链、并行 resume 用例）
- **依赖前置**：本 change 假定 13 个加固提案（尤其 orchestrator-check-record-result、init-crash-worktree-recovery、in-session-coder-timeout）已落地——并行会放大所有静默失败路径，未加固前不应实施。
