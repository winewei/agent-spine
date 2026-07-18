---
name: New Plan & Implement All Changes (v2, npc-powered)
description: 进入 plan 模式，按优先级规划并逐个实施所有活跃的 openspec changes。v2 把状态/事件/模板等确定性逻辑下沉到 `npc` CLI，主 session 只承担决策与人机交互。
category: OpenSpec
tags: [openspec, plan, implement, v2]
---

> **宿主适配**：本 playbook 是宿主中立的主 session 指令，可在任意 agent CLI（Claude Code / Kimi / Codex / …）内执行。文中的宿主机制按下表映射，宿主缺某机制时用通用回退：
>
> | 文中写法 | Claude Code | 其它宿主通用回退 |
> |---|---|---|
> | `Agent(...)`（spawn sub-agent） | `Agent` 工具 | 宿主的 sub-agent 派发机制；没有则改用 `npc implement run` / `npc fix run`（headless coder 子进程，效果等价） |
> | `isolation="worktree"` | Agent 工具参数 | 用 Bash `git worktree add` 手建隔离工作区，或把该波降级为串行 `npc implement run` |
> | `AskUserQuestion` | 同名工具 | 直接向用户提问并等待回复 |
> | `TodoWrite` | 同名工具 | 宿主的任务清单机制；没有则维护一份 markdown 清单 |
> | `EnterPlanMode` / `ExitPlanMode` | plan 模式审批门 | 打印计划全文，请用户确认后继续（`--auto` 档两边都跳过） |

**目标**
进入 plan 模式，分析所有活跃的 openspec changes，**用 DAG 拓扑排序**（节点 = change，边 = 来自 spec delta / 显式引用 / 文件创建三条硬规则）决定串行实施顺序，然后逐个跑 Implement → Codex Review → Fix Loop → Archive。tier（infra/security/business/optional）仅作为同入度节点的 tie-breaker，**不**是排序键。

**核心理念（fire-and-forget）**：`--auto` 模式下主 session 不向用户问决策；原本 `needs-user-decision` 的所有触发点改为调 `npc auto-decide --apply`，由 npc 基于 progress 数据直接给 action（continue-retry / skip / force-archive / abort）。只有主 session 自己也判断不了（npc 子命令 exit_code=3 等环境性失败）才停下问用户。

**v2 vs v1**：v1 用 bash + jq 散落实现状态/事件/模板渲染（1280 行）；v2 把这些工程化逻辑统一收敛到 `npc` CLI（详见 `docs/cli.md`），skill.md 仅保留 LLM 必须决策的部分。

**npc 1.1 新增能力**：

- `npc init` 输出 `state_drift` 字段：续跑时识别 task_log 引用的 commit 已不在 HEAD 链上的漂移
- `npc state repair --auto`：把漂移的 progress 项重置为 pending，旧 base 进 `.repaired/` 留存，openspec archive 同步回 active
- `npc phase rotate --seq N --to <phase>`：原子完成"上一 phase exit + 新 phase enter"，避免 fix loop 漏调 phase enter 导致的 started_at=null
- `npc agent timeout-budget` / `record-timeout`：Agent 调用渐进退避（base=1800s，×1.2，上限 3600s，retries≥5 视为耗尽）
- `npc auto-decide --seq N --trigger <kind> [--apply]`：把所有 `--auto` 决策点封装成单一 CLI，返回 `{action, set_status, reason}`，可选 `--apply` 直接写状态
- `npc focus render` round≥1 自动注入 `## Already-Fixed History` 段，避免 Codex 跨轮重报已修问题

主 session 单次 sub-agent 调用注意力开销：~150 tokens（spawn-prompt 引导语）+ ~250 tokens（render / record / timeout-budget JSON）≈ **400 tokens**。

**前置条件**
- 已安装 `npc` CLI：在 agent-spine 仓库根执行 `uv tool install --force --from . npc`，`npc --version` 输出 **1.4.0+**
- SessionStart hook 已装；未装时 npc 退化为 mtime 启发，不阻塞
- 在 git 仓库内运行；`openspec` 工具可用

