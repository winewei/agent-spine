---
name: spine-run
description: 本地自主 harness ——输入一句话目标或一批 openspec change，主 session 负责工程判断：按真实依赖调度，每个 change 在独立 worktree 内完成 implement/review/fix，验证后发布到启动分支（npc integrate / npc change run），跑完 implement→review→fix→archive 全循环。当用户说"spine-run""跑 changes""并行推进 changes""自主实施这个目标"时触发。
metadata:
  category: Workflow
  tags: [harness, autonomous, orchestration, openspec, parallel, worktree, review-loop]
---

> **宿主适配**：本 playbook 是宿主中立的主 session 指令，可在任意 agent CLI（Claude Code / Kimi CLI / Qwen Code / Codex / OpenCode / …）内执行。文中的宿主机制按下表映射，宿主缺某机制时用通用回退：
>
> | 文中写法 | Claude Code | 其它宿主通用回退 |
> |---|---|---|
> | `Agent(...)`（spawn sub-agent） | `Agent` 工具 | 宿主的 sub-agent 派发机制；没有则改用 `npc implement run` / `npc fix run`（headless coder 子进程，效果等价） |
> | `isolation="worktree"` | Agent 工具参数 | 用 Bash `git worktree add` 手建隔离工作区，或使用 `npc change run --isolated` 自动管理 worktree |
> | `AskUserQuestion` | 同名工具 | 直接向用户提问并等待回复 |
> | `TodoWrite` | 同名工具 | 宿主的任务清单机制；没有则维护一份 markdown 清单 |
> | `EnterPlanMode` / `ExitPlanMode` | plan 模式审批门 | 打印计划全文，请用户确认后继续（`--auto` 档两边都跳过） |
> | 后台执行隔离内环（Step 3b） | Bash 工具 `run_in_background: true`，完成通知回主 session | `nohup … > <log> 2>&1 &` 记 PID 轮询；两者都没有则前台串行执行——正确但丧失流水化收益，等价于 `--serial` |
> | 后台 monitor 通知 | `Monitor` 工具 | 至多 60 秒有界等待 + `npc monitor tick --format line` |

# spine-run

你负责把软件需求推进到可验证的交付。**npc 负责 Git、状态、回执和验证等机械动作，agent 负责工程判断。** 默认分派独立任务；主 session 可以按需读规格、源码、review 和日志，诊断根因、调整任务、处理冲突或直接做小修复，随后交独立 reviewer。避免重复搬运材料，不禁止理解材料。

## 本 playbook 怎么用（上下文预算）

本 playbook 只在启动时调用一次。Step 0–2 只执行一次，之后主 session 常驻的只有 **Step 3 调度循环**：收单 → 补位 → 发布，直到全部 change 终态。低频分支不放进常驻正文，出现对应情况时再取对应分节，读完照做：

| 触发 | 读取 |
|---|---|
| 多个 change 需要分析依赖（Step 2） | `npc playbook show spine-run --section plan` |
| monitor 输出里出现需要判断的动作，或要登记 job | `npc playbook show spine-run --section monitor` |
| 发布回执不是 `archived` | `npc playbook show spine-run --section publish` |
| 续跑、compaction 之后、接手他人 session | `npc playbook show spine-run --section recovery` |
| 全部终态或中止，进入收尾 | `npc playbook show spine-run --section finish` |

回执遵循“结果留盘、context 只放指针”：npc 命令只回一行，正文留在 `$RUN_DIR` 下的文件里，需要时才读。monitor 的 follow 输出是一行纯文本，完整结构用 `npc monitor list --open` 按需取。

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
| `--max-parallel N` | 4 | 整个开发闭环的并发预算（implement + review + fix 共享，不各自算 N） |
| `--serial` | off | 并发预算设为 1，仍使用隔离闭环；`--serial-waves` 为同义旧名 |
| `--no-architect` | off | 跳过语义裁定，直接用机械候选波次 |
| `--webhook URL` / `--webhook-format` | env / raw | 进度外呼（`npc notify`，永不阻塞） |

