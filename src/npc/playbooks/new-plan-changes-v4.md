---
name: new-plan-changes-v4
description: 波次并行推进所有活跃的 openspec changes：DAG 抽取与架构师裁定走 sub-agent，implement 并行 worktree，整合与单 change 内环各一条 npc 命令（npc integrate / npc change run）。当用户说"并行推进 changes""new-plan-changes-v4""v4 跑 openspec changes"时触发。
category: OpenSpec
tags: [openspec, plan, implement, parallel, worktree, v4]
---

> **宿主适配**：本 playbook 是宿主中立的主 session 指令，可在任意 agent CLI（Claude Code / Kimi CLI / Qwen Code / Codex / OpenCode / …）内执行。文中的宿主机制按下表映射，宿主缺某机制时用通用回退：
>
> | 文中写法 | Claude Code | 其它宿主通用回退 |
> |---|---|---|
> | `Agent(...)`（spawn sub-agent） | `Agent` 工具 | 宿主的 sub-agent 派发机制；没有则改用 `npc implement run` / `npc fix run`（headless coder 子进程，效果等价） |
> | `isolation="worktree"` | Agent 工具参数 | 用 Bash `git worktree add` 手建隔离工作区，或把该波降级为串行 `npc implement run` |
> | `AskUserQuestion` | 同名工具 | 直接向用户提问并等待回复 |
> | `TodoWrite` | 同名工具 | 宿主的任务清单机制；没有则维护一份 markdown 清单 |
> | `EnterPlanMode` / `ExitPlanMode` | plan 模式审批门 | 打印计划全文，请用户确认后继续（`--auto` 档两边都跳过） |
> | 后台执行 `npc change run`（Step 3d） | Bash 工具 `run_in_background: true`，完成通知回主 session | `nohup … > <log> 2>&1 &` 记 PID 轮询；两者都没有则前台串行执行——正确但丧失流水化收益，等价于 `--serial-waves` |

# new-plan-changes-v4

## 前置（不满足即报错退出）

- `npc --version` ≥ 1.5.0
- `npc doctor` 通过；缺 codex → 跳 review，记降级项、不阻塞
- `openspec` 可用（`openspec list --json` 是计划入口）
- git 工作树 clean
- worktree 隔离可用：Claude Code 宿主要求 `worktree.baseRef=head`（`.claude/settings.json` 或 `~/.claude/settings.json`），否则报错退出、不静默退化；其它宿主用 Bash `git worktree add`（基于 HEAD）等价实现

## 参数

| flag | 默认 | 含义 |
|---|---|---|
| `--auto` | off | 决策点全走 auto-decide；唯一真停是 exit 3 与环境前置失败 |
| `--fresh` | off | 忽略 in-progress 旧 run |
| `--max-parallel N` | 4 | 并发 implementer 上限（流水线槽位数） |
| `--serial-waves` | off | 退回逐波屏障流程（Step 3S），不做流水化 |
| `--no-architect` | off | 跳过语义裁定，直接用机械候选波次 |
| `--webhook URL` / `--webhook-format` | env / raw | 进度外呼（`npc notify`，永不阻塞） |

## Step 1 — 初始化 / 重定向

```bash
INIT=$(npc init ${AUTO:+--auto} ${FRESH:+--fresh})   # needs_resume / state_drift 看这里
```

- `state_drift.total_drifted > 0` → `npc state repair --auto`。
- `needs_resume=true`、经历过 context compaction、或接手他人 session → 先跑 `npc status --brief`，以其 `pending_decisions / notes / next_action` 重建盘面，不信任记忆里的进度。
- `init --auto` 弄脏 `.claude/settings.json` 时（仅 claude 宿主会写；其它宿主 init 自动跳过）：tracked → `npc git commit --message "chore: npc auto-auth settings"`；untracked → 写入 `.git/info/exclude`。

## Step 2 — 计划（全部在 sub-agent 里）

不读任何 proposal/design/tasks/spec 原文。