---

### 参数

- `--auto` — 全自动模式：任何决策点都走 `npc auto-decide --apply`，不向用户询问；只在 npc 报 exit_code=3（环境失败）时才停
- `--fresh` — 忽略任何 in-progress 旧 run，新建

`--auto` 下唯一会停下的情况：

- `npc <cmd>` 返回 exit_code=3（git 不在 / state.json 不可读 / 依赖缺失等环境故障）
- `npc <cmd>` 返回 exit_code=4（codex / openspec 二进制找不到）
- `npc auto-decide` 自己返回 `action=abort`（极少；唯一来源是主 session 用 trigger 表达"我不知道怎么办"）

---

### 主体流程

**Step 1. 解析参数**

```bash
AUTO=false; FRESH=false
for a in "$@"; do
  case "$a" in --auto) AUTO=true;; --fresh) FRESH=true;; esac
done
```

**Step 2. 初始化运行环境**

```bash
INIT_ARGS=""
[ "$AUTO" = "true" ] && INIT_ARGS="$INIT_ARGS --auto"
[ "$FRESH" = "true" ] && INIT_ARGS="$INIT_ARGS --fresh"
INIT_JSON=$(npc init $INIT_ARGS)
echo "$INIT_JSON" | jq .
```

主 session 从 `$INIT_JSON` 解析并**记忆两个字面值**，用于后续 `codex exec`（npc review run 已内置，此处通常不需要）：

- `REPO_ROOT = .repo_root`
- `SCHEMA_PATH = .schema_path`

其它派生路径不需要持有——所有 `npc <subcmd>` 已通过 `<task_log_dir>/active.json` + `<run_dir>/run.json` 自包含 resolve。

**Step 3. 续跑判定 + 自愈漂移**

```bash
NEEDS_RESUME=$(echo "$INIT_JSON" | jq -r '.needs_resume')
DRIFTED=$(echo "$INIT_JSON" | jq -r '.state_drift.total_drifted // 0')

if [ "$NEEDS_RESUME" = "true" ] && [ "$DRIFTED" != "0" ]; then
  # task_log 引用的 commit 已不在 git（用户 reset / rebase）→ 自愈
  npc state repair --auto
fi

if [ "$NEEDS_RESUME" = "true" ]; then
  RESUME=$(npc resume detect)
  NEXT_SEQ=$(echo "$RESUME" | jq -r '.next_seq')
  NEXT_PHASE=$(echo "$RESUME" | jq -r '.next_phase')
fi
```

- `$AUTO` 且 `$NEEDS_RESUME=true` → 自动续跑（repair 已隐式处理漂移）
- 交互模式：呈现 `RESUME` 摘要 + `state_drift` 是否触发了 repair
- `$NEEDS_RESUME=false` → 正常进入 Step 4

**Step 4-7. plan 模式制定计划（DAG 拓扑排序，严禁字符序）**

调用 `EnterPlanMode`。主 session **必须**用以下 5 阶段 DAG 算法决定 `plan_order`；**严禁**把 `openspec list` 的默认输出（按 mtime / name）直接当成 plan_order，也**严禁**仅凭"基础设施 → 安全 → 业务 → 可选"四层 tier 做线性排序——tier 仅作为同层 tie-breaker，不是排序键本身。

#### 4.1 列出所有活跃 changes（节点集 N）

```bash
NODES=$(openspec list --json 2>/dev/null | jq -r '.[].id' || openspec list | awk '/^[a-z]/{print $1}')
```

#### 4.2 抽取每个 change 的依赖元数据

对每个 `CID ∈ NODES`，并行调用 `openspec show "$CID" --json --deltas-only --no-interactive` 取 spec delta；同时 Read `openspec/changes/<CID>/proposal.md`、`openspec/changes/<CID>/tasks.md`。把每个节点结构化为 4 元组：