**`--auto` 的硬规则（fire-and-forget）**：绝不调用 AskUserQuestion；机械失败由 npc 返回证据，工程分叉由主 agent 根据代码与测试自主决定，一路跑到底。范围决策 → 拆出来的全部 change 一次跑完；plan 确认 → 不进 plan 模式，直接 `init-run`；执行中例行决策 → 优先检查现有产物、失败证据和根因，`npc auto-decide` 只是建议，不以降低审查标准代替收敛。唯一例外：硬依赖缺失（exit 4）或需要人类凭据/外部授权时停下说明。

**成本路由硬规则**（docs/principles.md 不变量 1 & 4）：coder（implement/fix）可以由 npc provider 注册表路由到廉价 Anthropic 兼容端点（`[coder].backend` / `[coder.phase]`）；主 session 编排、架构裁定、`npc review run`、`/spine-analyze` 恒用 Claude / codex。第三方廉价 provider **只许执行，绝不用于决策与分析/验证**，`npc verify routing` 在代码层强制。原生交接路径的 implement/fix 使用宿主 agent 与其工具，不能因为工具只能调 CLI 就强迫丢弃原生修复上下文。

---

## Step 0 — 前置检查（缺依赖立即停）

- `npc --version` ≥ 1.10.0；缺或版本过低 → 提示从发布 tag 安装（`uv tool install --reinstall --from git+https://github.com/winewei/agent-spine@v<版本> npc`）并停止；开发期验证用仓库内 `uv run npc`，不要用 `--from .` 本地目录安装。
- `npc doctor` 通过；配置的 review engine 及其可执行程序/凭据必须可用（默认 codex）。缺失时在初始化前停止；不能声明“跳 review”后仍调用 `change run --from review`，也不能静默免审归档。`experience` 项为 warn 只记一行降级；经验层由 `npc agent prompt render` / `npc archive run` 自动处理，主 session 不读经验正文，也绝不把经验给 review。
- `openspec` 可用（`openspec list --json` 是计划入口）。
- git 仓库且启动工作区 clean、位于命名分支。`npc init` 记录该分支的完整 ref 与启动提交，它是本次 run 的整合目标，绝不默认 checkout main/master；续跑保留原目标，不能偷偷重绑定。
- worktree 隔离可用：Claude Code 宿主要求 `worktree.baseRef=head`，否则报错退出；其它宿主用 `git worktree add`（基于 HEAD）。宿主既无 sub-agent 并发也无后台执行时按 `--serial` 走 Step 3S。

任一硬依赖缺失：用一句话告诉用户缺什么、怎么装，**不要继续**。用宿主任务列表建一个贯穿全程的清单（init / plan / 每个 change 一项 / 收尾），实时更新。

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
- `needs_resume=true`、经历过 context compaction、或接手他人 session → 读 `--section recovery` 并完成恢复闸门后才可跳过 Step 2 进入 Step 3；`npc resume detect` 的单个 `next_phase` 不能代表并行盘面。恢复时禁止使用 `--fresh`。
- 宿主每次调用使用独立 shell 时，从已保存的 `$RUN_DIR/run.json` 重新读取路径变量并 export，不能假定前一调用的 shell 变量仍存在。

### 启动 monitor（计划分析之前，整个 run 只启动一个）

初始化后立即执行 `npc monitor tick`，创建或恢复 `$RUN_DIR/monitor.json`，随后在后台启动 `npc monitor follow --interval 60`（Claude Code 用 `Monitor(command="npc monitor follow --interval 60")`，到期后立即重新挂接）。它是确定性后台监控，不占 LLM 槽位；只在出现新动作时输出**一行**，例如：

```
monitor: 3 open, 2 pending | *#12 CHECK_IN impl-003 -> agent:a1b2; #9 DONE_SIGNAL review-002 | detail: npc monitor list --open
```

