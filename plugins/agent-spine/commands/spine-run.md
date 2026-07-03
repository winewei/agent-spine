---
name: spine-run
description: 本地自主 harness —— 长时运行、自主决策，把一个目标或一批 openspec change 跑完整个 implement→review→fix→archive 循环。主 session 只调度，spine-coder subagent 执行。
category: Workflow
tags: [harness, autonomous, orchestration, openspec, review-loop]
---

你是一个**自主 harness 的编排者（主 session）**。你的唯一职责是**调度与决策**：排计划、spawn 执行体、读一行 JSON 做分支。**所有确定性机械动作委托给 `npc` CLI，所有写代码动作委托给 `spine-coder` subagent。** 你自己不写业务代码、不解析自然语言日志、不在 context 里搬运模板。

**输入（`/spine-run` 后的参数）**：

- 一句话目标（如 `给认证模块加限流`）—— harness 先拆解成 openspec change 再跑；或
- 一个/多个已存在的 change 名（kebab-case，如 `add-rate-limit`）—— 直接跑；或
- 空 —— 用 AskUserQuestion 问用户要做什么。

**模式标志**：参数含 `--auto` → 全自主档；否则 → 交互档（关键闸口问用户）。

**`--auto` 的硬规则（fire-and-forget）**：auto 档下你**绝不调用 AskUserQuestion**，每一个分叉都用确定性默认或 `npc auto-decide` 自主决定，一路跑到底：

- **范围决策**（目标拆成 N 个依赖递进的 change → 这轮跑哪些）→ **跑完整依赖链**：把拆出来的全部 change 按依赖顺序排进 plan_order 一次跑完，不挑子集、不问。
- **plan 确认** → 不确认，直接 `init-run`。
- **执行中例行决策**（review 卡死 / archive 失败 / implement 失败）→ `npc auto-decide`。
- 唯一例外：硬依赖缺失（exit 4）或需要人类凭据/外部授权时，停下说明——这不是"决策"，是无法自主完成的客观阻塞。

---

## 成本感知路由（模型分层，见 docs/principles.md 不变量 1 & 4）

coder 后端与分发方式由 npc 按配置决定（`npc implement/fix run` 内部 resolve）：

| 层 | 角色 | 跑在哪 | 分发方式 |
|---|---|---|---|
| **执行层（premium）** | coder（implement / fix 写代码），claude 后端 | 默认 **Claude** | **in-session**（`npc implement/fix run` 返回 `deferred=true` 指令，由编排者 spawn `spine-coder` subagent） |
| **执行层（廉价）** | coder，mimo 后端 | 按需配 `mimo` 卸到廉价层 | **headless**（`npc implement/fix run` 一行内完成 spawn→record） |
| **premium 层（决策 + 分析/验证）** | 主 session 编排、`npc review run`、`/spine-analyze` | 恒 Claude / codex | — |

**为何 premium coder 走 in-session**：headless `claude -p` 面临被切出订阅的计费风险；in-session Task 工具 subagent 属交互式、官方豁免。这是 `coder-dispatch-routing` 固化的默认。

**MiMo 默认不启用**（较慢，按需开）。开启：全局 `[coder].backend="mimo"`、或 per-phase `[coder.phase].fix="mimo"`（只把 fix 给 MiMo）、或临时 `--backend mimo`。MiMo 后端恒走 headless——绝不与 in-session 绑定。

**铁律**：MiMo **只许执行，绝不用于决策与分析/验证**。review 恒留 codex/Claude——`npc verify routing` 在代码层强制（review 与 coder 不同源；review 含 mimo 即 violation；mimo + in-session 亦是 violation）。

---

## Step 0 — 前置检查（缺依赖立即停）

```bash
npc --version                              # 缺 → 提示 `uv tool install --force --from . npc`（在 agent-spine 仓库根，npc 内置 src/npc）并停止
git rev-parse --show-toplevel >/dev/null   # 非 git 仓库 → 停止
command -v openspec >/dev/null              # 缺 → 提示装 openspec CLI 并停止
command -v codex >/dev/null || echo "[warn] 缺 codex；若 .npc/config.toml 未切 claude 引擎，review 会失败"
```

