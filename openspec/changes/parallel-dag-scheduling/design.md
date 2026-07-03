# parallel-dag-scheduling — Design

## Context

worktree-per-run（已 merge）给了每次 run 独立分支 `spine/<run_ts>` + 专属 worktree + ff-only 合回。但 run 内部仍是单编排者串行状态机：`spine-run.md` Step 3 对 plan_order 逐 SEQ 同步走完 implement→review-fix→archive 才进下一个。state.py 的游标模型（seq 线性推进）、resume.py 的断点检测、telemetry 的 phase 事件都假定串行。

2026-07 调研（docs/research/ 三份报告 + agent-team 方向调研）确立的边界：

- 并行只对**可分解**任务有效；编码类任务并行收益低于 research 类，除非文件不重叠。
- 3-5 并发是性价比拐点；token 成本随 agent 数线性涨。
- 真正的差异化能力在**合并策略**（merge queue、冲突回喂），不在隔离本身——隔离已由 worktree 解决。
- 长活 agent 团队 / 消息总线 / 多 coder 竞争：漂移>收益，明确不做。

## Goals / Non-Goals

**Goals:**

- 文件不重叠且无依赖的 change 并行执行，run 总时长从 Σ(全部) 降到 Σ(层数最深路径)。
- 合并冲突不再是终态：驱逐 + 冲突上下文回喂既有 fix 循环，让 coder 自己解决 rebase 冲突。
- 全部并行逻辑落在确定性底座（npc）：编排者仍只读一行 JSON 做分支，不新增自然语言协商。
- 串行是安全默认：DAG 分析信息不足 → 退化为现行为，diff 为零。

**Non-Goals:**

- 不做长活身份 agent、agent 间直连通信（SendMessage 总线）、多 coder 同题竞争、多 reviewer 评审团。
- 不改变 finalize 对主 checkout 的 ff-only 合回语义（run 分支 → base_branch 仍单一判定）。
- 不做跨 run 并行（同一时刻仍只有一个 run）。
- 不引入官方实验特性 Agent Teams（`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`）作为依赖——它尚在实验期；本设计只用稳定的 Task 工具并行 spawn。

## Decisions

### D1: DAG 数据源 = openspec 依赖声明 + tasks/specs 的 touched-paths 静态提取，不做语义预测

`npc plan dag` 只用确定性输入：① change 间显式依赖（proposal/tasks 里声明的前置 change）；② 各 change tasks.md / specs 中出现的文件路径（正则提取 + glob 归一）。两个 change 的路径集合有交集或路径信息为空 → 判为重叠。
**Why**：不变量「确定性机械动作归 npc」；让 LLM 预测会改哪些文件既不可靠又引入不可复现分支。**Alternative rejected**：让主 session 判断可并行性——违反"编排者不做语义分析"契约。
**保守性**：提取不到路径的 change 单独成层（串行）；依赖成环或依赖指向 plan_order 之外的 change → 整体退化为串行并在输出中报告原因。宁可少并行，不错并行。
**可解释性**：每个串行判定附 `serialization_reason`（触发热点路径 / no-paths / cycle / unknown-dep），并输出 `parallelizable_fraction` 估计——已用当前 13 个加固提案实测：spine-run.md 被 7 个 change 触碰、pipeline.py 4 个、state.py 3 个，harness 自举语料并行度趋近于零，诊断字段是让 /spine-analyze 能回答"为什么没并行"的唯一途径。

### D2: worktree 拓扑 = run worktree 之上叠 per-change worktree（仅并行层）

层 size=1：直接在 run worktree 干活，与现行为完全一致。层 size>1：每个 change 从 run 分支 HEAD 建 `spine/<run_ts>/<cid>` 分支 + 短命 worktree（置于 `~/.spine/worktrees/<run_ts>/<cid>`），coder 在其中执行；合回后立即拆除。
**Why**：串行路径零开销、零行为变化；并行 worktree 生命周期短（单 change），泄漏面小，且 init-crash-worktree-recovery 提案的孤儿回收机制可直接复用（按前缀扫描）。**Alternative rejected**：所有 change 一律 per-change worktree——串行 run 平白多一层 git 操作与失败面。
**路径绑定（硬约束）**：`load_paths()` 现按 cwd → repo_root → task_log 解析，per-change worktree 的绝对路径 / proj_key 与 run worktree 不同，cwd 推断必然指错 run。因此 per-change worktree 内的一切 npc 调用 MUST 显式绑定父 run（`--run-ts` + `--task-log-dir`，或在 per-change worktree 写入指向父 run 的 pointer 文件，二选一实施时定）；state 中该 change 记录 `exec_worktree` 字段，与 `Paths.repo_root` 分离。