每行都包含全部未处理动作的 id，足以 ack；`*` 是本次新增。只有这一行进 context，需要判断时再读 `--section monitor`。无推送机制的宿主用至多 60 秒有界等待 + `npc monitor tick --format line`；仅 `nohup` 写日志不会唤醒 session，不能据此宣称主动监控已工作。不要另写等待 RESULT 文件或日志的临时监控脚本。

**覆盖所有执行体**：每次派发后立即登记唯一任务 id、原生任务句柄、角色和专属产物。分析、架构、implement、review、fix、测试、headless 子进程都必须登记；同一 agent 的新任务使用新 id；不要把 monitor 自己登记进去。远程作业、长测试登记为 `--kind job`（见 `--section monitor`）。

```bash
npc monitor register --id "$JOB_ID" --role "$ROLE" --handle "$AGENT_HANDLE"   --worktree "$WT" --artifact "$RESULT_FILE" --done "test -s '$RESULT_FILE'"
```

非编码任务可省略 `--worktree`，登记分析结论、review JSON 或测试结果文件；有结果文件的任务加 `--done`，结果落盘即产生 `DONE_SIGNAL`。内部 review/fix 子进程由其持久 npc worker 的句柄负责，不重复登记。`monitor.json` 是监控清单；`scheduler.json` 仍由主 session 唯一写入。

---

## Step 2 — 计划

### 2.0 确定 NODES（要跑的 change 集合）

**A. 已存在的 change 名** → `openspec list --json` 确认每个都存在且 in-progress，NODES = 参数列表。

**B. 自由目标 → 拆解**（复杂目标可交给 architect 类 sub-agent；主 session 对拆解负责）：把目标拆成若干**单一职责**、可独立 implement+review+archive 的 change，显式给出依赖；逐个 `openspec new change "<name>"` 生成脚手架并补齐 artifact（proposal / specs / design / tasks），全部 `openspec validate <id> --strict` 通过后一次 commit。NODES = 新建的全部 change；交互档把清单与每个 change 一句话意图列给用户确认。

**C. 空输入** → NODES = `openspec list --json` 的全部 in-progress change。交互档若为空则 AskUserQuestion 问要做什么并转 B；auto 档为空直接结束并说明。

### 2.1 真实依赖与整合风险

`|NODES| == 1` 时不派分析 agent：令 `CID` 为唯一 change、`FINAL_WAVES = [[CID]]`，先写入 Step 3 要读取的 DAG 文件，再进入 2.2：

```bash
jq -n --arg cid "$CID" '{nodes:[$cid],edges:[],files:{},tie_break:{}}' > "$RUN_DIR/v3-dag-extract.json"
```

多个 change 时读 `--section plan`，按其中的分析深度规则产出 `v3-dag-extract.json`（nodes / edges / files / ordering_edges）与 FINAL_WAVES。

### 2.2 落地

运行时只接纳具体依赖对，不从波次位置创造依赖。确认 FINAL_WAVES 展平覆盖 NODES，保留原 edges，只追加架构裁定的 `ordering_edges`。例如 A→C、B 独立，A 完成就能开 C，不能因为展示成 [A,B]→[C] 就强加 B→C。

```bash
DAG="$RUN_DIR/v3-dag-extract.json"
jq '.edges = (((.edges // []) + (.ordering_edges // [])) | unique)' "$DAG" > "$DAG.tmp" && mv "$DAG.tmp" "$DAG"
```

先打印 Wave Plan Summary（波次、拆分理由、降级/提级）。交互档此时 ExitPlanMode 并等待批准；用户调整计划则返回 2.1 重算并重新确认，**批准前不能执行 `state init-run`**。`--auto` 跳过审批。计划获批（或 auto 档确定）后：

```bash
npc state init-run --plan-order "$(jq -nc --argjson w "$FINAL_WAVES" '$w|add')" --goal "<用户的原始目标一句话>"
echo "$FINAL_WAVES" > "$RUN_DIR/v4-waves.json"
```

