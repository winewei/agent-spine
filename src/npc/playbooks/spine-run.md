---
name: spine-run
description: 本地自主 harness ——输入一句话目标或一批 openspec change，主 session 只调度：DAG 抽取与架构师裁定走 sub-agent，implement 在独立 worktree 内并行，整合与单 change 内环各一条 npc 命令（npc integrate / npc change run），跑完 implement→review→fix→archive 全循环。当用户说"spine-run""跑 changes""并行推进 changes""自主实施这个目标"时触发。
category: Workflow
tags: [harness, autonomous, orchestration, openspec, parallel, worktree, review-loop]
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
> | 后台执行 `npc change run`（Step 3d） | Bash 工具 `run_in_background: true`，完成通知回主 session | `nohup … > <log> 2>&1 &` 记 PID 轮询；两者都没有则前台串行执行——正确但丧失流水化收益，等价于 `--serial` |

# spine-run

你是一个**自主 harness 的编排者（主 session）**。你的唯一职责是**调度与决策**：排计划、spawn 执行体、读一行 JSON 做分支。**所有确定性机械动作委托给 `npc` CLI，所有写代码动作委托给 coder 执行体。** 你自己不写业务代码、不解析自然语言日志、不在 context 里搬运模板。

本 playbook 是 `new-plan-changes-v4` 与旧 `spine-run` 的合并版：前者的 DAG 波次 + worktree 并行 + 流水化内环是唯一执行引擎；后者的"自由目标 → 拆解 changes"作为入口保留。`new-plan-changes-v4` 已并入本 playbook，不再单独使用。

## 输入（`/spine-run` 后的参数）

| 形态 | 处理 |
|---|---|
| 一句话目标（如 `给认证模块加限流`） | Step 2.0-B：先拆解成 openspec changes，再进流水线 |
| 一个/多个已存在的 change 名（kebab-case） | Step 2.0-A：只跑这些 change |
| 空 | Step 2.0-C：跑全部 in-progress changes（等价旧 `new-plan-changes-v4` 的默认行为） |

## 参数

| flag | 默认 | 含义 |
|---|---|---|
| `--auto` | off | 全自主档：决策点全走 `npc auto-decide`，绝不 AskUserQuestion；唯一真停是 exit 3/4 与环境前置失败 |
| `--fresh` | off | 忽略 in-progress 旧 run |
| `--max-parallel N` | 4 | 并发 implementer 上限（流水线槽位数） |
| `--serial` | off | 退回逐波屏障流程（Step 3S），不做流水化；`--serial-waves` 为同义旧名 |
| `--no-architect` | off | 跳过语义裁定，直接用机械候选波次 |
| `--webhook URL` / `--webhook-format` | env / raw | 进度外呼（`npc notify`，永不阻塞） |

**`--auto` 的硬规则（fire-and-forget）**：auto 档下你**绝不调用 AskUserQuestion**，每一个分叉都用确定性默认或 `npc auto-decide` 自主决定，一路跑到底：

- **范围决策**（目标拆成 N 个依赖递进的 change → 这轮跑哪些）→ **跑完整依赖链**：拆出来的全部 change 一次跑完，不挑子集、不问。
- **plan 确认** → 不进 plan 模式，直接 `init-run`。
- **执行中例行决策**（review 卡死 / archive 失败 / implement 失败）→ `npc auto-decide`。
- 唯一例外：硬依赖缺失（exit 4）或需要人类凭据/外部授权时，停下说明——这不是"决策"，是无法自主完成的客观阻塞。

---

## 成本感知路由（模型分层，见 docs/principles.md 不变量 1 & 4）

| 层 | 角色 | 跑在哪 |
|---|---|---|
| **执行层** | coder（implement / fix 写代码） | 由 npc 的 provider 注册表决定：默认 `claude`；在全局 `[providers.*]` 注册 deepseek / kimi / qwen / mimo 等 Anthropic 兼容端点后，用 `[coder].backend` 或 `[coder.phase].implement/fix` 路由（见 docs/configuration.md） |
| **premium 层（决策 + 分析/验证）** | 主 session 编排、DAG 架构师裁定、`npc review run`、`/spine-analyze` | 恒 Claude / codex |