### D3: merge queue = rebase → 复测 → ff → archive，失败即驱逐回喂；驱逐 rebase 由 npc 重放、coder 只解冲突

层内 change 的 review-fix 循环收敛（blocking==0）后进入队列。npc 逐个：rebase 到 run 分支 HEAD → `npc verify tests` 复跑（wire-verify-tests 的笼子直接复用）→ ff 合入 run 分支 → **在 run 分支上执行 archive**（`npc archive run`，openspec/ 树是共享写，归队列串行段）→ 拆 worktree。状态链 `merged → archived`，finalize 只认 archived 终态的语义不变。

**驱逐状态机（精确动作序列）**：rebase 冲突或复测失败 → 队列侧 `git rebase --abort`（run 分支在 ff 前始终未被修改，abort 只还原队列的工作现场）→ npc **在该 change 的 per-change worktree 里重放同一 rebase 并停在冲突处**（保留冲突标记），把冲突文件清单 + conflict diff（或测试失败输出）写入结构化 eviction 文件 → `npc fix run` 渲染 fix prompt 时注入（同 review findings 注入路径），并明确指令：coder MUST 解决 working tree 中已存在的冲突标记 + `git rebase --continue` + 修复复测失败，MUST NOT 自行发起新的 rebase/reset。修完重新排队。驱逐计数达上限 → `npc auto-decide --trigger merge-evicted`（默认 skip：该 change 标记 `skipped-auto` + `skipped_reason=merge-evicted`，不阻塞层屏障）。

**Why**：Ralphinho merge queue 的已验证模式——冲突信息完整回喂比盲重试收敛快得多；解冲突是"写代码"，按不变量归 coder，但**发起 rebase 是确定性机械动作，归 npc**——abort 后 worktree 里没有冲突标记，指望 coder 自己重跑 rebase 既越权又不可复现。archive 归队列：archive 写共享 openspec/ 树，放进 per-change worktree 必然在同层互相冲突。**Alternative rejected**：octopus merge / npc 自动解冲突（不可靠且违反生成⊥验证）；archive 在合回前的 per-change worktree 执行（共享写放进并行段，自造冲突）。

### D4: 并行 spawn 由编排者一条消息多个 Task 调用完成，屏障在层间；仅限 deferred 后端

`npc plan dag` 返回 `layers: [[cid...], ...]`。对每层：编排者对层内全部 `npc implement run --seq N` 逐个取 spawn_prompt，然后**单条消息并行发出多个 Task(spine-coder)**；逐个收 RESULT → `npc implement record`。review-fix 循环仍按 change 独立驱动（编排者轮询推进各 change 的状态机）。层内全部 change 到达终态（archived / failed / skipped-auto）才进下一层（层屏障）。并发上限 `[scheduler].max_parallel`（默认 3）：层 size 超限时 npc 在 dag 输出里自动把层再切片。

**后端边界**：并行 implement 仅在 coder 后端为 deferred（in-session Task）时生效。headless 后端（MiMo/codex 子进程）的 `npc implement run` 是阻塞调用，主 session 串行调用得不到并行——本期 headless 显式降级为串行执行（dag 分层与 telemetry 字段照记），异步 batch runner 列入明确不做。

**依赖失败传播**：显式依赖的前置 change 终态非 archived（failed/skipped-auto）→ 下游 change 自动 skip（`skipped_reason=dep-failed`），不 implement、不占并发额度；仅路径重叠（非显式依赖）不传播。

**Why**：Task 并行 spawn 是 Claude Code 稳定的一等公民能力；层屏障把状态空间限制在"一层内的 k 个独立状态机"，resume 复杂度可控。**Alternative rejected**：跨层流水线（layer N review 时 layer N+1 implement）——状态机组合爆炸，等指标证明层屏障是瓶颈再说（不变量 3）；依赖失败仍照跑下游——在缺失前置的基线上实现，产物必然错。

### D5: state 模型在现有 progress[] 上扩展 per-change 并行字段，给出与现有枚举的正式映射

现 schema 是 `progress[]`（非 `changes[]`），status 合法枚举与 finalize 收尾（只认 archived/failed/skipped-auto，state.py:519,538）保持不动。扩展方式：`progress[]` 每项**新增独立字段**而非改动 status 枚举——`dag_layer`、`merge_status`（pending/queued/evicted/merged）、`eviction_count`、`change_branch`、`exec_worktree`。正式映射：