把已采用的 Wave Plan Summary 留痕到 run.events.jsonl。

---

## Step 3 — 调度循环（常驻部分）

**生命周期**：implement → 独立 review → fix → 再 review，全部在同一个 change worktree 内进行；通过后进入发布队列，组合测试也在 worktree 内，最后才短暂持有目标工作区锁快进与 archive。多个 change 的 review/fix 可以同时推进，不能只并行 implement。

**容量**：MAX_PARALLEL 默认 4，`--serial` 为 1；implement、原生 fixer、headless review/fix 共享预算。原生 agent 返回交接后要释放其执行槽位，再启动该 change 的 reviewer；不要把等待型 agent 算作免费容量。

**集合**：ACTIVE = 正在 implement/review/fix（INNER 为其中的内环子集）；PENDING = 已验证待发布；DONE = 已发布到启动分支；FINISHED = archived/skipped/failed。任何失败未明确裁定前不算终态，尤其锁竞争、目标推进、待解决冲突、待人工/agent 决策。

**检查点**：保留每个 CID 的 SEQ、worktree、任务句柄、阶段、RESULT、manifest 和待执行动作。spawn 前记 launching，获得句柄后立即更新；收单先保存完整产物再改集合。主 session 是 scheduler checkpoint 的唯一写入者：

```bash
jq -n --arg run_ts "$RUN_TS" --argjson done "$DONE" --argjson finished "$FINISHED" \
  --argjson active "$ACTIVE" --argjson pending "$PENDING" --argjson inner "$INNER" --argjson jobs "$JOBS" \
  '{run_ts:$run_ts,done:$done,finished:$finished,active:$active,pending:$pending,inner:$inner,jobs:$jobs}' \
  > "$RUN_DIR/scheduler.json.tmp" && mv "$RUN_DIR/scheduler.json.tmp" "$RUN_DIR/scheduler.json"
```

### 3a. 按真实依赖补位

`SLOTS = MAX_PARALLEL - |ACTIVE|`，<=0 时不派新任务。优先恢复已有 worktree 的下一阶段，其次选择能解锁更多下游的 ready 任务。每次任务返回、发布结束或阻塞原因改变都重新计算，不等一整波。

```bash
DAG="$RUN_DIR/v3-dag-extract.json"
READY=$(jq -c --argjson done "$DONE" --argjson active "$ACTIVE" --argjson pending "$PENDING" --argjson finished "$FINISHED" --argjson lim "$SLOTS" \
          '{nodes,edges,files,tie_break} + {file_policy:"isolated", done:$done, active:($active + $pending), finished:$finished, limit:$lim}' "$DAG" \
        | npc plan ready | jq -r '.ready[]')
```

`integration_risks` 提示文件重叠，不阻止隔离开发；`dep-pending` 仍是硬依赖。ACTIVE∪PENDING 只排除重复派单。不得删除真实依赖来填槽；长期低利用率时检查依赖依据、任务切分和整合热点并主动重排。

### 3b. 启动或恢复一个 change

先执行 `npc state add-change "$SEQ" "$CID"`（只对新项）；每个 change 只有一个执行所有者。

**默认：宿主原生 agent 编码。** 用以下命令取得持久化交接任务：

```bash
npc change run --seq "$SEQ" --isolated --handoff ${AUTO:+--auto} > "$RUN_DIR/change-run-$SEQ.json" 2> "$RUN_DIR/change-run-$SEQ.stderr.log"
```