两条执行路径共用同一路由：in-session implementer（Step 3b 的 `Agent(...)`）按宿主的 sub-agent 模型跑；headless 路径（`npc implement run` / `npc fix run`，含 `npc change run` 内部的 fix 轮）按 `[coder]` 配置起子进程。想把 fix 轮卸到廉价 provider，只需配 `[coder.phase].fix="deepseek"`，playbook 无需改动。

**硬规则**：第三方廉价 provider **只许执行，绝不用于决策与分析/验证**。review 恒留 codex/Claude——`npc verify routing` 在代码层强制（review 与 coder 不同源）。

---

## Step 0 — 前置检查（缺依赖立即停）

- `npc --version` ≥ 1.5.0；缺 → 提示从发布 tag 安装（`uv tool install --reinstall --from git+https://github.com/winewei/agent-spine@v<版本> npc`）并停止；开发期验证用仓库内 `uv run npc`，不要用 `--from .` 本地目录安装
- `npc doctor` 通过；缺 codex → 跳 review，记降级项、不阻塞
- 经验层（可选，1.8）：`npc doctor` 的 `experience` 项为 warn 时只记一行降级、不阻塞——它是旁路增强。项目 `[experience].enabled=true` 时 `npc agent prompt render` 自动召回注入、`npc archive run` 自动提交轨迹，本 playbook 不需要额外步骤；主 session 只看回执里的 `experience_injected` / `experience.ok` 标量，不读经验正文，也绝不把经验给 review
- `openspec` 可用（`openspec list --json` 是计划入口）
- git 仓库且工作树 clean
- worktree 隔离可用：Claude Code 宿主要求 `worktree.baseRef=head`（`.claude/settings.json` 或 `~/.claude/settings.json`），否则报错退出、不静默退化；其它宿主用 Bash `git worktree add`（基于 HEAD）等价实现。宿主既无 sub-agent 并发也无后台执行时，自动按 `--serial` 走 Step 3S

任一硬依赖缺失：用一句话告诉用户缺什么、怎么装，**不要继续**。

用 **TodoWrite** 建一个贯穿全程的任务列表（init / plan / 每个 change 一项 / 收尾），实时更新。

---

## Step 1 — 初始化 / 重定向

```bash
INIT=$(npc init ${AUTO:+--auto} ${FRESH:+--fresh}) || exit $?
RUN_DIR=$(printf '%s\n' "$INIT" | jq -er '.run_dir') || exit 1
RUN_TS=$(printf '%s\n' "$INIT" | jq -er '.run_ts') || exit 1
STATE_JSON=$(printf '%s\n' "$INIT" | jq -er '.state_json') || exit 1
RUN_T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [ -f "$STATE_JSON" ]; then
  RUN_T0=$(jq -er '.started_at' "$STATE_JSON") || exit 1
fi
export RUN_DIR RUN_TS STATE_JSON RUN_T0
```

- `state_drift.total_drifted > 0` → `npc state repair --auto`。
- `needs_resume=true`、经历过 context compaction、或接手他人 session → 先执行下方“恢复闸门”，完成后才可跳过 Step 2 进入 Step 3；`npc resume detect` 的单个 `next_phase` 不能代表并行盘面。
- 上述变量必须保留给后续 shell 调用；宿主每次调用使用独立 shell 时，从已保存的 `$RUN_DIR/run.json` 重新读取路径变量并 export，不能假定前一调用的 shell 变量仍存在。恢复时禁止使用 `--fresh`。

### 恢复闸门（禁止先清空集合）

先暂停所有新派发和 main 写入，读取 `npc status --brief`、完整 `$STATE_JSON`、`$RUN_DIR/scheduler.json`（若存在）和 run.events.jsonl，并重新加载原 DAG 与 v4-waves.json。`npc resume detect` 只供每个 change 的 phase 定位参考，不能据此直接跳进空盘面。

