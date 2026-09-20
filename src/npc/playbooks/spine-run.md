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

# spine-run

你负责把软件需求推进到可验证的交付。**npc 负责 Git、状态、回执和验证等机械动作，agent 负责工程判断。** 默认分派独立任务；主 session 可以按需读规格、源码、review 和日志，诊断根因、调整任务、处理冲突或直接做小修复，随后交独立 reviewer。避免重复搬运材料，不禁止理解材料。

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
| `--max-parallel N` | 4 | 整个开发闭环的并发预算（implement + review + fix 共享，不各自算 N） |
| `--serial` | off | 并发预算设为 1，仍使用隔离闭环；`--serial-waves` 为同义旧名 |
| `--no-architect` | off | 跳过语义裁定，直接用机械候选波次 |
| `--webhook URL` / `--webhook-format` | env / raw | 进度外呼（`npc notify`，永不阻塞） |

**`--auto` 的硬规则（fire-and-forget）**：auto 档下你**绝不调用 AskUserQuestion**，机械失败由 npc 返回证据；工程分叉由主 agent 根据代码与测试自主决定，一路跑到底：

- **范围决策**（目标拆成 N 个依赖递进的 change → 这轮跑哪些）→ **跑完整依赖链**：拆出来的全部 change 一次跑完，不挑子集、不问。
- **plan 确认** → 不进 plan 模式，直接 `init-run`。
- **执行中例行决策**：优先检查现有产物、失败证据和根因；调整实现、任务边界、执行体或剩余计划。`npc auto-decide` 是建议，不以降低审查标准代替收敛。
- 唯一例外：硬依赖缺失（exit 4）或需要人类凭据/外部授权时，停下说明——这不是"决策"，是无法自主完成的客观阻塞。

---

## 成本感知路由（模型分层，见 docs/principles.md 不变量 1 & 4）

| 层 | 角色 | 跑在哪 |
|---|---|---|
| **执行层** | coder（implement / fix 写代码） | 由 npc 的 provider 注册表决定：默认 `claude`；在全局 `[providers.*]` 注册 deepseek / kimi / qwen / mimo 等 Anthropic 兼容端点后，用 `[coder].backend` 或 `[coder.phase].implement/fix` 路由（见 docs/configuration.md） |
| **premium 层（决策 + 分析/验证）** | 主 session 编排、DAG 架构师裁定、`npc review run`、`/spine-analyze` | 恒 Claude / codex |

原生交接路径的 implement/fix 使用宿主 agent 与其工具；headless 路径使用 `[coder]` / `[coder.phase]` 配置起子进程。按任务质量与成本选择执行体，不能因为工具只能调 CLI 就强迫丢弃原生修复上下文。

**硬规则**：第三方廉价 provider **只许执行，绝不用于决策与分析/验证**。review 恒留 codex/Claude——`npc verify routing` 在代码层强制（review 与 coder 不同源）。

---

## Step 0 — 前置检查（缺依赖立即停）

- `npc --version` ≥ 1.8.1；缺或版本过低 → 提示从发布 tag 安装（`uv tool install --reinstall --from git+https://github.com/winewei/agent-spine@v<版本> npc`）并停止；开发期验证用仓库内 `uv run npc`，不要用 `--from .` 本地目录安装
- `npc doctor` 通过；配置的 review engine 及其可执行程序/凭据必须可用（默认 codex）。缺失时在初始化前停止并提示安装或配置可用的 reviewer；不能声明“跳 review”后仍调用 `change run --from review`，也不能静默免审归档。
- 经验层（可选，1.8）：`npc doctor` 的 `experience` 项为 warn 时只记一行降级、不阻塞——它是旁路增强。项目 `[experience].enabled=true` 时 `npc agent prompt render` 自动召回注入、`npc archive run` 自动提交轨迹，本 playbook 不需要额外步骤；主 session 只看回执里的 `experience_injected` / `experience.ok` 标量，不读经验正文，也绝不把经验给 review
- `openspec` 可用（`openspec list --json` 是计划入口）
- git 仓库且启动工作区 clean、位于命名分支。`npc init` 记录该分支的完整 ref 与启动提交；它是本次 run 的整合目标，可能是 feature/release 分支，绝不默认 checkout main/master。续跑保留原目标；切换分支或改写历史时先核对，不能偷偷重绑定。
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