| 阶段 | progress.status | merge_status |
|---|---|---|
| implement/review/fix 中 | 现有值不变 | pending |
| review 收敛入队 | 现有值不变 | queued |
| 合回失败被驱逐 | 现有值不变（回 fix 循环） | evicted |
| ff 合入 run 分支 | 现有值不变 | merged |
| queue 内 archive 完成 | **archived**（+ archive_commit） | merged |
| 驱逐超限 skip | **skipped-auto**（`skipped_reason=merge-evicted`） | evicted |
| 依赖失败传播 | **skipped-auto**（`skipped_reason=dep-failed`） | pending |

层屏障与 finalize 的终态判定统一用 progress.status ∈ {archived, failed, skipped-auto}，与现有代码零冲突；`merge_status` 只是并行期的中间态标注。`resume detect` 按层重建：找到第一个未收敛层，输出结构化 payload：`{layer, changes: [{change_id, seq, status, merge_status, phase, round, eviction_count, change_branch, exec_worktree}], blocked: [...]}`；旧 state 无新字段按串行语义解释。
**Why**：resume 是硬约束（续跑优先 guardrail），并行状态必须完整可恢复；不改 status 枚举、不动 finalize，是把 diff 面压到最小的方式。**Alternative rejected**：把 merged/evicted 塞进 status 枚举——finalize/resume/telemetry 全部要跟着改，破坏旧 run 兼容。

### D6: state 变更引入 run 级互斥锁

npc 现有 state 写入是 read-mutate-write + `os.replace`：防撕裂、不防丢写。并行 review 子进程、merge queue、编排者 record 可能并发触发 npc 进程同时写同一 state.json。所有 state 变更 MUST 先取 run 级锁（state.json 同目录 `state.lock`，`fcntl.flock` + 超时重试，超时上报错误不静默降级），锁内完成 read-mutate-write。telemetry append 同锁保护（或改 O_APPEND 单行写，实施时定）。
**Why**：丢写的表现是"record 成功但状态没变"——正是 orchestrator-check-record-result 要消灭的那类静默失败，并行会把它从偶发变成必然。**Alternative rejected**：乐观 CAS/重试——实现更复杂，且本地单机场景 flock 足够。

## Risks / Trade-offs

- [路径静态提取漏报 → 两个"不重叠" change 实际改同一文件] → merge queue 的 rebase+复测是兜底：冲突被捕获、驱逐、回喂，最坏退化为串行修复，不会静默合坏。
- [并行放大既有静默失败路径（record 被吞、worktree 泄漏、coder 挂起）] → 硬前置：orchestrator-check-record-result、init-crash-worktree-recovery、in-session-coder-timeout 三提案必须先落地；tasks.md 首项即验证此前置。
- [token 成本上升（k 个并发 coder ≈ k 倍执行层 token）] → 执行层本就是 Sonnet/mimo 廉价层；telemetry 新增字段让 /spine-analyze 可量化"并行节省的 wall-clock vs 多花的 token"，指标不划算可把 max_parallel 调回 1（即全串行）。
- [层屏障下木桶效应：一个慢 change 拖住整层] → 接受；这是简单性代价，telemetry 的 layer 时长分布会暴露严重程度，再决定是否做跨层流水线。
- [并发 review 需要多个 codex 子进程] → review 本就是 headless 子进程，可并发；上限同受 max_parallel 约束。

## Migration Plan

1. npc 侧纯增量：`plan dag`、merge_queue 模块、state 新字段（旧 state 无新字段按串行解释）——向后兼容，旧 run 的 resume 不受影响。
2. spine-run.md 的 Step 3 改写为按层调度，但 `layers` 全部 size=1 时执行路径与现版本逐字等价。
3. 默认 `max_parallel=3`；首次可设 1 灰度（DAG 分析跑、telemetry 记、但不真并行），观察一两个 run 的 dag 字段正确性后再放开。
4. 回滚：`[scheduler].max_parallel=1` 即回到串行，无需还原代码。

## Open Questions

- ~~touched-paths 提取的召回率需要真实 change 语料验证~~ 已验证（2026-07-03）：13 个加固提案语料上热点文件（spine-run.md×7 / pipeline.py×4 / state.py×3）使并行度趋近零——结论转化为 D1 的 serialization_reason 诊断要求与 proposal 的收益边界声明。
- eviction 文件的 schema（放 focus.py 的 findings 通道还是独立通道）实施时定，倾向复用 findings 注入路径。telemetry 事件名与 payload 已在 merge-queue-eviction spec 定稿（`merge_enqueued`/`merge_done`/`merge_evicted`/`merge_evict_limit`），不再是 open question。
- per-change worktree 的 run 绑定用 CLI 参数（`--run-ts --task-log-dir`）还是 pointer 文件，实施时定（D2）。
- 命名统一：并行相关的 skip 一律沿用现有 `skipped-auto` + `skipped_reason`，不新增 `skipped` 状态。