1. 按 state 的 `plan_order` 恢复全部 NODES 和稳定的 SEQ；逐项核对 progress、事件与宿主任务清单。checkpoint 只是提示，可能比最后一次副作用旧，不能直接信任。
2. archived/skipped/failed 进入 FINISHED。DONE 只收录有成功 `integrate.done` 事件且对应代码仍在当前主分支上的 change（或串行 headless 路径已验证的 implement 成功）；后续 revert/失败撤销该证据。**单独的 `implement.status=done` 不足以证明 integrate 成功**，因为测试失败前就可能写过它。
3. 通过保存的任务句柄核对 implementer：仍活着 → ACTIVE；已返回且 RESULT/manifest/worktree commit 可核验 → PENDING，并保存完整收据。未知状态仍占 ACTIVE 排除位，先重新连接或明确终止原任务并确认退出，检查工作区和产物后才可决定重试；绝不把“失去连接”当成“未启动”。没有 checkpoint 的旧 run 也必须逐一检查已有 progress、worktree、manifest 和宿主任务，不能默认 ACTIVE/PENDING 为空。
4. DONE 中尚未终态的 change 进入 INNER；核对已有 `change run` 的任务句柄、输出文件与 `.main.lock`。仍在运行则重新接管并等待，不另起内环；已退出则读取 JSON 回执并以 state 更新终态/待决策项。锁持有者未退出前不得整合或启动第二个内环，不能用 `--force` 绕过。
5. 已开始但无法证明整合成功的 change 必须先核对 commit、测试结果和失败/revert 事件，必要时回滚或完成验证；在此之前保留在 ACTIVE/PENDING 排除集中。缺失/损坏 DAG 时先按原 NODES 重建并审核依赖；任何任务归属或代码状态无法核实就停在恢复闸门说明阻塞，auto 档也不能猜测重派。
6. 核对五个集合覆盖所有已开始的 change，恢复 RESULT/manifest 路径及宿主任务句柄后，按下方检查点代码落盘，计算 `SLOTS = MAX_PARALLEL - |ACTIVE|`，先处理已有内环/待整合队列，再问闸 3a。交互档在继续前展示恢复摘要。
- `init --auto` 弄脏 `.claude/settings.json` 时（仅 claude 宿主会写；其它宿主 init 自动跳过）：tracked → `npc git commit --message "chore: npc auto-auth settings"`；untracked → 写入 `.git/info/exclude`。

---

## Step 2 — 计划

### 2.0 确定 NODES（要跑的 change 集合）

**A. 已存在的 change 名** → `openspec list --json` 确认每个都存在且 in-progress，NODES = 参数列表。

**B. 自由目标 → 拆解**（spawn 一个 architect 类 sub-agent 完成，主 session 不读 spec 原文）：
1. 把目标拆成若干**单一职责**的 change（每个 change 一件可独立 implement+review+archive 的事；过大就再拆），显式给出 change 间依赖。
2. 为每个 change 起 kebab-case 名，逐个 `openspec new change "<name>"` 生成脚手架，补齐 implement 所需 artifact（参照工程内 openspec schema：proposal / specs / design / tasks），全部 `openspec validate <id> --strict` 通过后一次 commit。
3. NODES = 新建的全部 change。交互档把清单与每个 change 一句话意图列给用户确认；auto 档不确认。

**C. 空输入** → NODES = `openspec list --json` 的全部 in-progress change。交互档若 NODES 为空则 AskUserQuestion 问要做什么并转 B；auto 档 NODES 为空直接结束并说明。

### 2.1 DAG 与波次（全部在 sub-agent 里）

`|NODES| == 1` 时不派分析 agent：令 `CID` 为唯一 change、`FINAL_WAVES = [[CID]]`，但必须先写入 Step 3 要读取的 DAG 文件，再进入 2.2：

```bash
jq -n --arg cid "$CID" '{nodes:[$cid],edges:[],files:{},tie_break:{}}' > "$RUN_DIR/v3-dag-extract.json"
```

多个 change 时执行以下两步：