| 字段 | 来源 | 用途 |
|---|---|---|
| `tier ∈ {infra, security, business, optional}` | proposal.md 的 *Why* / *What Changes* 段语义判定<br/>关键词速查：foundation / scaffold / migration / bootstrap / 基础设施 → **infra**；auth / permission / audit / token / 安全 → **security**；optimization / refactor / cleanup / 优化 → **optional**；其余 → **business** | 同层 tie-breaker（小者先）|
| `deltas: {spec → ADD\|UPDATE\|REMOVE}` | `openspec show --deltas-only` JSON | R1 连边 |
| `refs: [CID]` | proposal.md / tasks.md 全文 regex 抽 `\b(add\|update\|remove)-[a-z0-9-]+\b`，**保留仅出现在 active NODES 中的，去掉自身与已归档** | R2 连边 |
| `scope_size` | tasks.md 中 `- [ ]` / `- [x]` 行数 | tie-breaker（小者先，先把小盘子打通）|

#### 4.3 构造 DAG 边集 E（A → B 表示 A 必须先于 B）

只用以下三条硬规则连边；软偏好不构成边：

| 规则 | A → B 的条件 |
|---|---|
| **R1（spec 创建依赖）** | 存在 spec X：A.deltas[X] = ADD ∧ B.deltas[X] ∈ {UPDATE, REMOVE} |
| **R2（显式引用）** | A ∈ B.refs ∧ 上下文为 "depends on" / "requires" / "after" / "based on" / "基于" / "前置"（粗略可仅按 refs 命中即连，注意 R3 与 R1 重复时去重）|
| **R3（文件创建依赖）** | A 的 proposal.md *Impact → Affected Code* 列出的某路径 P：`git ls-files -- P` 在 HEAD 不存在 ∧ 同一 P 出现在 B 的 *Affected Code* 中 |

#### 4.4 Kahn 拓扑排序 + tie-breaker

```python
# 伪代码（主 session 内心执行，无需真跑）
indeg = {n: 0 for n in N}
adj   = {n: [] for n in N}
for (u, v) in E: adj[u].append(v); indeg[v] += 1

TIER_W = {"infra": 0, "security": 1, "business": 2, "optional": 3}
def key(n): return (TIER_W[tier[n]], scope_size[n], n)   # tier → scope → id 字典序

plan_order = []
ready = sorted([n for n in N if indeg[n] == 0], key=key)
while ready:
    n = ready.pop(0)
    plan_order.append(n)
    for m in adj[n]:
        indeg[m] -= 1
        if indeg[m] == 0:
            ready.append(m); ready.sort(key=key)

if len(plan_order) != len(N):
    cycle_nodes = [n for n in N if indeg[n] > 0]
    # 环处理：见 4.5
```

#### 4.5 环（cycle）处理

若 `plan_order` 长度 ≠ `|N|`，存在循环依赖：

- **交互模式**：列出环上节点 + 触发规则，停下等用户裁决（用户可能要求拆 change / 调整 proposal）
- **`--auto` 模式**：按 `key(n)` 选环内最小节点删入边、强行插入 `plan_order`，向 `<run_dir>/run.events.jsonl` 写一条 `{"type": "plan.cycle_break", "nodes": [...], "broken_edge": [u, v]}` 事件，继续

#### 4.6 输出 DAG 计划摘要（plan 模式期间必须打印）

供用户审计 + 后续 run.events.jsonl 留痕，机器可解析：

```
DAG Plan Summary
================
Nodes (N=<count>):
  - <CID>  tier=<t>  scope=<n>  deltas=[X:ADD, Y:UPDATE]
  ...
Edges (E=<count>):
  - <A> -> <B>  rule=R1  via_spec=<X>
  - <A> -> <B>  rule=R2  via_ref="depends on <A>"
  - <A> -> <B>  rule=R3  via_file=<path>
Plan Order (topological):
  1. <CID>   level=0  reason=<entry, tier=infra>
  2. <CID>   level=0  reason=<entry, tier=security>
  3. <CID>   level=1  reason=<depends on #1 via R1>
  ...
Cycles: <none | [...nodes...]>
```