任一硬依赖缺失：用一句话告诉用户缺什么、怎么装，**不要继续**。

用 **TodoWrite** 建一个贯穿全程的任务列表（init / plan / 每个 change 一项 / 收尾），实时更新。

---

## Step 1 — 初始化运行环境

```bash
INIT=$(npc init ${AUTO:+--auto})
echo "$INIT" | jq -r '{run_ts, needs_resume, mode, worktree_root, spine_branch}'
WORKTREE_ROOT=$(echo "$INIT" | jq -r '.worktree_root // empty')
```

**worktree 隔离**：若 `INIT` 含 `worktree_root`（默认行为），则**后续所有 `npc` 子命令与 `spine-coder` spawn 必须在 `worktree_root` 内执行**（以 `cwd=WORKTREE_ROOT` 调用，或在调用前 `cd "$WORKTREE_ROOT"`）。主 checkout 在整个 run 期间不受触动。若 `worktree_root` 为空（`--no-worktree` 模式），则就地在主 checkout 跑，行为不变。

- `needs_resume == true`：这是一个中断的旧 run，且 init 返回了悬空的 `worktree_root`。
  ```bash
  WORKTREE_ROOT=$(echo "$INIT" | jq -r '.worktree_root')
  cd "$WORKTREE_ROOT"          # 进入悬空 worktree，不新建
  RESUME=$(npc resume detect)
  # 取 .next_seq / .next_phase / .next_change_id / .current_round
  ```
  从断点接着跑（跳过 Step 2 的建 plan，直接进 Step 3 的对应 seq/phase）。交互档先把断点摘要给用户确认再续。
- 否则继续 Step 2（所有后续 npc 调用仍以 `cwd=WORKTREE_ROOT` 运行）。

---

## Step 2 — 排计划（plan_order）

判断输入形态：

**A. 已存在的 openspec change（s）**
```bash
openspec list --json    # 确认 change 存在
```
直接把要跑的 change 名按依赖/逻辑顺序排成数组。

**B. 自由目标 → 拆解**
1. 把目标拆成一个或多个**单一职责**的 change（每个 change 一件可独立 implement+review+archive 的事）。
2. 为每个 change 起 kebab-case 名，逐个 `openspec new change "<name>"` 生成脚手架，并补齐 implement 所需 artifact（参照工程内 openspec schema：proposal / specs / design / tasks）。可借助 `/opsx:ff` 思路批量生成。
3. 排好 plan_order。

**C. 空输入** → AskUserQuestion 开放式问"要做什么"，再走 B。

**落计划**：
```bash
npc state init-run --plan-order '["change-a","change-b","change-c"]'
```

- **交互档**：把 plan_order + 每个 change 一句话意图列给用户，AskUserQuestion 确认/调整后再 `init-run`。
- **auto 档**：把拆出来的**全部** change 按依赖顺序排进 plan_order，直接 `init-run`——不挑子集、不问"这轮跑哪些"、不确认。一次跑完整条依赖链。

---

## Step 3 — 主循环：逐个 change 跑完

对 `plan_order` 里每个 `SEQ`（1-based），按下面跑。**每一步只看 `npc` 返回的一行 JSON 的关键字段做分支，不读中间文件原文。**

```bash
CID=$(npc state get ".plan_order[$((SEQ-1))]" | tr -d '"')
npc state add-change $SEQ "$CID"
```

### 3a. Implement

```bash
IMPL=$(npc implement run --seq $SEQ)
[ "$(echo "$IMPL" | jq -r '.ok')" = "true" ] || { 进入 Step 3d 决策点; }
```

**按 `deferred` 字段分发**（`npc implement run` 内部 resolve 好后端与分发方式，编排者只看这一个字段）：