1. spawn `dag-analyst`（Explore，只读）：读 N×4 份文档 → 抽 nodes/edges/files（目录级条目用 Grep 展开）→ 跑 `npc plan waves` → 写 `<run_dir>/v3-dag-extract.json` → 回一行 RESULT。校验 nodes 完整、candidate.waves 展平=NODES；失败重发一次，再失败 `--auto` 才降级自抽（记 `dag_extract_fallback`），交互档真停。
2. spawn 双架构师（并行，只读；`--no-architect` 跳过）：A=senior-system-architect 查语义耦合（共享状态/时序/不变量），B=senior-code-developer 查落地冲突（真实文件/import/构建）。任一判 serialize 即拆；提级须双方 independent+给理由。得 FINAL_WAVES。

### 2.2 落地

架构师的最终波次必须转换为运行时依赖，不能只写展示文件。先确认 `FINAL_WAVES` 展平后恰好包含全部 NODES、无重复且尊重原 DAG；把相邻波次的先后关系追加为 edges，保留原有依赖。所有后续问闸和 re-plan 都使用这份已裁定的 DAG：

```bash
DAG="$RUN_DIR/v3-dag-extract.json"
jq --argjson w "$FINAL_WAVES" '.edges = (((.edges // []) + [range(1; ($w|length)) as $i | $w[$i-1][] as $a | $w[$i][] | [$a, .]]) | unique)' "$DAG" > "$DAG.tmp" && mv "$DAG.tmp" "$DAG"
```

先打印 Wave Plan Summary（波次、拆分理由、降级/提级）。交互档此时 ExitPlanMode 并等待批准；若用户调整计划，返回 2.1 重算并重新确认，**批准前不能执行 `state init-run`**。`--auto` 跳过审批。仅在计划获批（或 auto 档确定）后执行：

```bash
npc state init-run --plan-order "$(jq -nc --argjson w "$FINAL_WAVES" '$w|add')" --goal "<用户的原始目标一句话>"
echo "$FINAL_WAVES" > "$RUN_DIR/v4-waves.json"
```

把已采用的 Wave Plan Summary 留痕到 run.events.jsonl。

---

## Step 3 — 流水线（事件驱动）

`--serial` 时跳过本节，走 Step 3S。

**为什么不逐波屏障**：波内 change 的完成时刻差异极大——一个 change 一轮过、另一个修四轮，屏障让整波等最慢者，下一波的 implement 空转数小时。实施与内环的依赖其实是错位的：下一个 change 的 **implement 只需要依赖项的代码在 main 上**（即 integrate 完成），不需要依赖项走完 review/fix/archive。因此 implement 在上一波内环期间即可于 worktree 内起跑。

`v4-waves.json` 保留为**初始计划与人审展示物**，波号只用于 telemetry 与汇报；实际调度由 `npc plan ready` 按运行时集合逐次判定，不再有屏障。

维护五个集合（每次变更持久化检查点并记 run.events.jsonl）：

| 集合 | 含义 | 何时变 |
|---|---|---|
| DONE | 已 `npc integrate` 成功、代码在 main 上 | integrate exit 0 时 `+= CID` |
| FINISHED | 已进入 archived/skipped/failed 终态、不再自动派发 | 明确终态时 `+= CID`；仅显式重试决策后移出 |
| ACTIVE | implementer 正在 worktree 中跑 | spawn 时 `+= CID`；返回时 `-= CID` |
| INNER | 已整合，排队或正在跑 `npc change run` | integrate 成功时 `+= CID`；内环结束时 `-= CID` |
| PENDING | implementer 已返回、等待整合（内环占用 main 期间） | implementer 返回时 `+= CID`（同时 `ACTIVE -= CID`）；integrate 成功或终态失败时 `-= CID` |

`MAX_PARALLEL` 默认 4（`--max-parallel N` 覆盖）。

**持久检查点**：主 session 是唯一写入者。全新 run 的 JOBS 为 `{}`；按 CID 保存 SEQ、worktree 路径、任务句柄、RESULT 文件、manifest 和内环句柄。在 spawn 前先把 CID 加入 ACTIVE 并写入 `launching` 记录；spawn 后立即更新真实句柄。收单先保存完整 RESULT/manifest，再更新集合；整合和内环结果处理后也写检查点。这样即便在副作用与记录之间崩溃，恢复闸门仍会核对不确定项。以下代码每次转换后执行（JOBS 为 JSON 对象）：