#### 4.7 落地 plan

```bash
PLAN_JSON=$(printf '%s\n' "${PLAN_ORDER[@]}" | jq -R . | jq -sc .)   # bash 数组 → JSON 数组
npc state init-run --plan-order "$PLAN_JSON"
```

调用 `ExitPlanMode`；`$AUTO` 模式不等用户 approve，立即进入 Step 9 实施循环。

**Step 9. 实施循环**

按 `plan_order` 严格串行。下面是单 change 的完整流程：

```
FOR SEQ in 1..TOTAL:
  CID=$(npc state get ".plan_order[$((SEQ-1))]" | tr -d '"')
  npc state add-change "$SEQ" "$CID"
  BASE=$(npc state get ".progress[$((SEQ-1))].base" | tr -d '"')

  # === 10.1 Implement ===
  npc phase rotate --seq "$SEQ" --to implement      # ★ v1.1：原子换 phase

  # 取 Agent 调用 timeout 预算（渐进退避）
  BUDGET=$(npc agent timeout-budget --seq "$SEQ" --phase implement)
  TIMEOUT=$(echo "$BUDGET" | jq -r '.timeout_sec')
  EXHAUSTED=$(echo "$BUDGET" | jq -r '.exhausted')

  if [ "$EXHAUSTED" = "true" ]; then
    # timeout 退避耗尽：Implementer 无法在 3600s 内完成 → change 拆得太大
    DECISION=$(npc auto-decide --seq "$SEQ" --trigger agent-timeout-exhausted --apply)
    continue  # 主 session 直接跳过这个 change
  fi

  # 渲染 prompt + 拿薄引导语
  npc agent prompt render --phase implement --change-id "$CID"
  SPAWN=$(npc agent spawn-prompt --phase implement --change-id "$CID")
  PROMPT_TEXT=$(echo "$SPAWN" | jq -r '.prompt')

  # 调 Agent(senior-code-developer, prompt=$PROMPT_TEXT)；记录 start ts
  AGENT_START_TS=$(date +%s)
  # ... Agent 返回 ...
  AGENT_ELAPSED=$(($(date +%s) - AGENT_START_TS))

  if [ "$AGENT_ELAPSED" -ge "$TIMEOUT" ]; then
    npc agent record-timeout --seq "$SEQ" --phase implement
    DECISION=$(npc auto-decide --seq "$SEQ" --trigger implementer-failed --apply)
    ACTION=$(echo "$DECISION" | jq -r '.action')
    [ "$ACTION" = "continue-retry" ] && continue-implement-this-seq
    continue  # skip
  fi

  # 解析 RESULT 行 + 校验 summary + git 验 commit（全部下沉到 npc implement record）
  REC=$(npc implement record --seq "$SEQ" --result "<RESULT 行>")
  OK=$(echo "$REC" | jq -r '.ok')
  if [ "$OK" != "true" ]; then
    DECISION=$(npc auto-decide --seq "$SEQ" --trigger implementer-failed --apply)
    ACTION=$(echo "$DECISION" | jq -r '.action')
    [ "$ACTION" = "continue-retry" ] && continue-implement-this-seq
    continue
  fi

  # === 10.2 Round 0 Codex Review ===
  REVIEW=$(npc review run --seq "$SEQ" --round 0)
  OK=$(echo "$REVIEW" | jq -r '.ok')
  if [ "$OK" != "true" ]; then
    # codex exec 失败（npc 内部已重试 1 次）→ 立刻 skip（不再问用户）
    DECISION=$(npc auto-decide --seq "$SEQ" --trigger codex-failed --apply)
    continue
  fi
  BLOCKING=$(echo "$REVIEW" | jq -r '.blocking')
  STALE=$(echo "$REVIEW" | jq -r '.stale')

  # === 10.3 分级判定 ===
  if [ "$BLOCKING" = "0" ]; then
    GOTO_ARCHIVE
  fi

  # === 10.4 Fix Loop（最多 MAX_FIX_ROUNDS=20） ===
  for N in $(seq 1 20); do
    npc phase rotate --seq "$SEQ" --to "fix-r$N"   # ★ v1.1：原子换 phase

    # 同样的 timeout 退避 + Agent 调用模式
    BUDGET=$(npc agent timeout-budget --seq "$SEQ" --phase "fix-r$N")
    TIMEOUT=$(echo "$BUDGET" | jq -r '.timeout_sec')
    EXHAUSTED=$(echo "$BUDGET" | jq -r '.exhausted')
    if [ "$EXHAUSTED" = "true" ]; then
      DECISION=$(npc auto-decide --seq "$SEQ" --trigger agent-timeout-exhausted --apply)
      break
    fi

    npc agent prompt render --phase fix --change-id "$CID" --round $N
    SPAWN=$(npc agent spawn-prompt --phase fix --change-id "$CID" --round $N)
    PROMPT_TEXT=$(echo "$SPAWN" | jq -r '.prompt')
    # 调 Agent(senior-code-developer)；超时检测同上；失败时 npc auto-decide --trigger fixer-failed

    REC=$(npc fix record --seq "$SEQ" --round $N --result "<RESULT 行>")
    OK=$(echo "$REC" | jq -r '.ok')
    if [ "$OK" != "true" ]; then
      DECISION=$(npc auto-decide --seq "$SEQ" --trigger fixer-failed --apply)
      ACTION=$(echo "$DECISION" | jq -r '.action')
      [ "$ACTION" = "continue-retry" ] && continue-this-round
      break
    fi

    # re-review
    REVIEW=$(npc review run --seq "$SEQ" --round $N)
    OK=$(echo "$REVIEW" | jq -r '.ok')
    if [ "$OK" != "true" ]; then
      DECISION=$(npc auto-decide --seq "$SEQ" --trigger codex-failed --apply)
      break
    fi
    BLOCKING=$(echo "$REVIEW" | jq -r '.blocking')
    STALE=$(echo "$REVIEW" | jq -r '.stale')

    if [ "$BLOCKING" = "0" ]; then break; fi

    if [ "$STALE" = "true" ]; then
      DECISION=$(npc auto-decide --seq "$SEQ" --trigger stale --apply)
      ACTION=$(echo "$DECISION" | jq -r '.action')
      [ "$ACTION" = "force-archive" ] && break-to-archive  # 跳到 10.5
      break  # skip / abort 都跳出 fix loop
    fi

    if [ "$N" = "20" ]; then
      DECISION=$(npc auto-decide --seq "$SEQ" --trigger max-rounds --apply)
      ACTION=$(echo "$DECISION" | jq -r '.action')
      [ "$ACTION" = "force-archive" ] && break-to-archive
      break
    fi
  done

  CURR_STATUS=$(npc state get ".progress[$((SEQ-1))].status" | tr -d '"')
  [ "$CURR_STATUS" = "skipped-auto" ] && continue
  [ "$BLOCKING" != "0" ] && [ "$CURR_STATUS" != "in-fix-loop" ] && continue

  # === 10.5 Archive ===
  ARC=$(npc archive run --seq "$SEQ")
  OK=$(echo "$ARC" | jq -r '.ok')
  if [ "$OK" != "true" ]; then
    # archive 失败一律 skip，不再问用户（precheck / validate / archive / commit 失败）
    REASON=$(echo "$ARC" | jq -r '.error')
    npc state set-progress "$SEQ" --status failed --reason "$REASON"
    continue
  fi
done
```