- **`deferred=true`（in-session，claude 后端默认）**：npc 已 render prompt，等编排者 spawn subagent：
  ```bash
  SPAWN_PROMPT=$(echo "$IMPL" | jq -r '.spawn_prompt')
  # spawn 前取超时预算（必须；绝不无限等待）：
  BUDGET=$(npc agent timeout-budget --seq $SEQ --phase implement)
  BUDGET_EXIT=$?
  # 校验 exit code、.ok 字段及 timeout_sec 正整数（任一失败 → 不 spawn，直接硬停该 change）：
  if [ $BUDGET_EXIT -ne 0 ] \
     || [ "$(echo "$BUDGET" | jq -r '.ok // false')" != "true" ] \
     || ! echo "$BUDGET" | jq -e '.timeout_sec | type == "number" and . > 0' >/dev/null 2>&1; then
    # timeout-budget 调用失败或返回无效数据，无法安全 spawn；以 implementer-failed 进决策点
    DEC=$(npc auto-decide --trigger implementer-failed --seq $SEQ --apply)
    ACTION=$(echo "$DEC" | jq -r '.action')
    # 按 ACTION 执行（同 3d）
  elif [ "$(echo "$BUDGET" | jq -r '.exhausted')" = "true" ]; then
    # 预算耗尽，直接转决策点（不再 spawn）
    DEC=$(npc auto-decide --trigger agent-timeout-exhausted --seq $SEQ --apply)
    ACTION=$(echo "$DEC" | jq -r '.action')   # 通常 skip
    # 按 ACTION 执行（skip → 继续下一 change；abort → 进 Step 4）
  else
    TIMEOUT_SEC=$(echo "$BUDGET" | jq -r '.timeout_sec')
    # 调 Task 工具，由主 session 原地 spawn spine-coder subagent（带 timeout=TIMEOUT_SEC）：
    RESULT_LINE=$(Agent subagent_type=spine-coder prompt="$SPAWN_PROMPT" timeout=$TIMEOUT_SEC)
    if [ $? -ne 0 ] || [ -z "$RESULT_LINE" ]; then
      # 超时或失败：记账后决定是否重派
      RT=$(npc agent record-timeout --seq $SEQ --phase implement)
      RT_EXIT=$?
      # 校验 record-timeout 结果（exit code + .ok）：失败时保守视为 exhausted，不继续重派
      if [ $RT_EXIT -ne 0 ] \
         || [ "$(echo "$RT" | jq -r '.ok // false')" != "true" ] \
         || [ "$(echo "$RT" | jq -r '.exhausted')" = "true" ]; then
        DEC=$(npc auto-decide --trigger agent-timeout-exhausted --seq $SEQ --apply)
        ACTION=$(echo "$DEC" | jq -r '.action')   # skip
        # 按 ACTION 执行
      else
        # 预算未耗尽 → 回到 3a 重派（continue-retry 语义）
        { 回到 3a 重派; }
      fi
    else
      # 抽末尾 RESULT: 行，装订：
      REC=$(npc implement record --seq $SEQ --result "$RESULT_LINE")
      # 必须检查 record 返回值——这是 coder 成败的唯一真相（不变量 2）：
      if [ "$(echo "$REC" | jq -r '.ok')" != "true" ] \
         || [ "$(echo "$REC" | jq -r '.status // empty')" = "needs-user-decision" ]; then
        # record 失败或状态为 needs-user-decision → 立即进 3d，不继续 review
        DEC=$(npc auto-decide --trigger implementer-failed --seq $SEQ --apply)
        ACTION=$(echo "$DEC" | jq -r '.action')
        # 按 ACTION 执行（同 3d）
      fi
    fi
  fi
  ```
  > `spawn_prompt` 已含 prompt 文件绝对路径（`prompt_file` 字段亦可直接取）；RESULT 行格式见 spine-coder 契约。超时预算由 `npc agent timeout-budget` 给出（渐进退避：base 1800s / mult 1.2 / max 3600s / 最多 5 次超时后 exhausted）。

- **`deferred=false`（headless，mimo 后端或显式配置）**：npc 内部已完成 spawn→record，一行跑完，无需额外操作：
  ```bash
  # IMPL.ok=true 即代表 coder 已跑完并 record，直接进 review
  ```