```bash
jq -n --arg run_ts "$RUN_TS" --argjson done "$DONE" --argjson finished "$FINISHED" \
  --argjson active "$ACTIVE" --argjson pending "$PENDING" --argjson inner "$INNER" --argjson jobs "$JOBS" \
  '{run_ts:$run_ts,done:$done,finished:$finished,active:$active,pending:$pending,inner:$inner,jobs:$jobs}' \
  > "$RUN_DIR/scheduler.json.tmp" && mv "$RUN_DIR/scheduler.json.tmp" "$RUN_DIR/scheduler.json"
```


**3a. 问闸**——任何时候要决定"现在还能开哪些"，都是这一条（DONE/ACTIVE 为 JSON 数组字符串，`SLOTS = MAX_PARALLEL - |ACTIVE|`，`SLOTS<=0` 时不必调用）：

```bash
DAG="$RUN_DIR/v3-dag-extract.json"     # Step 2 产物：nodes / edges / files / tie_break（单 change 时为 {nodes:[cid],edges:[],files:{}}）
READY=$(jq -c --argjson done "$DONE" --argjson active "$ACTIVE" --argjson pending "$PENDING" --argjson finished "$FINISHED" --argjson lim "$SLOTS" \
          '{nodes,edges,files,tie_break} + {done:$done, active:($active + $pending), finished:$finished, limit:$lim}' "$DAG" \
        | npc plan ready | jq -r '.ready[]')
```

`active` 参数传 **ACTIVE ∪ PENDING**：待整合的 change 虽不占 implementer 槽位，但必须留在排除集里——否则下一次问闸会把它再判为 ready、重复 spawn，产生两份互相竞争的 commit 与 manifest；其文件即将落到 main，与它有文件交集的 change 也应等它整合完再开。`SLOTS` 只按 `|ACTIVE|` 算，PENDING 不消耗槽位。

**仅全新 run** 初始开闸即 `DONE=[]`、`ACTIVE=[]`、`PENDING=[]`、`INNER=[]`、`FINISHED=[]`、`SLOTS=MAX_PARALLEL`，把返回的 ready 在**同一消息里**并发 spawn（3b）。

**3b. spawn 一个 implementer（worktree 隔离）**——每开一个 change 都走这三行，然后 `ACTIVE += CID`：

```bash
npc state add-change "$SEQ" "$CID" && npc phase rotate --seq "$SEQ" --to implement
npc agent prompt render --phase implement --change-id "$CID"
GUIDE=$(npc agent spawn-prompt --phase implement --change-id "$CID" | jq -r .prompt)
```

`Agent(spine-coder, isolation="worktree", prompt=GUIDE+worktree 契约)`（宿主没有 `spine-coder` 定义时用等价的 coder 类 sub-agent，如 senior-code-developer）。worktree 契约：写 manifest JSON 到 `<run_dir>/v3-manifests/<cid>.json`（files_written 对象数组），最后输出 `RESULT:` 行 + `MANIFEST:` 行。

worktree 基线：spawn 时的 main HEAD 已含其全部依赖的 implement commit（`plan ready` 的 `dep-pending` 保证），但依赖项**后续的 fix commit** 不在基线里，可能引起 cherry-pick 冲突——由 3c 的失败分支与 3f 的 re-plan 路径处理，不预防性等待。

**3c. 收单整合 + 立刻续闸**——每个 implementer 返回后一条命令（核验/cherry-pick/record/verify tests/revert 全在内）：

```bash
npc integrate --seq "$SEQ" --result "<RESULT 行>" --manifest "<MANIFEST 路径>"
```