**Step 11. 收尾**

```bash
npc state finalize       # 判定 completed / completed-with-issues
npc summary render       # 写 <run_dir>/run-summary.md
npc index append         # 追加 <task_log_dir>/index.jsonl
```

---

### §A / §B Sub-agent Prompt（1.0 起内置于 npc）

模板内置于 `npc.templates` 包资源。本 skill 不再嵌入模板原文；如需审计 / 修改契约，看 agent-spine 仓库根的 [`src/npc/templates.py`](../../src/npc/templates.py) 与 [`docs/cli.md`](../../docs/cli.md) §7a 段。

**契约速记**（sub-agent 必须遵守，npc 模板已强制；本节只供审计）：

| Phase | Sub-agent 必产 | RESULT 行 schema |
|---|---|---|
| **Implement** | `$BASE/implement.summary.md`（含 Commit / Tasks Completed / Tests / Files Modified / Key Decisions / Issues Encountered / Verification 段） | `RESULT: commit=<hash> tasks=<n> tests=<pass\|fail> summary=<path> notes=<...>` |
| **Fix** | `$BASE/round-N.fix.summary.md`（含 Per-Finding Resolution / Locations Scanned / Real Regressions / Self-Check / Side Effects 段） | `RESULT: commit=<hash> fixed=<n> tests=<pass\|fail> summary=<path> categories_scanned=<csv> regressions_added=<csv\|-> notes=<...>` |