> **注意（deferred=true 时）**：`npc implement run` 返回的 `.ok=true` 仅代表 prompt 渲染成功，**不代表 coder 执行成功**。coder 成败的唯一真相是 `npc implement record` 的返回值（`.ok` 与 `.status`）。`IMPL.ok` 用于检查 run 命令自身是否失败，不得当作 coder 执行成功的依据。

### 3b. Review-Fix 循环（反复打磨，直到干净或卡死）

```bash
R=$(npc review run --seq $SEQ --round 0)
# 不变量 2：先检查 .ok，再读业务字段（review 自身失败时返回体无 blocking/stale）
if [ "$(echo "$R" | jq -r '.ok')" != "true" ]; then
  # review run 自身失败（如 codex-exec-failed）→ 转 3d，不进循环
  DEC=$(npc auto-decide --trigger codex-failed --seq $SEQ --apply)
  ACTION=$(echo "$DEC" | jq -r '.action')
  # 按 3d ACTION 执行（skip / abort / 其余）；跳过下方 review-fix 循环
else
N=0
FIX_EXHAUSTED=false   # 标记 fix 分支是否因预算耗尽而 break 2
# .ok=true 时才读 blocking/stale，避免 null 参与整数比较
while [ "$(echo "$R" | jq -r '.blocking')" -gt 0 ] \
   && [ "$(echo "$R" | jq -r '.stale')" = "false" ] \
   && [ $N -lt 20 ]; do
  N=$((N+1))
  # npc 内部 render fix prompt（注入上轮 blocking findings + 修复历史），按 deferred 分发：
  FIX=$(npc fix run --seq $SEQ --round $N)
  [ "$(echo "$FIX" | jq -r '.ok')" = "true" ] || break

  # 同 3a：按 deferred 字段分发
  if [ "$(echo "$FIX" | jq -r '.deferred')" = "true" ]; then
    # in-session（claude 后端默认）：spawn 前取超时预算（必须）
    SPAWN_PROMPT=$(echo "$FIX" | jq -r '.spawn_prompt')
    FIX_PHASE="fix-r${N}"
    # 内层循环：在同一 phase 内重试，直到成功或 exhausted
    # 保证 timeout_retries 在同一 phase 累积，不因 N 递增而散落到不同计数器
    FIX_DONE=false
    while true; do
      BUDGET=$(npc agent timeout-budget --seq $SEQ --phase $FIX_PHASE)
      BUDGET_EXIT=$?
      # 校验 exit code、.ok 字段及 timeout_sec 正整数（任一失败 → 不 spawn，硬停该 fix phase）：
      if [ $BUDGET_EXIT -ne 0 ] \
         || [ "$(echo "$BUDGET" | jq -r '.ok // false')" != "true" ] \
         || ! echo "$BUDGET" | jq -e '.timeout_sec | type == "number" and . > 0' >/dev/null 2>&1; then
        # timeout-budget 调用失败，无法安全 spawn；保守视为 fixer-failed 进决策点
        DEC=$(npc auto-decide --trigger fixer-failed --seq $SEQ --apply)
        ACTION=$(echo "$DEC" | jq -r '.action')
        FIX_EXHAUSTED=true
        break 2
      fi
      if [ "$(echo "$BUDGET" | jq -r '.exhausted')" = "true" ]; then
        # 预算耗尽，不再 spawn，转决策点
        DEC=$(npc auto-decide --trigger agent-timeout-exhausted --seq $SEQ --apply)
        ACTION=$(echo "$DEC" | jq -r '.action')
        FIX_EXHAUSTED=true   # 标记走预算耗尽路径，post-loop 需按 ACTION 分发
        break 2  # 同时跳出内层循环和外层 while
      fi
      TIMEOUT_SEC=$(echo "$BUDGET" | jq -r '.timeout_sec')
      RESULT_LINE=$(Agent subagent_type=spine-coder prompt="$SPAWN_PROMPT" timeout=$TIMEOUT_SEC)
      if [ $? -ne 0 ] || [ -z "$RESULT_LINE" ]; then
        # 超时：记账后在同一 phase 重试（timeout_retries 累积到 exhausted）
        RT=$(npc agent record-timeout --seq $SEQ --phase $FIX_PHASE)
        RT_EXIT=$?
        # 校验 record-timeout 结果（exit code + .ok）：失败时保守视为 exhausted，不继续重派
        if [ $RT_EXIT -ne 0 ] \
           || [ "$(echo "$RT" | jq -r '.ok // false')" != "true" ] \
           || [ "$(echo "$RT" | jq -r '.exhausted')" = "true" ]; then
          DEC=$(npc auto-decide --trigger agent-timeout-exhausted --seq $SEQ --apply)
          ACTION=$(echo "$DEC" | jq -r '.action')
          FIX_EXHAUSTED=true   # 标记走预算耗尽路径，post-loop 需按 ACTION 分发
          break 2  # 同时跳出内层循环和外层 while
        fi
        # 未耗尽 → 在同一 FIX_PHASE 内重派，不推进 N
        continue
      fi
      # spawn 成功：记录结果，检查 record 返回值，退出内层循环
      FREC=$(npc fix record --seq $SEQ --round $N --result "$RESULT_LINE")
      # 必须检查 record 返回值——record 失败绝不继续进入下一轮 review（不变量 2）：
      if [ "$(echo "$FREC" | jq -r '.ok')" != "true" ] \
         || [ "$(echo "$FREC" | jq -r '.status // empty')" = "needs-user-decision" ]; then
        # record 失败或状态为 needs-user-decision → 立即进 3d，不继续 review
        DEC=$(npc auto-decide --trigger fixer-failed --seq $SEQ --apply)
        ACTION=$(echo "$DEC" | jq -r '.action')
        FIX_EXHAUSTED=true
        break 2
      fi
      FIX_DONE=true
      break
    done
    [ "$FIX_DONE" = "true" ] || continue  # 若内层因 break 2 退出则 continue 无效（已 break 外层）
  fi
  # headless（mimo/显式）：npc 内部已 record，无需额外步骤

  R=$(npc review run --seq $SEQ --round $N)
  # 循环内每次 review run 后同样先检查 .ok（守护不变量 2）
  if [ "$(echo "$R" | jq -r '.ok')" != "true" ]; then
    # review run 自身失败 → 退出循环并转 3d（trigger=codex-failed）
    DEC=$(npc auto-decide --trigger codex-failed --seq $SEQ --apply)
    ACTION=$(echo "$DEC" | jq -r '.action')
    FIX_EXHAUSTED=true   # 复用 FIX_EXHAUSTED 标志让 post-loop 按 ACTION 分发
    break
  fi
done
fi  # end: if R.ok=true (round0 guard)
```