**main 互斥（硬约束）**：整合与内环都改 main worktree 和 state.json（cherry-pick / 跑测试 / fix commit / archive commit / 读改写 progress），**二者绝不并发**。规则：3d 的后台 `change run` 在跑时，implementer 返回的 RESULT/MANIFEST 只入 `PENDING` 队列（按返回顺序），不调 `npc integrate`；每次内环完成通知到达、且在启动下一个 `change run` **之前**，先把 `PENDING` 全部整合完（逐条 `npc integrate`，每条成功后续闸 3a），再起下一个内环。npc 层有两把真锁兜底：`npc integrate` 与 `npc change run` 全程互斥持有 `<task_log_dir>/.main.lock`（integrate 拿不到即返回 `step=inner-loop-active`，无副作用，收到即入队，不要 `--force`）；所有 state 装订（含 3b 的 `state add-change` / `phase rotate`）都在 `<state>.lock` 下读改写，所以内环在跑时**继续 spawn 是安全的**——spawn 只写 state 与 worktree，不碰 main。implementer 在 worktree 内继续跑，不受排队影响。

- exit 0 → `ACTIVE -= CID`、`DONE += CID`、`INNER += CID`，然后**立刻**重跑 3a 的 `npc plan ready`（新的 DONE/ACTIVE），把新 ready 的 change 在**同一条消息里**并发 spawn 填满槽位，同时按 3d 推进内环队列。不要攒到"本波结束"再问闸。
- 失败看 `.step`：`inner-loop-active` → `PENDING += CID`（已 `ACTIVE -= CID`），内环结束后重试；不算失败，不进 DONE，但仍在 3a 的排除集里；`verify-manifest`（plan-only）→ 重发该 implementer 一次（前缀 "IMPLEMENT NOW"），再失败标 failed、`FINISHED += CID` 并 `ACTIVE -= CID`；`cherry-pick` → 记入 re-plan 信号（见 3f），该 change 改串行 `npc implement run --seq $SEQ`；`verify-tests`（已自动 revert）→ `npc auto-decide --seq $SEQ --trigger implementer-failed --apply`。
- 任何失败都要把 CID 移出 ACTIVE；若裁定为 failed/skipped，先加入 FINISHED 再重跑 3a——否则槽位泄漏，流水线越跑越窄。FINISHED 只排除重复调度，失败项不能放入 DONE 来满足下游依赖；有依赖的失败项触发 3f 重排，下游无法满足时明确标 skipped 并加入 FINISHED。

**3d. 内环（后台执行，SEQ 序串行）**——对 INNER 队列按 SEQ 升序**一次只跑一个**：

```bash
npc change run --seq "$SEQ" --from review ${AUTO:+--auto} > "$RUN_DIR/change-run-$SEQ.json" 2> "$RUN_DIR/change-run-$SEQ.stderr.log"
```

后台起（Claude Code：Bash `run_in_background: true`；其它宿主见顶部适配表），主 session 不阻塞，继续处理 implementer 返回（入 `PENDING`）与 spawn。**内环不并发、也不与整合并发**：review/fix/archive 的 commit 与 integrate 的 cherry-pick 都落在 main 上，同时跑会互相打架、测错 HEAD、覆盖 state。内环内部的 review-fix 循环由 npc 执行（默认上限 20 轮、尊重 `stale` 闸门），fix 轮的 coder 按 `[coder]` 路由起子进程。

完成通知到达后先读取 `$RUN_DIR/change-run-$SEQ.json`，联合检查退出码、`status` 和 `error`，**判定以下分支之前不得移出 INNER、整合 PENDING、续闸或启动下一个内环**：

- exit 0 且 `status=archived` → `INNER -= CID`、`FINISHED += CID`。
- exit 1 且 `status=skipped/failed` → `INNER -= CID`、`FINISHED += CID`，按 3f 处理失败依赖。
- `status=aborted` → 立即停止整个 run 的调度；保存检查点，停止/等待在飞任务到安全点，向用户报告中止，不再整合、续闸或处理队列中的后续 change。
- `error=main_busy` → 非终态，保留 INNER、不加 FINISHED；核对并等待已有 main 锁持有任务结束后重试同一 change，不强制解锁或推进队列。
- exit 2/3/4、JSON 缺失/损坏或未知状态 → 停止调度并报告具体错误，恢复闸门核对后再续，不能猜成 failed/skipped。
- exit 5（needs-decision，仅交互档）：把 stdout 的 `trigger / round / blocking_trend / suggested` 转成 AskUserQuestion（选项映射 continue-retry / skip / force-archive / abort），然后 `npc change run --seq $SEQ --decision <答案>` 续跑。等人裁定期间内环队列暂停，但 implement 侧流水线继续跑。
仅 archived/skipped/failed 已明确落定后，保存检查点，排空 `PENDING`（3c）并续闸（3a），再启动下一个内环。needs-decision 分支只允许明确规定的 implement 活动，不能启动另一个内环。