1. `openspec list --json` 只取 in-progress change 名 → NODES。
2. spawn `dag-analyst`（Explore，只读）：读 N×4 份文档 → 抽 nodes/edges/files（目录级条目用 Grep 展开）→ 跑 `npc plan waves` → 写 `<run_dir>/v3-dag-extract.json` → 回一行 RESULT。校验 nodes 完整、candidate.waves 展平=nodes；失败重发一次，再失败 `--auto` 才降级自抽（记 `dag_extract_fallback`），交互档真停。
3. spawn 双架构师（并行，只读；`--no-architect` 跳过）：A=senior-system-architect 查语义耦合（共享状态/时序/不变量），B=senior-code-developer 查落地冲突（真实文件/import/构建）。任一判 serialize 即拆；提级须双方 independent+给理由。得 FINAL_WAVES。
4. 落地：

```bash
npc state init-run --plan-order "$(jq -nc --argjson w "$FINAL_WAVES" '$w|add')" --goal "<用户的原始目标一句话>"
echo "$FINAL_WAVES" > "$RUN_DIR/v4-waves.json"
```

打印 Wave Plan Summary（波次、拆分理由、降级/提级）留痕到 run.events.jsonl。交互档 ExitPlanMode 等批准；`--auto` 不进 plan 模式直接跑。

## Step 3 — 流水线（事件驱动）

`--serial-waves` 时跳过本节，走 Step 3S。

**为什么不逐波屏障**：波内 change 的完成时刻差异极大——一个 change 一轮过、另一个修四轮，屏障让整波等最慢者，下一波的 implement 空转数小时。实施与内环的依赖其实是错位的：下一个 change 的 **implement 只需要依赖项的代码在 main 上**（即 integrate 完成），不需要依赖项走完 review/fix/archive。因此 implement 在上一波内环期间即可于 worktree 内起跑。

`v4-waves.json` 保留为**初始计划与人审展示物**，波号只用于 telemetry 与汇报；实际调度由 `npc plan ready` 按运行时集合逐次判定，不再有屏障。

维护三个集合（主 session 内存，每次变更记 run.events.jsonl）：

| 集合 | 含义 | 何时变 |
|---|---|---|
| DONE | 已 `npc integrate` 成功、代码在 main 上 | integrate exit 0 时 `+= CID` |
| ACTIVE | implementer 正在 worktree 中跑 | spawn 时 `+= CID`；返回时 `-= CID` |
| INNER | 已整合，排队或正在跑 `npc change run` | integrate 成功时 `+= CID`；内环结束时 `-= CID` |
| PENDING | implementer 已返回、等待整合（内环占用 main 期间） | implementer 返回时 `+= CID`（同时 `ACTIVE -= CID`）；integrate 成功或终态失败时 `-= CID` |

`MAX_PARALLEL` 默认 4（`--max-parallel N` 覆盖，Step 1 由用户指定）。

**3a. 问闸**——任何时候要决定"现在还能开哪些"，都是这一条（DONE/ACTIVE 为 JSON 数组字符串，`SLOTS = MAX_PARALLEL - |ACTIVE|`，`SLOTS<=0` 时不必调用）：

```bash
DAG="$RUN_DIR/v3-dag-extract.json"     # Step 2 产物：nodes / edges / files / tie_break
READY=$(jq -c --argjson done "$DONE" --argjson active "$ACTIVE" --argjson pending "$PENDING" --argjson lim "$SLOTS" \
          '{nodes,edges,files,tie_break} + {done:$done, active:($active + $pending), limit:$lim}' "$DAG" \
        | npc plan ready | jq -r '.ready[]')
```

`active` 参数传 **ACTIVE ∪ PENDING**：待整合的 change 虽不占 implementer 槽位，但必须留在排除集里——否则下一次问闸会把它再判为 ready、重复 spawn，产生两份互相竞争的 commit 与 manifest；其文件即将落到 main，与它有文件交集的 change 也应等它整合完再开。`SLOTS` 只按 `|ACTIVE|` 算，PENDING 不消耗槽位。

初始开闸即 `DONE=[]`、`ACTIVE=[]`、`SLOTS=MAX_PARALLEL`，把返回的 ready 在**同一消息里**并发 spawn（3b）。

**3b. spawn 一个 implementer（worktree 隔离）**——每开一个 change 都走这三行，然后 `ACTIVE += CID`：