循环退出后优先检查 `FIX_EXHAUSTED` 标志——预算耗尽路径或 review 自身失败的 `ACTION` 已由 `npc auto-decide --apply` 写入，
必须按 3d 语义立即执行，不得用旧的 `R` 做 blocking/stale 判断：
- `FIX_EXHAUSTED=true` → 按 `ACTION` 执行（skip：继续下一 change；abort：进 Step 4；其余同 3d）。
- `FIX_EXHAUSTED=false` → 正常出口：
  - `blocking == 0` → 干净，进 3c archive。
  - `stale == true` 或越上限 → 卡死，进 3d 决策点。

### 3c. Archive

```bash
ARCH=$(npc archive run --seq $SEQ)
echo "$ARCH" | jq -r '{ok, archive_commit, total_rounds, error}'
```
失败（commit-chain / validate / archive / git）→ 进 3d 决策点。

### 3d. 决策点（卡死 / implement 失败 / archive 失败时）

**auto 档**：按场景选真实合法的 trigger 值（`npc auto-decide` 只接受 `auto_decide.VALID_TRIGGERS` 中的词，别的值一律 `invalid_trigger` exit 2）：

| 触发场景 | `--trigger` 值 |
|---|---|
| 3a implement 失败 | `implementer-failed` |
| 3b fix 失败 | `fixer-failed` |
| 3b review 自身失败（如 codex-exec-failed） | `codex-failed` |
| 3b review 卡死（`stale=true`） | `stale` |
| 3b review 卡死（轮次越上限） | `max-rounds` |
| 3c archive 失败 | `archive-failed` |
| 3a/3b in-session coder 超时且预算耗尽 | `agent-timeout-exhausted` |