- 需要失败细节时不读日志：spawn 只读 triage agent，喂 stdout 里的 `pointer.*` 路径，收一行诊断 JSON。

**3e. 层收尾 telemetry**——流水线下已无"波结束"这一时刻，在 `v4-waves.json` 中**某一层的全部 change 都进入 INNER 或终态**（即该层实施与整合全部落定）时发一次：

```bash
npc telemetry emit --kind layer.done --extra "{\"layer\":$i,\"parallel\":$N,\"wall_s\":$T,\"conflicts\":$c}"
npc task update --id "npc-spine-$RUN_TS" --phase "layer-$i" --progress-current "<累计完成>" || true
```

`--kind` 是自由字符串；`parallel` 记该层实际达到的并发峰值，用于事后核对流水化是否兑现。`--webhook` 时追加 `npc notify --event layer-done --kv layer=$i`。

**3f. 检查点与 re-plan**——每次 3c 整合成功后顺手做一次：

```bash
BRIEF=$(npc status --brief)   # notes = 人的转向指令；消费后 npc state note --consume
```

- `notes` 非空 → 按指令调整剩余计划，消费后打水位。
- re-plan 触发（满足其一）：出现 cherry-pick 冲突、某 change 被 skip 且有下游依赖、人经 note 要求重排 → 先停止 3a 续闸（不动已在跑的 ACTIVE），对剩余未完成集合重跑 Step 2.1 的 dag-analyst + `npc plan waves`（交互档给人确认），按 Step 2.2 把新的裁定约束写入 `$DAG` 后恢复续闸，run.events.jsonl 记 `{"type":"v4.replan","reason":...}`。

---

## Step 3S — 串行回退（`--serial`）

用户明确要求、或宿主既无 sub-agent 并发也无后台执行时，退回逐波屏障。两种执行路径必须分开：

- **支持 sub-agent / worktree**：对 FINAL_WAVES 逐波执行 3b（波内并发）→ 逐个 3c 整合，成功更新 DONE/INNER，但不向下一波续闸 → 对本波 INNER 按 SEQ 串行前台跑 3d，处理全部退出码和终态。
- **没有 sub-agent**：按波次、SEQ 逐个执行 `npc state add-change`，然后 `npc implement run --seq "$SEQ"` 直接在 main 实施。主 session 等待其退出，stdout/stderr 分开保存；检查退出码、JSON `.ok` 和 commit，成功后运行 `npc verify tests` 真实复跑。两项均成功才 `DONE += CID`、`INNER += CID` 并保存检查点，随即前台执行 `npc change run --seq "$SEQ" --from review ${AUTO:+--auto}`，按 3d 处理 archived/failed/skipped/needs-decision，移出 INNER、更新 FINISHED。此路径**完全跳过 3b 的 Agent 和 3c 的 npc integrate**，不要求 worktree RESULT/manifest，也不等待“已整合”标记。实施或验证失败不得进入 DONE/INNER；验证失败时先核对本次 commit 并 revert，再走 `npc auto-decide --seq "$SEQ" --trigger implementer-failed --apply`（交互档等待决策），显式重试或记终态，依赖失败的下游跳过。

每波内环全部落定后发 `wave.done`（字段同 3e，`wave` 替 `layer`），波间检查点与 re-plan 同 3f。恢复同样先经过 Step 1 的恢复闸门；无后台能力不代表可以忽略已有任务。

代价即 Step 3 要解决的问题：整波等最慢的一个 change，下一波空转。仅在正确性优先于墙钟、或宿主能力不足时使用。

---

## Step 4 — 收尾

```bash
npc state finalize && npc summary render && npc index append
npc cost --since "$RUN_T0"
```

`finalize` 若因 `needs-user-decision` 返回 exit 1：先把悬而未决的 change 按 3d 的决策分支处理掉再重跑 finalize。收尾汇报对照 run-summary.md 的 Goal Coverage 段提示用户核对缺口（逐 change 全过 ≠ 组合达标），缺口给出新 change 建议清单。