### 启动 monitor（计划分析之前，整个 run 只启动一个）

初始化路径后立即执行 `npc monitor tick`，创建或恢复 `$RUN_DIR/monitor.json`，随后启动 `npc monitor follow --interval 60`。它是确定性后台监控，不占常驻 LLM 编码槽位；默认每 10 分钟询问，15 分钟无产出标记停滞，已发送询问给 5 分钟回复期限。只在事件变化或询问周期输出，不要每分钟唤醒 LLM 重读所有日志。

使用宿主**实际可用**的后台通知机制将输出交回主 session；Claude Code 有 `Monitor` 时可用 `Monitor(command="npc monitor follow --interval 60", persistent=true)`。Codex 或其它宿主若无推送机制，主 session 使用至多 60 秒的有界等待，并执行 `npc monitor tick` 处理到期事件。仅 `nohup` 写日志不会唤醒 session，不能据此宣称主动监控已工作。需要独立 monitor agent 时按事件启动短诊断任务，并计入真实槽位预算。

**覆盖所有执行体**：每次派发后立即登记唯一任务 id、原生任务句柄、角色和专属产物。分析、架构、implement、review、fix、测试、headless 子进程都必须登记，不能只登记 coder。同一 agent 的新任务使用新 id；不要把 monitor 自己登记进去。

```bash
npc monitor register --id "$JOB_ID" --role "$ROLE" --handle "$AGENT_HANDLE"   --worktree "$WT" --artifact "$RESULT_FILE"
```

非编码任务可省略 `--worktree`，登记分析结论、review JSON 或测试结果文件；产物路径可在生成前登记，多个产物重复传 `--artifact`。内部 review/fix 子进程由其持久 npc worker 的句柄负责，不对同一个进程重复登记；角色/阶段与日志路径保存在 scheduler。`monitor.json` 是监控清单；`scheduler.json` 仍由主 session 唯一写入。

**处理事件**：

1. `CHECK_IN`：对所有到期 agent 并行发送宿主原生询问：“最近一个检查周期完成了什么？提供 commit、diff、测试/review 或分析产物；当前阻塞是什么？下一步和预计完成时间？”发送成功后才执行 `npc monitor ack --action-id ID --decision sent`。相同 action id 不重复询问，`sent_at` 非空表示已发；发不出去先核对句柄和原任务，不能伪造 sent。
2. 收到回复后检查实际证据。`--decision progress --note '证据与结论'` 记录核验；长测试/调研仍合理时 `--decision wait --wait-seconds 600 --note '进程/阶段证据、原因与期限'`，最多等待 900 秒，到期重新询问。ack 和“仍在工作”的口头报告不会重置实际产出时钟。
3. `no_progress=true` 或 `CONTROL_REQUIRED`：主 session 必须诊断，不只转述警报。检查卡住的进程、依赖、重复 findings 和测试；选择缩小任务、补充信息、调整方案、继续有界等待或接替执行，并用 `--decision intervene --note '证据、已采取动作与下一检查点'` 落账。`CONTROL_REQUIRED` 只有询问确已发送且到期未处理才产生；即使发现新产物，也须核验并给出判断。
4. 停止/接替前保存 worktree、提交、RESULT 和日志，确认原执行体已退出；未知状态仍占位。核验收单或退出后执行 `npc monitor finish --id ID --note '收单/退出证据'`；接替者使用新 id，并继续原 worktree。monitor 不自行 kill、不改 Git 或业务 state、不替主 session 发布代码。

文件内容/专属 worktree HEAD 或 diff 变化只是工作证据，不保证有用收敛；即使一直变化，也必须每 10 分钟询问。转录 mtime、心跳、工具调用次数、共享目标 HEAD 不算实际进展；同根因反复修复须主动重排。未跟踪文件需通过专属 `--artifact` 登记。`npc watch --once` 可补充发现宿主任务，核对是否漏登；不能将其它 session 的历史任务加入本 run。

恢复时重新连接唯一后台 monitor（重复 follow 会被拒绝），保留清单、待处理 action id、发送记录和期限。先核验宿主存活任务与 monitor/scheduler 清单是否一致，再续闸；不得重新注册来重置计时。run 完成或中止时核实所有执行体退出、逐项 finish，执行 `npc monitor stop`，follow 会退出。不要因一次扫描没有 agent 就认为 run 已完成。