- `needs-coder` → 读取 `phase/round/worktree/prompt_file/prompt`，派原生 coder 在**给出的同一个 worktree** 中完成任务，不另建隔离分支。允许其读源码、运行调试工具、补测试、验证假设；复用合适的现有 agent 可以保留修复上下文。保存句柄，确认无同一任务存活后才重派。
- coder 必须提交实际代码，保存 RESULT 文件（implement 用 tasks；fix 用 fixed/categories_scanned/regressions_added）和 manifest，都返回 commit、tests、summary；主 agent 可以查看必要证据，不能把自报 pass 当真实测试。
- 收单后释放该原生执行槽位，再用 `npc change run --seq "$SEQ" --isolated --handoff --result-file "$RESULT_FILE" --manifest "$MANIFEST"` 继续。npc 校验产物并独立 review；需要下一轮 fix 时再次返回 needs-coder。不要每次加 `--from review`，它会强制新增一轮。
- 已有 implementer worktree 可用首次 `--worktree "$WT" --result-file "$RESULT_FILE" --manifest "$MANIFEST"` 接入。

**headless 替代路径**：不加 `--handoff`，npc 在独立 worktree 内按 `[coder]` 调 implement/fix 子进程，review 仍是独立验证。用宿主后台任务运行，按实际占用计入同一并发预算。

### 3c. 发布已准备产物

`ready-to-integrate` → ACTIVE/INNER 移除、PENDING 加入，先保存回执，然后：

```bash
npc integrate --seq "$SEQ" --prepared
```

`archived` → PENDING 移除，DONE/FINISHED 加入并续闸；正常路径只有此处满足下游代码依赖。其它任何 `status`（`target-busy` / `target-moved` / `archive-failed` / `needs-review` / `needs-resolution` / `tests-failed` / `needs-recovery` / `needs-decision` / `aborted`）读 `--section publish` 按对应分支处理；不要绕过锁或使用 force。

### 3d. 工程反馈与主动重排

空槽持续存在、整合风险集中、同根因重复 review/fix、阶段超时或需求变化，都是重新检查计划的信号。按需读规格、源码、review/findings、测试日志；需要独立意见时派有明确边界的分析任务，避免每次都重读所有文档或固定派双架构师。优先保持原 coder 的修复上下文，必要时换执行体；小改动可由主 agent 在该 worktree 直接修复，再交独立 reviewer。一行回执只是为了省 context，不得让 agent 为遵守一行摘要而放弃诊断能力。

记录执行时长、等依赖/容量/发布时间、重复阶段和失败原因。`max(实现工作量/容量, 内环工作量/容量)` 只是资源下界，不能当墙钟承诺。FINISHED 覆盖全部 NODES 后读 `--section finish` 收尾。

## Step 3S — 单执行槽位

`--serial` 仅把 MAX_PARALLEL 设为 1，仍使用 Step 3 的 worktree 闭环、回执和发布协议。宿主没有原生 agent 时使用 headless 路径；没有后台能力时前台运行。不要恢复为“先把所有实现放到共享工作区，再串行修复”。

---

## Guardrails（保护工程产物，不限制工程判断）

- 整合目标是 run 启动分支；禁止硬编码 main/master，禁止续跑时静默重绑定。
- 每个 change 一个 worktree、一个写入所有者；主 agent、原生 coder 与 headless coder 不同时写同一工作区。
- coder 与 reviewer 保持独立；经验只提供给 coder。不得把 reviewer 变成 coder 自评，也不降低 blocking 标准来制造吞吐。
- 结构化回执决定状态；源码、日志、规格与审查原文是可按需读取的诊断证据。不要全量搬运，也不要禁止读取。
- 调度遵守真实依赖；文件重叠是整合风险。主动检查不合理依赖、任务拆分和资源饥饿。
- 同一并发预算覆盖 implement/review/fix；排队不是运行。不要派等待型 agent 占槽。
- 中断保留 worktree、commit、RESULT、review 与测试回执；恢复前核对旧进程，不重复执行已完成阶段。
- 只有发布/归档短临界区占目标工作区锁；绝不手动删除活锁，绝不 force 绕过验证。
- 用户可随时转向或中止。auto 档自主处理工程决策；真正缺凭据或授权时才请求输入，不能假装成功。
- 用户授权优先；工具权限由宿主控制。commit 禁 AI 署名 trailer，禁 `--no-verify`。
- 全程用宿主任务列表反映真实推进，汇报产物与证据，而不只汇报进程存活。