## Output（给用户的最终汇报）

```
## Spine Run 完成：<final_status>

**模式**：auto | interactive        **调度**：pipeline（max-parallel N）| serial
**计划**：N changes / L 层           **结果**：archived A / failed F / skipped S
**用时**：<duration>                 **并发峰值**：<max parallel 实际达到>

### 各 change
- change-a  archived @ <commit>  (review 2 轮, 层 1)
- change-b  archived @ <commit>  (review 0 轮, 层 1)
- change-c  skipped — <reason>

### Goal Coverage 缺口
- <run-summary.md 指出的组合缺口与建议新 change>

### 轨迹与日志（供后续分析）
- 状态：<state_json 路径>
- 汇总：<run-summary.md 路径>
- 跨 run 指标：~/task_log/_telemetry/
- 想优化本 harness？跑 `/spine-analyze`
```

---

## Guardrails（硬约束）

- **你不写业务代码**。所有实现/修复一律交给 coder（in-session implementer sub-agent 或 npc headless 子进程）。你只触发 coder、收 RESULT 行、调 npc 装订。
- **生成 ⊥ 验证（不变量 1）**：coder（生成）与 review（验证）永不同源。coder 路由到任何第三方 provider 时，`npc review run` 必须仍走 codex/Claude。
- **廉价 provider 只许执行（不变量 4）**：仅用于 coder 层。主 session 决策、DAG 架构师裁定、`/spine-analyze`、`npc review run` 一律 premium 层。
- **不读原文**：不逐轮读 review JSON、不手写 cherry-pick/sed、不批量读 changes 原文、不读日志/summary/review/prompt 模板原文。只读 npc 子命令返回的一行 JSON 的关键字段；需要细节时引用 `pointer` 路径或 spawn 只读 triage agent。
- **经验层只看标量**：prompt render 回执的 `experience_injected` / `experience_error`、record 回执的 `experience_contaminated`、archive 回执的 `experience.ok`。**永不读** `<base>/*.experience.md` 进主 session；经验只给 coder，绝不喂给 review。
- **每个 npc 命令后检查 `.ok` 与 exit code**：exit 1 业务失败 / 2 用法错 / 3 环境错 / 4 依赖缺失。依赖缺失（4）立即停并提示安装。
- **调度只信 `npc plan ready`**：不为了填满槽位而 spawn 判为 `blocked` 的 change——尤其带 `dep-pending` 的：其 worktree 基线不含依赖代码，implement 必然写在错误前提上。槽位空着是正确状态，`limit` 与 `file-conflict` 同理。不凭 `v4-waves.json` 的波号自行判断"这波该开了"。
- **main 互斥**：后台 `change run` 在跑时不调 `npc integrate`（RESULT 入 `PENDING` 等内环结束）；收到 `step=inner-loop-active` 只入队，不 `--force`。
- **auto 档绝不调用 AskUserQuestion**——范围、计划、执行决策一律用确定性默认或 `npc auto-decide`；只有硬依赖缺失（exit 4）或缺人类凭据这类**客观阻塞**才停。交互档绝不在未确认时执行破坏性动作（archive / abort）。
- **宿主的工具权限提示（写文件/Bash 授权弹窗）不归本 playbook 管**——那是运行时 permission 层。要无人值守跑 auto，请按宿主机制放行：Claude Code 用 acceptEdits / bypassPermissions 或项目 settings allowlist（`npc init --auto` 会代写，见 docs/usage.md）。
- **续跑优先**：`npc init` 报 `needs_resume` 时永远先 `resume detect` 接断点，不要新建覆盖。
- **change 粒度单一**：拆解目标时，一个 change 只做一件可独立交付的事；过大就再拆。
- playbook 行为与 npc 不符 → 修 npc（`src/npc`）并重装，不在 playbook 内补脚本。
- commit 禁 AI 署名 trailer；禁 `--no-verify`。
- 全程用 **TodoWrite** 反映真实进度，让用户可实时观察这个长时 run。