```bash
npc state add-change "$SEQ" "$CID" && npc phase rotate --seq "$SEQ" --to implement
npc agent prompt render --phase implement --change-id "$CID"
GUIDE=$(npc agent spawn-prompt --phase implement --change-id "$CID" | jq -r .prompt)
```

`Agent(senior-code-developer, isolation="worktree", prompt=GUIDE+worktree 契约)`。worktree 契约：写 manifest JSON 到 `<run_dir>/v3-manifests/<cid>.json`（files_written 对象数组），最后输出 `RESULT:` 行 + `MANIFEST:` 行。

worktree 基线：spawn 时的 main HEAD 已含其全部依赖的 implement commit（`plan ready` 的 `dep-pending` 保证），但依赖项**后续的 fix commit** 不在基线里，可能引起 cherry-pick 冲突——由 3c 的失败分支与 3f 的 re-plan 路径处理，不预防性等待。

**3c. 收单整合 + 立刻续闸**——每个 implementer 返回后一条命令（核验/cherry-pick/record/verify tests/revert 全在内）：

```bash
npc integrate --seq "$SEQ" --result "<RESULT 行>" --manifest "<MANIFEST 路径>"
```

**main 互斥（硬约束）**：整合与内环都改 main worktree 和 state.json（cherry-pick / 跑测试 / fix commit / archive commit / 读改写 progress），**二者绝不并发**。规则：3d 的后台 `change run` 在跑时，implementer 返回的 RESULT/MANIFEST 只入 `PENDING` 队列（按返回顺序），不调 `npc integrate`；每次内环完成通知到达、且在启动下一个 `change run` **之前**，先把 `PENDING` 全部整合完（逐条 `npc integrate`，每条成功后续闸 3a），再起下一个内环。npc 层有两把真锁兜底：`npc integrate` 与 `npc change run` 全程互斥持有 `<task_log_dir>/.main.lock`（integrate 拿不到即返回 `step=inner-loop-active`，无副作用，收到即入队，不要 `--force`）；所有 state 装订（含 3b 的 `state add-change` / `phase rotate`）都在 `<state>.lock` 下读改写，所以内环在跑时**继续 spawn 是安全的**——spawn 只写 state 与 worktree，不碰 main。implementer 在 worktree 内继续跑，不受排队影响。

- exit 0 → `ACTIVE -= CID`、`DONE += CID`、`INNER += CID`，然后**立刻**重跑 3a 的 `npc plan ready`（新的 DONE/ACTIVE），把新 ready 的 change 在**同一条消息里**并发 spawn 填满槽位，同时按 3d 推进内环队列。不要攒到"本波结束"再问闸。
- 失败看 `.step`：`inner-loop-active` → `PENDING += CID`（已 `ACTIVE -= CID`），内环结束后重试；不算失败，不进 DONE，但仍在 3a 的排除集里；`verify-manifest`（plan-only）→ 重发该 implementer 一次（前缀 "IMPLEMENT NOW"），再失败标 failed 并 `ACTIVE -= CID`；`cherry-pick` → 记入 re-plan 信号（见 3f），该 change 改串行 `npc implement run --seq $SEQ`；`verify-tests`（已自动 revert）→ `npc auto-decide --seq $SEQ --trigger implementer-failed --apply`。
- 任何失败都要把 CID 移出 ACTIVE 并重跑一次 3a——否则槽位泄漏，流水线越跑越窄。

**3d. 内环（后台执行，SEQ 序串行）**——对 INNER 队列按 SEQ 升序**一次只跑一个**：

```bash
npc change run --seq "$SEQ" --from review ${AUTO:+--auto} > "$RUN_DIR/change-run-$SEQ.json" 2>&1
```

后台起（Claude Code：Bash `run_in_background: true`；其它宿主见顶部适配表），主 session 不阻塞，继续处理 implementer 返回（入 `PENDING`）与 spawn。**内环不并发、也不与整合并发**：review/fix/archive 的 commit 与 integrate 的 cherry-pick 都落在 main 上，同时跑会互相打架、测错 HEAD、覆盖 state。

完成通知到达后读 `$RUN_DIR/change-run-$SEQ.json` 一行 JSON，`INNER -= CID`；**先排空 `PENDING`（3c）并续闸（3a）**，再按退出码分支并启动队列中下一个 `change run`：