### 恢复闸门（禁止先清空集合）

先暂停所有新派发和目标工作区写入，读取 `npc status --brief`、完整 `$STATE_JSON`、`$RUN_DIR/scheduler.json`（若存在）和 run.events.jsonl，并重新加载原 DAG 与 v4-waves.json。`npc resume detect` 只供每个 change 的 phase 定位参考，不能据此直接跳进空盘面。

1. 从 state 的 `plan_order`、`isolation.worktree`、`pending_coder`、`prepared`、`publication` 与 scheduler checkpoint 重建任务盘面，核对宿主任务句柄。保存的 worktree/receipt 是恢复依据，不重建、不 stash 后丢弃。
2. 原任务仍运行时重新连接，不能再派同一任务。转录 mtime 只能说明活动，不能证明业务推进；用新增提交、阶段迁移、测试/审查产物核对推进。未知状态先保留占位，核实后再补位。
3. `prepared` 存在 → 重试 `npc integrate --prepared --seq N`；已快进但未归档时会续归档，不重跑 implement/fix。`pending_coder` 存在 → 接回原原生 agent 或核验其 RESULT/manifest；相同 handoff 回执不是新的任务。
4. `npc change run --isolated --seq N` 自动恢复阶段：已完成 fix 不重复执行，已完成 review 按 blocking 进入 fix 或准备发布。存在未装订的提交或脏 worktree 时保留现场，主 agent 按需检查并补齐回执，不丢弃产物从头来。
5. 老版本已整合的 change 可沿用兼容命令完成；新隔离协议必须有可核验的启动目标。旧 run 没记录分支时，不能从当前 checkout 猜原目标。不要混用旧内环和新隔离内环来处理同一 change。
6. FINISHED 只包含确证的终态。DONE 必须有目标分支上的整合证据，不能只看 implement 完成。恢复后检查容量并续闸。

---

## Step 2 — 计划

### 2.0 确定 NODES（要跑的 change 集合）

**A. 已存在的 change 名** → `openspec list --json` 确认每个都存在且 in-progress，NODES = 参数列表。

**B. 自由目标 → 拆解**（复杂目标可交给 architect 类 sub-agent；主 session 按需读规格并对拆解负责）：
1. 把目标拆成若干**单一职责**的 change（每个 change 一件可独立 implement+review+archive 的事；过大就再拆），显式给出 change 间依赖。
2. 为每个 change 起 kebab-case 名，逐个 `openspec new change "<name>"` 生成脚手架，补齐 implement 所需 artifact（参照工程内 openspec schema：proposal / specs / design / tasks），全部 `openspec validate <id> --strict` 通过后一次 commit。
3. NODES = 新建的全部 change。交互档把清单与每个 change 一句话意图列给用户确认；auto 档不确认。

**C. 空输入** → NODES = `openspec list --json` 的全部 in-progress change。交互档若 NODES 为空则 AskUserQuestion 问要做什么并转 B；auto 档 NODES 为空直接结束并说明。

### 2.1 真实依赖与整合风险

`|NODES| == 1` 时不派分析 agent：令 `CID` 为唯一 change、`FINAL_WAVES = [[CID]]`，但必须先写入 Step 3 要读取的 DAG 文件，再进入 2.2：

```bash
jq -n --arg cid "$CID" '{nodes:[$cid],edges:[],files:{},tie_break:{}}' > "$RUN_DIR/v3-dag-extract.json"
```

多个 change 时按复杂度选择分析深度；已有可信 DAG 时增量核对，不固定重派分析团队：

1. 简单变更由主 session 直接核对 DAG；复杂目标才 spawn `dag-analyst`（Explore，只读）：按需读变更文档 → 抽 nodes/edges/files（目录级条目用 Grep 展开）→ 跑 `npc plan waves` → 写 `<run_dir>/v3-dag-extract.json` → 回一行 RESULT。校验 nodes 完整、candidate.waves 展平=NODES；失败重发一次，再失败 `--auto` 才降级自抽（记 `dag_extract_fallback`），交互档真停。
2. 仅在耦合复杂或证据冲突时 spawn 架构审查（可并行，只读；`--no-architect` 跳过）：A=senior-system-architect 查语义耦合（共享状态/时序/不变量），B=senior-code-developer 查落地冲突（真实文件/import/构建）。输出具体依赖对及理由：真正需要上游代码/契约的关系放入 edges；必须先后执行的语义约束放入 ordering_edges。共享注册表、同文件不同修改只列为整合风险，不能自动升级成依赖。主 agent 核对证据后裁定，得 FINAL_WAVES 展示计划。