**Fixer 硬约束**（npc 模板已包含 A-D 四条）：

- A. Root-cause 全落点扫描（出现过的 category 强制全扫，Locations Scanned 自报）
- B. 并发 / 事务 / 锁 / 重试 / 竞态 / 部分失败类 finding 必须真实回归（不可 mock-only）
- C. Self-Check 段自检失败模式（无限重试 / 锁未释放 / 竞态扩大 / partial-failure 静默吞掉）
- D. 只修 blocking + in_scope=true；不做额外重构；commit 格式 `fix(<change>): review round N — <摘要>`

---

### 关键约束（速查）

- **plan_order 必须来自 DAG 拓扑排序**：边集严格按 R1（spec ADD/UPDATE 依赖）/ R2（proposal/tasks 显式引用）/ R3（文件创建依赖）三条硬规则；tier 仅作同层 tie-breaker。严禁 `openspec list` 默认序、字符序、mtime 序直接落 `plan_order`。任何排序决策必须在 plan 模式打印 *DAG Plan Summary* 留痕
- **运行轨迹外置**：所有 state / event / summary / index 落 `~/task_log/<PROJ_KEY>/`；工程目录零侵入
- **task_log append-only**：state.json 的 `repair_log` 数组单向增长；run.events.jsonl / per-change events.jsonl 一律 append；旧 base 在 repair 时 `mv` 到 `.repaired/` 留存
- **`--auto` 全自动**：所有决策点走 `npc auto-decide --apply`；只有 npc exit_code=3/4（环境/依赖缺失）才停下问用户
- **`npc init` 自检漂移**：续跑前 `state_drift` 字段告知 commit 是否还在 HEAD 链上；非空时 `npc state repair --auto` 自愈
- **Agent 调用渐进超时**：默认 base 1800s，超时 `record-timeout` 后下次 ×1.2，上限 3600s，retries≥5 → exhausted（视为 change 拆得太大，skip）
- **Fix loop 统一用 `npc phase rotate`**：原子换 phase，避免漏调 phase enter 导致的 started_at=null 漂移
- **Codex `focus render` round≥1 自动注入 Already-Fixed History**：从 prior round 的 fix.summary.md 抽 Per-Finding Resolution，避免 Codex 跨轮重报已修问题
- **STATE_JSON 与 STATE_MD 同步**：所有 `npc state ...` / `npc phase ...` 都自动同步重写 STATE_MD
- **schema_version=2 only**：v2 不支持续跑旧 schema_version=1 run；旧 run 由 `npc state repair` 自愈或用户手工搬走
- **Implementer / Fixer 一律 `Agent(senior-code-developer)`**：独立 context，RESULT 行 + summary 文件双产物
- **Sub-agent prompt 一律走 `npc agent prompt render` + `npc agent spawn-prompt`**：模板内容不流过主 session，仅 ~150 tokens 引导语
- **Reviewer 一律 `codex exec`**（已封装在 `npc review run`）：stateless 一次性子进程，--output-schema 强约束 JSON 输出
- **每轮 fix 独立 commit**（`fix(<change>): review round N`），archive 前 `npc archive precheck` 校验 commit chain
- **commit message 严禁 AI 署名 trailer**（项目级 hook + CLAUDE.md 兜底，prompt 不重复提醒）
- **禁止 `--no-verify` / 跳过测试 / 跳过签名**