- exit 0 → archived；exit 1 → skipped/failed（auto-decide 已落账），继续队列下一个。
- exit 5（needs-decision，仅交互档）：把 stdout 的 `trigger / round / blocking_trend / suggested` 转成 AskUserQuestion（选项映射 continue-retry / skip / force-archive / abort），然后 `npc change run --seq $SEQ --decision <答案>` 续跑。等人裁定期间内环队列暂停，但 implement 侧流水线继续跑。
- 需要失败细节时不读日志：spawn 只读 triage agent，喂 stdout 里的 `pointer.*` 路径，收一行诊断 JSON。

**3e. 层收尾 telemetry**——流水线下已无"波结束"这一时刻，原 `wave.done` 改名 `layer.done`，在 `v4-waves.json` 中**某一层的全部 change 都进入 INNER 或终态**（即该层实施与整合全部落定）时发一次：

```bash
npc telemetry emit --kind layer.done --extra "{\"layer\":$i,\"parallel\":$N,\"wall_s\":$T,\"conflicts\":$c}"
npc task update --id "npc-v4-$RUN_TS" --phase "layer-$i" --progress-current "<累计完成>" || true
```

`--kind` 是自由字符串，改名无需改 npc 代码；`parallel` 记该层实际达到的并发峰值，用于事后核对流水化是否兑现。`--webhook` 时追加 `npc notify --event layer-done --kv layer=$i`。

**3f. 检查点与 re-plan**——每次 3c 整合成功后顺手做一次（不再是"波间"）：

```bash
BRIEF=$(npc status --brief)   # notes = 人的转向指令；消费后 npc state note --consume
```

- `notes` 非空 → 按指令调整剩余计划，消费后打水位。
- re-plan 触发（满足其一）：出现 cherry-pick 冲突、某 change 被 skip 且有下游依赖、人经 note 要求重排 → 先停止 3a 续闸（不动已在跑的 ACTIVE），对剩余未完成集合重跑 Step 2 的 dag-analyst + `npc plan waves`（交互档给人确认），刷新 `$DAG` 后恢复续闸，run.events.jsonl 记 `{"type":"v4.replan","reason":...}`。

## Step 3S — 串行回退（`--serial-waves`）

用户明确要求、或宿主既无 sub-agent 并发也无后台执行时，退回逐波屏障：对 FINAL_WAVES 逐波执行 3b（波内全部 CID 同一消息并发 spawn）→ 逐个 3c 整合（整合后**不**续闸）→ 波内全部整合完毕后，对本波已整合的 change 按 SEQ 串行前台跑 3d 的 `npc change run` → 波收尾发 `wave.done`（字段同 3e，`wave` 替 `layer`）→ 波间检查点与 re-plan 同 3f。整波走完再进下一波。

代价即本节要解决的问题：整波等最慢的一个 change，下一波空转。仅在正确性优先于墙钟时使用。

## Step 4 — 收尾

```bash
npc state finalize && npc summary render && npc index append
npc cost --since "$RUN_T0"
```

收尾汇报对照 run-summary.md 的 Goal Coverage 段提示用户核对缺口（逐 change 全过 ≠ 组合达标），缺口给出新 change 建议清单。

## 约束

- 不逐轮读 review JSON、不手写 cherry-pick/sed、不批量读 changes 原文、不读日志/summary/review 原文。
- 不为了填满槽位而 spawn `npc plan ready` 判为 `blocked` 的 change——尤其带 `dep-pending` 的：其 worktree 基线不含依赖代码，implement 必然写在错误前提上。槽位空着是正确状态，`limit` 与 `file-conflict` 同理。
- 调度集合只信 `npc plan ready` 的返回，不凭 `v4-waves.json` 的波号自行判断"这波该开了"。
- 后台 `change run` 在跑时不调 `npc integrate`（RESULT 入 `PENDING` 等内环结束）；收到 `step=inner-loop-active` 只入队，不 `--force`。
- skill 行为与 npc 不符 → 修 npc（`src/npc`）、发布后从 tag 重装（`uv tool install --reinstall --from git+https://github.com/winewei/agent-spine@v<版本> npc`），不在 skill 内补脚本；不要用 `--from .` 本地目录安装。
- commit 禁 AI 署名 trailer；禁 `--no-verify`。