### 2.2 落地

运行时只接纳具体依赖对，不从波次位置创造依赖。确认 FINAL_WAVES 展平覆盖 NODES，保留原 edges，只追加架构裁定的 `ordering_edges`。例如 A→C、B 独立，A 完成就能开 C，不能因为展示成 [A,B]→[C] 就强加 B→C。

```bash
DAG="$RUN_DIR/v3-dag-extract.json"
jq '.edges = (((.edges // []) + (.ordering_edges // [])) | unique)' "$DAG" > "$DAG.tmp" && mv "$DAG.tmp" "$DAG"
```

先打印 Wave Plan Summary（波次、拆分理由、降级/提级）。交互档此时 ExitPlanMode 并等待批准；若用户调整计划，返回 2.1 重算并重新确认，**批准前不能执行 `state init-run`**。`--auto` 跳过审批。仅在计划获批（或 auto 档确定）后执行：

```bash
npc state init-run --plan-order "$(jq -nc --argjson w "$FINAL_WAVES" '$w|add')" --goal "<用户的原始目标一句话>"
echo "$FINAL_WAVES" > "$RUN_DIR/v4-waves.json"
```

把已采用的 Wave Plan Summary 留痕到 run.events.jsonl。

---

## Step 3 — 每个 worktree 承载完整开发闭环

**生命周期**：implement → 独立 review → fix → 再 review，全部在同一个 change worktree 内进行。通过后进入发布队列；组合测试也在 worktree 内，最后才短暂持有目标工作区锁进行快进与 archive。多个 change 的 review/fix 可以同时推进，不能只并行 implement。

**容量**：MAX_PARALLEL 默认 4，`--serial` 设为 1。implement、原生 fixer、headless review/fix 共享预算。原生 agent 返回交接后要释放/关闭其执行槽位，再启动该 change 的 reviewer；不要在等待 agent 内嵌 reviewer 子进程却把它算作免费容量。另有独立 provider 额度时可以明确调整预算，但不能把同一套 4 槽计算两遍。

集合：ACTIVE 包含正在 implement/review/fix 的 change（INNER 为其中的内环子集）；PENDING 是已验证待发布项；DONE 是已发布到启动分支的项；FINISHED 是 archived/skipped/failed。任何失败未明确裁定前不算终态，尤其锁竞争、目标推进、待解决冲突、待人工/agent 决策。

检查点保留每个 CID 的 SEQ、worktree、任务句柄、阶段、RESULT、manifest 和待执行动作。spawn 前记 launching，获得句柄后立即更新；收单先保存完整产物再改集合。主 session 是 scheduler checkpoint 的唯一写入者：

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

`integration_risks` 提示文件重叠，不阻止隔离开发；`dep-pending` 仍是硬依赖。ACTIVE∪PENDING 只排除重复派单。不得删除真实依赖来填槽；若长期低利用率，检查依赖依据、任务切分和整合热点并主动重排。共享注册表可以由 agent 合并不同注册项，只有反复冲突且收益明确时再改成分片结构。

### 3b. 启动或恢复一个 change

先执行 `npc state add-change "$SEQ" "$CID"`（只对新项）；每个 change 只有一个执行所有者。

**默认：宿主原生 agent 编码。** 用以下命令取得持久化交接任务：

```bash
npc change run --seq "$SEQ" --isolated --handoff ${AUTO:+--auto} > "$RUN_DIR/change-run-$SEQ.json" 2> "$RUN_DIR/change-run-$SEQ.stderr.log"
```