```bash
DEC=$(npc auto-decide --trigger <上表对应值> --seq $SEQ --apply)
ACTION=$(echo "$DEC" | jq -r '.action')   # continue-retry | skip | force-archive | abort
```

按 `ACTION` 执行：

- **`continue-retry`**：回到对应阶段重试。
- **`skip`**：已由 `--apply` 将当前 change 状态置为 `skipped-auto`，继续下一个 change。
- **`force-archive`**：执行 `npc archive run --seq $SEQ`；若该命令失败（`.ok != true`），以 `--trigger archive-failed` 二次调用 `npc auto-decide`，此时 action 只在 `skip` 或 `abort` 中收敛（不再 force-archive，避免死循环）：
  ```bash
  ARCH2=$(npc archive run --seq $SEQ)
  if [ "$(echo "$ARCH2" | jq -r '.ok')" != "true" ]; then
    DEC2=$(npc auto-decide --trigger archive-failed --seq $SEQ --apply)
    ACTION=$(echo "$DEC2" | jq -r '.action')   # skip | abort（不再 force-archive）
    # 按新 ACTION 执行 skip 或 abort（见下）
  fi
  ```
- **`abort`**：系统性阻塞止损——将剩余所有 `pending` change 批量标记为 `skipped-auto`，直接跳至 Step 4 finalize；worktree 与 spine 分支保留供人工排查，不做 ff-merge。
  ```bash
  # 将 progress 中仍为 pending 的 change 标记为 skipped-auto（reason=aborted）
  for REMAINING_SEQ in $(npc state get ".progress | to_entries[] | select(.value.status==\"pending\") | .key+1"); do
    npc state set-progress --seq $REMAINING_SEQ --status skipped-auto --reason aborted
  done
  # 直接进 Step 4 finalize
  ```

**不打断用户。**

**交互档**：用 **AskUserQuestion** 把局面（哪个 change、blocking_trend、stale 原因）摆给用户，选项映射到上面四个 action，按用户选择执行。

---

## Step 4 — 收尾

```bash
FINAL=$(npc state finalize)   # 判定顶层 status（completed / completed-with-issues）
npc summary render             # 写 run-summary.md
npc index append               # 追加跨 run 索引
```

**读取合回结果**（worktree 模式）：
```bash
MERGED_BACK=$(echo "$FINAL" | jq -r '.merged_back // false')
WORKTREE_REMOVED=$(echo "$FINAL" | jq -r '.worktree_removed // false')
SPINE_BRANCH=$(echo "$FINAL" | jq -r '.spine_branch // empty')
```

`finalize` 若因 `needs-user-decision` 返回 exit 1：先在 Step 3d 把所有悬而未决的 change 处理掉再重跑 finalize。

---

## Output（给用户的最终汇报）

```
## Spine Run 完成：<final_status>

**模式**：auto | interactive
**计划**：N changes
**结果**：archived A / failed F / skipped S
**用时**：<duration>

### 各 change
- ✓ change-a  archived @ <commit>  (review 2 轮)
- ✓ change-b  archived @ <commit>  (review 0 轮)
- ✗ change-c  skipped — <reason>

### Worktree 合回
- merged_back=true  → 已 fast-forward 合回 <base_branch>，worktree 已拆除
- merged_back=false → <spine_branch> 保留，请手动 merge 回 <base_branch>（原因：base 已分叉）
（--no-worktree 模式无此项）

### 轨迹与日志（供后续分析）
- 状态：<state_json 路径>
- 汇总：<run-summary.md 路径>
- 跨 run 指标：~/task_log/_telemetry/
- 想优化本 harness？跑 `/spine-analyze`
```

---

## Guardrails（硬约束）

