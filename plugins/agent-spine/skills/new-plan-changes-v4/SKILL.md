---
name: new-plan-changes-v4
description: 波次并行推进所有活跃的 openspec changes（v3 的上下文预算重构版）：DAG 抽取与架构师裁定走 sub-agent，implement 并行 worktree，整合与单 change 内环各一条 npc 命令（npc integrate / npc change run）。主 session 每 change 只花 ~400 tokens，只在真正的决策分叉点出场。当用户说"并行推进 changes""new-plan-changes-v4""v4 跑 openspec changes"时触发。
category: OpenSpec
tags: [openspec, plan, implement, parallel, worktree, v4, context-budget]
---

# New Plan & Implement All Changes (v4 — 上下文预算版)

**你是调度器，不是搬运工。** 本 skill 的核心不变量（docs/optimization-proposals/2026-07-05-orchestration-context-budget.md）：

> 主 session 每推进一个 change 消耗 O(1) token（~400）。盘面状态以磁盘为唯一真相，你的 context 只是缓存；异常向上冒泡，状态留在下面。

与 v3 的差异：review-fix 循环与整合编排已下沉进 npc 1.5（`npc change run` / `npc integrate`），你不再逐轮搬 JSON、不再执行任何多行 bash 编排。**skill 内任何超过三行的 bash 块都是 bug**——发现即在 agent-spine 仓库修 npc 后重装，绝不在 skill 里写脚本。

**前置**：`npc --version` ≥ 1.5.0；`npc doctor` 过（缺 codex→跳 review、缺 openspec→只 commit 不 archive，记降级项）；`npc verify routing` 无 violation（有则真停，提示改 config）；git 工作树 clean；`.claude/settings.json` 或 `~/.claude/settings.json` 中 `worktree.baseRef=head`（否则报错退出，绝不静默退化）。

## 参数

| flag | 默认 | 含义 |
|---|---|---|
| `--auto` | off | 决策点全走 auto-decide，fire-and-forget；唯一真停是 exit 3（编排基底坏掉）与环境前置失败 |
| `--fresh` | off | 忽略 in-progress 旧 run |
| `--max-parallel N` | 不限 | 单波并发 implementer 上限 |
| `--no-architect` | off | 跳过语义裁定，直接用机械候选波次 |
| `--webhook URL` / `--webhook-format` | env / raw | 进度外呼（`npc notify`，永不阻塞） |

## Step 1 — 初始化 / 重定向

```bash
INIT=$(npc init ${AUTO:+--auto} ${FRESH:+--fresh})   # needs_resume / state_drift 看这里
```

- `state_drift.total_drifted > 0` → `npc state repair --auto`。
- **重定向纪律**：`needs_resume=true`、或你经历了任何 context compaction、或接手他人 session——一律先跑 `npc status --brief`，以其 `pending_decisions / notes / next_action` 重建盘面。**绝不信任你记忆里的进度。**
- `init --auto` 弄脏 `.claude/settings.json` 时：tracked → `npc git commit --message "chore: npc auto-auth settings"`；untracked → 写入 `.git/info/exclude`。

## Step 2 — 计划（唯一的重分析段，全部在 sub-agent 里）

**你不读任何 proposal/design/tasks/spec 原文。**

1. `openspec list --json` 只取 in-progress change 名 → NODES。
2. **spawn `dag-analyst`**（Explore，只读）：读 N×4 份文档 → 抽 nodes/edges/files（目录级条目用 Grep 展开）→ 跑 `npc plan waves` → 写 `<run_dir>/v3-dag-extract.json` → 回一行 RESULT。校验 nodes 完整、candidate.waves 展平=nodes；失败重发一次，再失败 `--auto` 才降级自抽（记 `dag_extract_fallback`），交互档真停。
3. **spawn 双架构师**（并行，只读；`--no-architect` 跳过）：A=senior-system-architect 查语义耦合（共享状态/时序/不变量），B=senior-code-developer 查落地冲突（真实文件/import/构建）。合并规则安全优先：任一判 serialize 即拆；提级须双方 independent+给理由。得 FINAL_WAVES。
4. 落地（把人设的原始目标写进 state，收尾验收用）：

```bash
npc state init-run --plan-order "$(jq -nc --argjson w "$FINAL_WAVES" '$w|add')" --goal "<用户的原始目标一句话>"
echo "$FINAL_WAVES" > "$RUN_DIR/v4-waves.json"
```

打印 Wave Plan Summary（波次、拆分理由、降级/提级）留痕到 run.events.jsonl。交互档 ExitPlanMode 等批准；`--auto` 不进 plan 模式直接跑。

## Step 3 — 波次循环（每 change 三条命令）

对 FINAL_WAVES 逐波执行：

**3a. 并行 implement（worktree 隔离）**——对波内每个 CID 同一消息并发 spawn：

