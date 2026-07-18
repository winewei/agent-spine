---
name: new-plan-changes-v4
description: 波次并行推进所有活跃的 openspec changes：DAG 抽取与架构师裁定走 sub-agent，implement 并行 worktree，整合与单 change 内环各一条 npc 命令（npc integrate / npc change run）。当用户说"并行推进 changes""new-plan-changes-v4""v4 跑 openspec changes"时触发。
category: OpenSpec
tags: [openspec, plan, implement, parallel, worktree, v4]
---

# new-plan-changes-v4

## 前置（不满足即报错退出）

- `npc --version` ≥ 1.5.0
- `npc doctor` 通过；缺 codex → 跳 review，记降级项、不阻塞
- `openspec` 可用（`openspec list --json` 是计划入口）
- git 工作树 clean
- `worktree.baseRef=head`（`.claude/settings.json` 或 `~/.claude/settings.json`）；否则报错退出，不静默退化

## 参数

| flag | 默认 | 含义 |
|---|---|---|
| `--auto` | off | 决策点全走 auto-decide；唯一真停是 exit 3 与环境前置失败 |
| `--fresh` | off | 忽略 in-progress 旧 run |
| `--max-parallel N` | 不限 | 单波并发 implementer 上限 |
| `--no-architect` | off | 跳过语义裁定，直接用机械候选波次 |
| `--webhook URL` / `--webhook-format` | env / raw | 进度外呼（`npc notify`，永不阻塞） |

## Step 1 — 初始化 / 重定向

```bash
INIT=$(npc init ${AUTO:+--auto} ${FRESH:+--fresh})   # needs_resume / state_drift 看这里
```

- `state_drift.total_drifted > 0` → `npc state repair --auto`。
- `needs_resume=true`、经历过 context compaction、或接手他人 session → 先跑 `npc status --brief`，以其 `pending_decisions / notes / next_action` 重建盘面，不信任记忆里的进度。
- `init --auto` 弄脏 `.claude/settings.json` 时：tracked → `npc git commit --message "chore: npc auto-auth settings"`；untracked → 写入 `.git/info/exclude`。

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

## Step 3 — 波次循环

对 FINAL_WAVES 逐波执行：

**3a. 并行 implement（worktree 隔离）**——对波内每个 CID 同一消息并发 spawn：

```bash
npc state add-change "$SEQ" "$CID" && npc phase rotate --seq "$SEQ" --to implement
npc agent prompt render --phase implement --change-id "$CID"
GUIDE=$(npc agent spawn-prompt --phase implement --change-id "$CID" | jq -r .prompt)
```

`Agent(senior-code-developer, isolation="worktree", prompt=GUIDE+worktree 契约)`。worktree 契约：写 manifest JSON 到 `<run_dir>/v3-manifests/<cid>.json`（files_written 对象数组），最后输出 `RESULT:` 行 + `MANIFEST:` 行。

**3b. 收单整合**——每个 implementer 返回后一条命令（核验/cherry-pick/record/verify tests/revert 全在内）：

```bash
npc integrate --seq "$SEQ" --result "<RESULT 行>" --manifest "<MANIFEST 路径>"
```

失败看 `.step`：`verify-manifest`（plan-only）→ 重发该 implementer 一次（前缀 "IMPLEMENT NOW"），再失败标 failed；`cherry-pick` → 记入 re-plan 信号（见 3e），该 change 改串行 `npc implement run --seq $SEQ`；`verify-tests`（已自动 revert）→ `npc auto-decide --seq $SEQ --trigger implementer-failed --apply`。

**3c. 内环**——对本波已整合的 change 按 SEQ 串行：

```bash
npc change run --seq "$SEQ" --from review ${AUTO:+--auto}
```

- exit 0 → archived；exit 1 → skipped/failed（auto-decide 已落账），继续下一个。
- exit 5（needs-decision，仅交互档）：把 stdout 的 `trigger / round / blocking_trend / suggested` 转成 AskUserQuestion（选项映射 continue-retry / skip / force-archive / abort），然后 `npc change run --seq $SEQ --decision <答案>` 续跑。
- 需要失败细节时不读日志：spawn 只读 triage agent，喂 stdout 里的 `pointer.*` 路径，收一行诊断 JSON。

**3d. 波收尾**：

```bash
npc telemetry emit --kind wave.done --extra "{\"wave\":$i,\"parallel\":$N,\"wall_s\":$T,\"conflicts\":$c}"
npc task update --id "npc-v4-$RUN_TS" --phase "wave-$i" --progress-current "<累计完成>" || true
```

`--webhook` 时追加 `npc notify --event wave-done --kv wave=$i`。

**3e. 波间检查点**：

```bash
BRIEF=$(npc status --brief)   # notes = 人的转向指令；消费后 npc state note --consume
```

- `notes` 非空 → 按指令调整剩余计划，消费后打水位。
- re-plan 触发（满足其一）：本波有 cherry-pick 冲突、某 change 被 skip 且有下游依赖、人经 note 要求重排 → 对剩余未完成集合重跑 Step 2 的 dag-analyst + `npc plan waves`（交互档给人确认），run.events.jsonl 记 `{"type":"v4.replan","reason":...}`。

## Step 4 — 收尾

```bash
npc state finalize && npc summary render && npc index append
npc cost --since "$RUN_T0"
```

收尾汇报对照 run-summary.md 的 Goal Coverage 段提示用户核对缺口（逐 change 全过 ≠ 组合达标），缺口给出新 change 建议清单。

## 约束

- 不逐轮读 review JSON、不手写 cherry-pick/sed、不批量读 changes 原文、不读日志/summary/review 原文。
- skill 行为与 npc 不符 → 修 npc（`src/npc`）并 `uv tool install --force --from . npc` 重装，不在 skill 内补脚本。
- commit 禁 AI 署名 trailer；禁 `--no-verify`。