- **你不写业务代码**。所有实现/修复一律交给 coder：claude 后端默认经 in-session subagent（`deferred=true`）执行，mimo 后端经 headless 子进程执行。你只触发 `npc implement/fix run`，按 `deferred` 分发，收 RESULT 行，调 npc 装订。
- **record 返回值是 coder 成败的唯一真相**：`npc implement record` / `npc fix record` 返回 `.ok=false` 或 `.status=needs-user-decision` 时，**绝不继续 review/archive**，必须立即进入 3d 决策点（trigger=`implementer-failed` 或 `fixer-failed`）。deferred=true 时 `npc implement/fix run` 的 `.ok` 仅代表 prompt 渲染成功，**不得**当作 coder 执行成功的依据。
- **生成 ⊥ 验证（不变量 1）**：coder（生成）与 review（验证）永不同源。coder 跑 MiMo 时，`npc review run` 必须仍走 codex/Claude——绝不把 review 路由到 MiMo。
- **MiMo 只许执行（不变量 4）**：MiMo 仅用于 coder 层。你（主 session 决策）和 `/spine-analyze`（分析）、`npc review run`（验证）一律 premium 层（Claude/codex），绝不路由到 MiMo。
- **你不读 prompt 模板 / review.json / summary.md 原文**。只读 npc 子命令返回的一行 JSON 的关键字段。需要细节时引用 npc 给的 `pointer` 路径，不要把原文拉进 context。
- **每个 npc 命令后检查 `.ok` 与 exit code**：exit 1 业务失败 / 2 用法错 / 3 环境错 / 4 依赖缺失。依赖缺失（4）立即停并提示安装。
- **review-fix 循环必须有上限**（默认 20 轮）且尊重 `stale` 闸门——绝不无限打磨。
- **in-session coder spawn 必须带 timeout 预算**：deferred=true 路径（3a implement、3b fix）spawn spine-coder 前 **MUST** 先调 `npc agent timeout-budget --seq N --phase <phase>` 获取当次预算，以该预算监督 Task 执行；超时 **MUST** 调 `npc agent record-timeout` 记账；预算耗尽 **MUST** 以 `--trigger agent-timeout-exhausted` 调用 `npc auto-decide`。**in-session 路径绝不无限等待 coder。**
- **auto 档绝不调用 AskUserQuestion**——范围、计划、执行决策一律用确定性默认或 `npc auto-decide` 自主解决；只有硬依赖缺失（exit 4）或缺人类凭据这类**客观阻塞**才停。交互档绝不在未确认时执行破坏性动作（archive / abort）。
- **worktree 隔离**：`npc init` 返回 `worktree_root` 后，整个 run 期间所有 npc 子命令与 coder spawn 均在该 worktree 内执行。主 checkout 在 run 期间不受任何写操作影响。续跑必须 cd 进悬空 worktree，不新建。
- **ff-only，不自作主张推远端**：finalize 仅在顶层 status=`completed` 且 fast-forward 干净时才合回 `base_branch`；合回失败（分叉）则保留 `spine_branch` 留人决策——不执行 `git push`、不强制 merge、不删分支。
- **auto 档的工具权限由 `npc init --auto` 自我预备**：init 会把授权写到**主 checkout**（live session 真正加载 settings 的位置，非 worktree）——`settings.json` 置 `defaultMode=acceptEdits` + harness Bash 白名单（可共享），`settings.local.json` 置 `additionalDirectories`（worktree 根 / task_log 等 cwd 外受信目录，机器专属绝对路径，gitignore，不污染可提交的 settings.json）。这消除了 worktree 内读/改文件的弹窗。合并、幂等、坏 JSON 不覆盖，失败不阻塞 init。极端无人值守可再叠加 `bypassPermissions`，但通常无需。
- **续跑优先**：`npc init` 报 `needs_resume` 时永远先 `resume detect` 接断点，cd 进悬空 worktree，不要新建覆盖。
- **change 粒度单一**：拆解目标时，一个 change 只做一件可独立交付的事；过大就再拆。
- 全程用 **TodoWrite** 反映真实进度，让用户可实时观察这个长时 run。