---

### 已知陷阱速查

- **plan_order 退化为字符序 / mtime 序** → LLM 看到模糊指令"分析依赖关系"会偷懒按 `openspec list` 默认顺序落地。v2 修复：plan 模式强制走 4.1–4.7 的 5 阶段 DAG 算法，且必须打印 *DAG Plan Summary*（节点/边/拓扑序/环）作为留痕，无 summary 视为流程违规
- **`claude -p` 子进程在 cc 主 session 中调用必卡死** → 本 skill 完全弃用，用 Agent 调用
- **常驻 codex daemon 跨轮累积运行会假死** → v2 用 stateless `codex exec` 一次性子进程
- **Codex 缺少项目威胁模型时会泛 adversarial 误报** → `npc focus render` 自动从 `openspec/project.md` / `CLAUDE.md` 抽 "评审重点"/"威胁模型" 章节注入；都无时用默认中性约束
- **Fixer 只改点名那一行 → 同类问题反复重报** → npc Fixer 模板 Root-cause 全落点扫描（A 条）+ Locations Scanned 自报段
- **并发类 finding 用 mock-only 反复被驳** → npc Fixer 模板强制真实回归（B 条）+ Real Regressions 自报段
- **Codex 跨轮重报已修问题（换措辞重新标 blocking）** → v1.1：`focus render` round≥1 自动注入 `## Already-Fixed History` 段
- **Fix loop 第二轮起漏调 phase enter → started_at=null** → v1.1：fix loop 一律用 `npc phase rotate`，`npc fix record` 内部兜底
- **Sub-agent 假死无 timeout，整 run 卡住** → v1.1：Agent 调用前用 `npc agent timeout-budget` 取墙钟上限，超时 `record-timeout` 渐进退避，3600s 上限耗尽 → auto-decide skip
- **git reset 后 task_log state 还指着 dangling commit** → v1.1：`npc init` 输出 `state_drift`，主 session 调 `npc state repair --auto` 自愈
- **主 session context 被 prompt 模板淹没**（1.0 前）→ `npc agent prompt render` 把模板下沉到 disk
- **stale 检测在 ±1 震荡的 blocking 上失效** → `npc review check-stale` 用"连续 ≥ 3 轮 blocking 未严格下降"硬规则

---

### 调试与查询

所有路径在 `npc init` 输出（`$INIT_JSON`）里都可取到。

- 跨 run 学习入口：`cat <task_log_dir>/index.jsonl | jq -c 'select(.status=="completed-with-issues")'`
- 性能复盘：`jq -c 'select(.duration_ms)' <run_dir>/run.events.jsonl | sort -t: -k3 -n -r | head -10`
- 当前 run 状态：`npc state get '.progress | map({seq,change_id,status})'`；或 `cat <task_log_dir>/<run_ts>-plan-state.md`
- Phase 时长：`npc state get '.progress[] | {cid:.change_id, phases: (.phases | to_entries | map({key, dur: .value.duration_ms}))}'`
- 当前 active run：`cat <task_log_dir>/active.json`；`<run_dir>/run.json` 含本次所有派生路径
- Agent timeout 计数：`npc state get '.progress[] | {cid:.change_id, retries: [.phases | to_entries[] | {phase:.key, t:.value.timeout_retries}] | map(select(.t))}'`
- Repair 历史：`npc state get '.repair_log'`
- Auto-decide 历史：`npc state get '.progress[] | select(.reason) | {seq, change_id, status, reason}'`