```bash
npc state add-change "$SEQ" "$CID" && npc phase rotate --seq "$SEQ" --to implement
npc agent prompt render --phase implement --change-id "$CID"
GUIDE=$(npc agent spawn-prompt --phase implement --change-id "$CID" | jq -r .prompt)
```

`Agent(senior-code-developer, isolation="worktree", prompt=GUIDE+worktree 契约)`。worktree 契约：写 manifest JSON 到 `<run_dir>/v3-manifests/<cid>.json`（files_written 对象数组），最后输出 `RESULT:` 行 + `MANIFEST:` 行。

**3b. 收单整合**——每个 implementer 返回后一条命令（manifest 核验 + cherry-pick + hash 翻译 + record + verify tests + fail 自动 revert 全在内）：

```bash
npc integrate --seq "$SEQ" --result "<RESULT 行>" --manifest "<MANIFEST 路径>"
```

失败看 `.step`：`verify-manifest`（plan-only）→ 重发该 implementer 一次（前缀 "IMPLEMENT NOW"），再失败标 failed；`cherry-pick`（DAG 漏边）→ 记入 **re-plan 信号**（见 3e），该 change 改串行：`npc implement run --seq $SEQ`；`verify-tests`（已自动 revert，main 保持绿）→ `npc auto-decide --seq $SEQ --trigger implementer-failed --apply`。

**3c. 内环**——对本波已整合的 change 按 SEQ 串行，一条命令跑完 review→fix 循环→archive：

```bash
npc change run --seq "$SEQ" --from review ${AUTO:+--auto}
```

- exit 0 → archived；exit 1 → skipped/failed（auto-decide 已裁定并落账），继续下一个。
- **exit 5（needs-decision，仅交互档）**：这是人出场的唯一时刻。把 stdout 的 `trigger / round / blocking_trend / suggested` 转成 AskUserQuestion（选项映射 continue-retry / skip / force-archive / abort），然后 `npc change run --seq $SEQ --decision <答案>` 续跑。
- 需要失败细节时**绝不 cat 日志**：spawn 一个只读 triage agent，喂 stdout 里的 `pointer.*` 路径，收一行诊断 JSON。

**3d. 波收尾**：

```bash
npc telemetry emit --kind wave.done --extra "{\"wave\":$i,\"parallel\":$N,\"wall_s\":$T,\"conflicts\":$c}"
npc task update --id "npc-v4-$RUN_TS" --phase "wave-$i" --progress-current "<累计完成>" || true
```

（可选 `npc notify --event wave-done ...` 外呼。）

**3e. 波间检查点（steering + re-plan）**：

```bash
BRIEF=$(npc status --brief)   # notes = 人的转向指令；消费后 npc state note --consume
```

- `notes` 非空 → 按指令调整剩余计划（修剪范围/追加约束/改优先级），消费后打水位。
- **re-plan 触发**（满足其一）：本波有 cherry-pick 冲突（DAG 漏边）、某 change 被 skip 且有下游依赖、人经 note 要求重排 → 对**剩余未完成集合**重跑 Step 2 的 dag-analyst + `npc plan waves`（交互档给人确认），run.events.jsonl 记 `{"type":"v4.replan","reason":...}`。

## Step 4 — 收尾

```bash
npc state finalize && npc summary render && npc index append
npc cost --since "$RUN_T0"
```

run-summary.md 的 **Goal Coverage 段是给人的验收对照表**（原始目标 × 各 change 终态）——收尾汇报时明确提示用户：逐 change 全过 ≠ 组合达标，请对照原始目标核对缺口；缺口应立为新 change（可直接给出建议清单）。

## Guardrails（速查）

- **O(1) 纪律**：每 change 的主 session 流量 = spawn（~150t）+ integrate 一行 + change run 一行。任何让你逐轮读 review JSON、手写 cherry-pick/sed、批量拉原文的冲动，都是在退化回 v3。
- **重定向纪律**：compaction / 续跑 / 接手之后，第一条命令永远是 `npc status --brief`。
- **triage 纪律**：主 session 永不读日志/summary/review 原文；失败细节走只读 triage agent + `pointer.*`。
- **决策点即人闸**：交互档只在 exit 5 时问人（AskUserQuestion 带 trigger/trend/suggested）；`--auto` 绝不问人，唯一真停是 exit 3 / 环境前置失败。
- **生成⊥验证 / MiMo 只许执行**：由 `npc verify routing` 与 `npc change run` 内部强制，你无须（也不得）绕过。
- **worktree.baseRef=head 硬前置**；工作树必须 clean；commit 严禁 AI 署名 trailer；禁 `--no-verify`。
- **运行轨迹外置**：state/events/notes/telemetry 全在 `~/task_log/<PROJ_KEY>/`，工程目录零侵入。
- **skill 零脚本**：机械逻辑全部在 npc 1.5（waves / integrate / change run / verify manifest|tests|tasks / notify / note / brief）。行为不符 → 修 npc（agent-spine 仓库 `src/npc`）并 `uv tool install --force --from . npc` 重装。