- `needs-coder` → 读取 `phase/round/worktree/prompt_file/prompt`，派 Codex 或 Claude Code 原生 coder 在**给出的同一个 worktree** 中完成任务，不再另建一个隔离分支。允许其读源码、运行调试工具、补测试、验证假设。复用合适的现有 agent 可以保留修复上下文。保存句柄，确认无同一任务存活后才重派。
- 要求 coder 提交实际代码，保存 RESULT 文件（implement 使用 tasks；fix 使用 fixed/categories_scanned/regressions_added）和 manifest。两种阶段都必须返回 commit、tests、summary；主 agent 可以查看必要证据，不能把自报 pass 当真实测试。
- 收单后释放该原生执行槽位，再用 `npc change run --seq "$SEQ" --isolated --handoff --result-file "$RESULT_FILE" --manifest "$MANIFEST"` 继续。npc 校验产物并独立 review；需要下一轮 fix 时再次返回 needs-coder。不要每次加 `--from review`，它会强制新增一轮而失去自动续跑语义。
- 已有 implementer worktree 可用首次 `--worktree "$WT" --result-file "$RESULT_FILE" --manifest "$MANIFEST"` 接入，不必先把未审查实现放到目标分支。

**headless 替代路径**：不加 `--handoff`，npc 在独立 worktree 内按 `[coder]` 调 implement/fix 子进程，review 仍是独立验证。用宿主后台任务运行，按实际占用计入同一并发预算；不要求先整合，也不持目标分支锁等待模型。

### 3c. 发布已准备产物

`ready-to-integrate` → ACTIVE/INNER 移除、PENDING 加入，先保存回执，然后运行：

```bash
npc integrate --seq "$SEQ" --prepared
```

- npc 把启动目标分支的新提交合到 change worktree，校验补丁与已审查补丁完全一致，并在组合树复跑测试；目标分支在此期间可供其他发布使用。当前候选已验证时复用测试回执。
- 组合测试后短暂抢目标锁，复核启动分支身份、基线 HEAD 和干净状态，快进发布并 archive。保存原 implement/fix 提交链，无需 hash 翻译。worktree 保留供核查，不自动删除。
- `archived` → PENDING 移除，DONE/FINISHED 加入并续闸。正常路径只有此处满足下游代码依赖。
- `target-busy` / `target-moved` 表示**启动目标**忙或已推进；保存 PENDING，稍后重试 publication，不重跑 implement。`archive-failed` 同样保留发布回执，解决原因后重试归档。
- `needs-review` → 补丁已改变，转 ACTIVE/INNER，在现有 worktree 执行 `change run --isolated --handoff --from review`，审查必须覆盖最终 diff，包括冲突解决。
- `needs-resolution` / `tests-failed` → agent 读取必要诊断，在原 worktree 合入最新目标提交、解决冲突或测试回归并提交，然后按上述路径重新 review；禁止丢弃原实现或改在目标工作区裸写。
- `needs-recovery` → 核对未装订提交和回执，优先恢复已有工作。`needs-decision` → 主 agent 按证据选择修复、调整执行体、拆分任务或解释阻塞；不得仅因轮数上限把 critical/high 问题当可接受并归档。
- `aborted` → 停止调度并保留所有产物。未知错误先诊断，不猜成成功或终态失败。准备/发布命令可重试；不要绕过锁或使用 force。

### 3d. 工程反馈与主动重排

空槽持续存在、整合风险集中、同根因重复 review/fix、阶段超时或需求变化，都是重新检查计划的信号。主 agent 可以按需读规格、源码、review/findings、测试日志；需要独立意见时派有明确边界的分析任务，避免每次都重读所有 N×4 文档或固定派双架构师。

优先保持原 coder 的修复上下文，必要时换执行体；复用已完成阶段与固定提交的验证结果。小改动可由主 agent 在该 worktree 直接修复，再交独立 reviewer。不得让 agent 为遵守一行摘要而放弃诊断能力。

记录执行时长、等依赖/容量/发布时间、重复阶段和失败原因。`max(实现工作量/容量, 内环工作量/容量)` 只是资源下界，不能当墙钟承诺；关键路径、尾部收尾、限流与组合验证也要计入。收尾报告区分实测和估算。

## Step 3S — 单执行槽位

`--serial` 仅把 MAX_PARALLEL 设为 1，仍使用 Step 3 的 worktree 闭环、回执和发布协议。宿主没有原生 agent 时使用 headless 路径；没有后台能力时前台运行。不要恢复为“先把所有实现放到共享工作区，再串行修复”。

## Step 4 — 收尾

```bash
npc state finalize && npc summary render && npc index append
# 已核验并 finish 所有执行体后停止监控
npc monitor stop
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
