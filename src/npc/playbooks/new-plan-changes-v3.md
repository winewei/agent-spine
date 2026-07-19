---
name: new-plan-changes-v3
description: 波次并行推进所有活跃的 openspec changes：先用 DAG + 架构师 sub-agent 把 changes 切成并行波次，每波在独立 git worktree 内并行 implement，再串行 cherry-pick 整合并复用 npc 的 codex review / fix loop / archive。是 new-plan-changes-v2（串行）的并行升级版，复用 npc CLI 的状态/日志/续跑/telemetry。当用户说"并行推进 changes""波次并行实施""new-plan-changes-v3""并行版 plan-changes""用 swarm 方式跑 openspec changes"时触发。
category: OpenSpec
tags: [openspec, plan, implement, parallel, worktree, v3]
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

# New Plan & Implement All Changes (v3 — 波次并行)

把 v2 的串行 pipeline 升级为**波次并行**：同一 DAG 拓扑层级、且文件不相交、且经架构师 sub-agent 确认无语义耦合的 changes，在各自 worktree 内**并行** implement；写完串行 cherry-pick 回主分支，再**逐字复用 v2/npc** 的 review → fix → archive。

> **三条核心原则**
> 1. **只并行 implement**：最贵的环节（senior-code-developer 真写代码）并行掉；review/fix/archive 留在主分支线性链上，npc **零改动**。
> 2. **切分全程 sub-agent**：DAG 抽取（§4.0 `dag-analyst`）+ 语义裁定（§4.9 双架构师）都在 sub-agent 内完成，主 session 不读 N×4 份 proposal/design/tasks/spec_delta、只接 JSON；N 个 change 的上下文压力都摊到 sub-agent 上。安全优先——任一裁定 agent 要求串行即串行。
> 3. **整合即锚定**：worktree commit 串行 cherry-pick 到 main 得新 hash，喂 `npc implement record`，commit chain 始终线性、可续跑。

**与同类 skill 的关系**
- `new-plan-changes-v2`：纯串行，npc pipeline。v3 是其并行超集，**复用其全部 npc 命令**（init/resume/state/agent/review/fix/archive/finalize/telemetry）。本文档只描述差异部分；§10.2–10.5 与 Step 11 行为与 v2 完全一致，需要细节时对照同包分发的 v2 playbook（`npc playbook show new-plan-changes-v2`，或已安装宿主目录下的同名文件）。
- `architect-swarm`：并行 worktree 但无 commit/review/archive/持久日志。v3 借用其 worktree 隔离 + manifest + plan-only 重试机制（1.4 起由 `npc verify manifest` 承担，兼容其 legacy JSON RESULT 格式）。

**前置条件**
- `npc --version` ≥ 1.4.0（在 agent-spine 仓库根执行 `uv tool install --force --from . npc`）。1.4 起 v3 **零自带脚本**：原 waves.py / detect_plan_only.py / verify_manifest.py / notify.py 已下沉为 `npc plan waves` / `npc verify manifest` / `npc notify`（契约见 docs/cli.md §8c）
- `npc doctor` 通过（git 为必需项；codex/openspec 缺失不阻塞、记降级项，见 §0.2）
- 在 git 仓库内；SessionStart hook 已装（未装则 npc 退化 mtime 启发，不阻塞）
- **`worktree.baseRef=head`**（关键，见 §0.3）

---

## 参数

| flag | 默认 | 含义 |
|---|---|---|
| `--auto` | off | 全自动：决策点走 `npc auto-decide --apply`，不问用户；切分分析照常跑（分析非询问） |
| `--fresh` | off | 忽略 in-progress 旧 run，新建 |
| `--max-parallel N` | 不限 | 单波次并发 implementer 上限（harness 本身钳到 ~min(16, cores−2)） |
| `--max-retries N` | 1 | implementer 返回 plan-only 时最多重试次数 |
| `--no-architect` | off | 跳过 §4.9 架构师裁定，直接用机械候选划分（更保守，调试用） |
| `--webhook URL` | `$NPC_V3_WEBHOOK` | 进度推到该 webhook（implementer 完成 / 波次完成 / change 归档 / run 收尾）。空则不推 |
| `--webhook-format` | `raw` | webhook 载荷形状：`raw`（结构化 JSON）/ `slack`（`{text}`）/ `feishu`（`{msg_type,content.text}`） |

**`--auto` 失败处理分两类（比 v2 更激进地不打断）：**

| 类别 | 触发 | 处理 |
|---|---|---|
| **单 change 范围** | `auto-decide` 八种 trigger（implementer-failed / codex-failed / fixer-failed / stale / max-rounds / agent-timeout-exhausted / summary-missing / commit-not-found）、cherry-pick 冲突、openspec validate 失败、`npc` **exit 4**（codex/openspec 二进制缺失） | `npc auto-decide --apply` 或就地 skip → **跳过该 change，继续整个 run**，汇总进收尾报告。**绝不打断。** exit 4 时该 phase 直接降级（缺 codex→跳过 review，缺 openspec→只 commit 不 archive） |
| **编排基底坏掉** | `npc` **exit 3**（不在 git 仓库 / state.json 读写失败 / task_log 不可写）、`auto-decide` 返回 `abort` | 真停——连进度都记不了、skip 都没法 coherent 落账、也没法续跑。这是"没法继续"的客观阻断，不是确认门 |

---

## Step 0 — 前置检查

```bash
AUTO=false; FRESH=false; MAXPAR=""; MAXRETRY=1; NOARCH=false
for a in "$@"; do case "$a" in
  --auto) AUTO=true;; --fresh) FRESH=true;; --no-architect) NOARCH=true;;
  --max-parallel=*) MAXPAR="${a#*=}";; --max-retries=*) MAXRETRY="${a#*=}";;
  --webhook=*) WEBHOOK="${a#*=}";; --webhook-format=*) WHFMT="${a#*=}";;
esac; done
WEBHOOK="${WEBHOOK:-${NPC_WEBHOOK:-$NPC_V3_WEBHOOK}}"; WHFMT="${WHFMT:-raw}"
# 统一 notify 封装：永不打断 run（npc notify 总是 exit 0，1.4+）
notify() { npc notify --url "$WEBHOOK" --format "$WHFMT" "$@"; }
```

### 0.1 git repo + 工作树 clean

```bash
git rev-parse --is-inside-work-tree || { echo "需在 git 仓库内"; exit 3; }
DIRTY=$(git status --short)
```

工作树**必须 clean**（无 unstaged/untracked）——v3 要 cherry-pick 整合，脏工作树会污染。非空则停下提示用户先 commit/stash（`--auto` 也停，这是环境前提不是决策）。

### 0.2 npc 可用 + 环境体检（npc 1.4+）

```bash
npc --version   # 期望 1.4.0+（plan waves / verify manifest / notify 是 1.4 才有）；低版本或命令缺失 → exit 3 真停
DOC=$(npc doctor)           # 单行 JSON {ok, checks[], summary}；required(git) 缺失时 exit 4 → 真停
ROUTE=$(npc verify routing) # 路由不变量：exit 1（violations 非空）→ 真停
```

- `doctor` 报 codex / openspec 缺失：**不真停**，记入降级清单——缺 codex 跳过 review、缺 openspec 只 commit 不 archive（与 §参数表的 exit 4 分类一致），收尾报告标注。
- `verify routing` 挡两条硬规则：coder 与 review 引擎不同源（生成⊥验证）、MiMo 不得承担 review。violation 属配置错误（环境前提），`--auto` 也停，提示用户改 `.npc/config.toml` / `~/.config/npc/config.toml`。

### 0.3 worktree.baseRef 硬校验

并行 worktree **必须从当前本地 HEAD 分叉**（= 上一波整合后的 main），否则后续波次拿不到前序成果。

`npc` 无 config 子命令；直接从 settings.json 读（项目级优先于用户级）：

```bash
BASEREF=$(jq -r '.worktree.baseRef // empty' .claude/settings.json 2>/dev/null)
[ -z "$BASEREF" ] && BASEREF=$(jq -r '.worktree.baseRef // empty' ~/.claude/settings.json 2>/dev/null)
```

- `BASEREF != "head"`（含未设置，默认即 `fresh`）→ **报错退出**，提示用户在 `.claude/settings.json` 或 `~/.claude/settings.json` 设 `"worktree": {"baseRef": "head"}`。**绝不静默退化为 fresh**（会让每波从 origin 分叉，丢失前序波次的 commit）。可建议用户用 `/update-config` skill 改这一项。
- 若 Agent worktree 隔离在本仓库不可用 → 同 architect-swarm，报错退出，不退化为"主工作树跑"。

---

## Step 1–3 — 初始化 / 续跑 / 自愈（逐字复用 v2）

与 v2 Step 2–3 完全一致：

```bash
RUN_T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)          # 供收尾 npc cost --since 用
INIT_ARGS=""; [ "$AUTO" = true ] && INIT_ARGS="$INIT_ARGS --auto"; [ "$FRESH" = true ] && INIT_ARGS="$INIT_ARGS --fresh"
INIT_JSON=$(npc init $INIT_ARGS)
NEEDS_RESUME=$(echo "$INIT_JSON" | jq -r '.needs_resume')
DRIFTED=$(echo "$INIT_JSON" | jq -r '.state_drift.total_drifted // 0')
RUN_TS=$(echo "$INIT_JSON" | jq -r '.run_ts')
[ "$NEEDS_RESUME" = true ] && [ "$DRIFTED" != 0 ] && npc state repair --auto
```

**npc 1.3 `init --auto` 副作用收编（必做）**：`init --auto` 会向 `<repo>/.claude/settings.json` 写 auto_auth 授权（幂等、合并保留既有键）。v3 依赖 clean 工作树做 cherry-pick，因此 init 后立即处置：

```bash
if [ -n "$(git status --short -- .claude/settings.json)" ]; then
  if git ls-files --error-unmatch .claude/settings.json >/dev/null 2>&1; then
    git add .claude/settings.json && git commit -m "chore: npc auto-auth settings"   # tracked 被改 → 收编成 commit
  else
    # untracked：不自动 commit（本机授权文件是否入库由用户定）。--auto 下加入 .git/info/exclude 本地忽略；
    # 交互模式提示用户选 commit 或 gitignore。不处理会让下次 run 的 §0.1 clean 校验失败。
    [ "$AUTO" = true ] && echo ".claude/settings.json" >> .git/info/exclude
  fi
fi
```

**登记 watchable task（1.3+，best-effort）**：让 `npc watch` 能观测本 run，与 webhook 互补。所有 `npc task *` 调用失败不阻塞（`|| true`）：

```bash
npc task start --id "npc-v3-$RUN_TS" --description "new-plan-changes-v3 run" --source npc \
  --phase plan --progress-total "<N>" --progress-unit changes --replace || true
```

- 续跑时 `npc resume detect` 给 `next_seq`/`next_phase`。v3 续跑语义：**未整合（未走到 `implement record`）的 change 一律退回 pending，下次重新并行**（worktree 可能已被 Agent 清理）；已整合并进入 review/fix/archive 的按 v2 断点续。
- `REPO_ROOT=.repo_root`、`SCHEMA_PATH=.schema_path` 记下备用。

---

## Step 4 — 计划：DAG 抽取 (sub-agent) → 候选波次 → 架构师裁定 → 落地

**plan 模式是给人看计划的约定，不是计算前提。`--auto` 下绝不进 plan 模式。**

- **`--auto`（全自动，无人工门）**：跳过 `EnterPlanMode` / `ExitPlanMode`。§4.0 抽取由 sub-agent 完成、§4.1–4.10 inline 编排，*DAG Plan Summary* / *Wave Plan Summary* 作为普通输出打印（仅留痕、不阻塞），§4.11 落地后**直接进入 Step 9**。整个 run 没有任何"按确认"动作——`ExitPlanMode` 本质就是审批门，调用它=请求批准，因此 `--auto` 一律不调。
- **交互模式（无 `--auto`）**：才 `EnterPlanMode`，§4.10 后 `ExitPlanMode` 等用户批准，再进 Step 9。

> 唯一会让 `--auto` 停下的，是 Step 0 的**环境前置硬失败**（非 git 仓库 / 工作树脏 / `worktree.baseRef≠head`）与运行中 `npc` **exit 3**（编排基底坏掉）、`auto-decide` 返回 `abort`。运行中的 `npc` **exit 4** 与一切单 change 失败都**降级跳过、继续**（见 §参数表的失败分类），不打断。

### 4.0 DAG 抽取与候选波次（sub-agent，**默认必经**）

主 session **绝不**直接读 changes 的原 markdown（proposal/design/tasks/spec_delta）。**所有这些都委托给一个只读 sub-agent**，它在自己的上下文里完成 "读 N 份 doc → 抽 nodes/edges/files → 跑 `npc plan waves` → 出候选 JSON"，主 session 只接结果 JSON。这是 N 个 change 时唯一可扩展的做法——线性增长的上下文压力压到 sub-agent，不污染主编排。

**前置**（主 session 自己跑，唯一被允许读 changes 的步骤）：

```bash
# 仅取名字与状态，不读 proposal/design/tasks 任何正文
NODES_JSON=$(openspec list --json | jq -c '[.changes[] | select(.status=="in-progress") | .name]')
# 例：["add-foo","fix-bar",...]，长度=N
```

**Agent — `dag-analyst`（`subagent_type=Explore`，只读分析）**

工具：Read / Grep / Glob / Bash（用来跑 `npc plan waves`）。**禁止写代码**。

prompt 主干（主 session 用 here-doc 注入 NODES/REPO_ROOT/RUN_DIR）：

> 你是并行实施编排的 DAG 抽取者。**只读分析 + 跑一次 `npc plan waves` + 输出严格 JSON**。
>
> 工作目录：`<REPO_ROOT>`。
> 候选 changes 集合：`<NODES_ARRAY>`（来自 `openspec list --json`，仅含 status=in-progress）。
> 输出落盘路径：`<run_dir>/v3-dag-extract.json`。
>
> 对每个 CID：
> 1. **读** `openspec/changes/<CID>/proposal.md`、`design.md`（若有）、`tasks.md`、`specs/<capability>/spec.md`。
> 2. **抽 edges**（v2 三规则）：
>    - **R1 spec 创建**：affected spec 是 `add:<cap>` 且 `openspec/specs/<cap>/spec.md` 不存在 → 后续 modify 该 cap 的 change 依赖此 add。
>    - **R2 显式引用**：proposal/design/tasks 中显式说 "depends on / blocked by / 前置依赖 / requires <other-cid>"。
>    - **R3 文件创建**：`Affected Code` 中标"新增 xxx"的代码路径被别的 change 引用 → 后者依赖前者。
>    - 每条边附 `edge_evidence={from,to,rule,cite}`。
> 3. **抽 files**：从 *Impact → Affected Code* 段取代码路径集合。**关键**：proposal 常写不全，遇到 `app/services/foo/`、`web/src/api/` 这种**目录级**写法，**主动用 Grep/Glob 展开为真实改动的具体文件**（按 proposal 上下文推断该目录里哪些 .py/.ts/.tsx 会被改）；展不开就保留目录级字符串作为保守冲突标识。
> 4. **跑 `npc plan waves`**：拼 `{nodes, edges, files, tie_break}` JSON 经 stdin（或 `--input FILE`）喂 `npc plan waves`，拿到候选 `waves / layers / split_reasons / cycle`（单行 JSON；exit 2 = 输入不合法）。
> 5. **写 `<run_dir>/v3-dag-extract.json`**，严格 schema：
>    ```json
>    {
>      "nodes": ["cid", ...],
>      "edges": [["a","b"], ...],
>      "edge_evidence": [{"from":"a","to":"b","rule":"R1|R2|R3","cite":"openspec/changes/a/proposal.md:42"}],
>      "files": {"cid": ["path", ...]},
>      "tie_break": {"cid": [tier, scope]},
>      "candidate": {"waves":[...], "layers":[...], "split_reasons":[...], "cycle":[...]},
>      "notes": "<一段话：抓取覆盖度、不确定项、目录级展开范围>"
>    }
>    ```
> 6. **不要**回灌主 session 任何长文。**最后输出严格一行** `RESULT: {"phase":"dag-extract","status":"ok","path":"<v3-dag-extract.json 绝对路径>"}`。

主 session 拿到 RESULT 行后做最小校验：

```bash
DAG_JSON=$(echo "<RESULT 行>" | jq -r '.path')
# 1. path 存在 + 可解析
[ -f "$DAG_JSON" ] && jq -e . "$DAG_JSON" >/dev/null || FAIL="path-or-json-invalid"
# 2. nodes 完整（无缺失、无外加）
EXPECTED=$(echo "$NODES_JSON" | jq -S 'sort')
GOT=$(jq -S '.nodes | sort' "$DAG_JSON")
[ "$EXPECTED" = "$GOT" ] || FAIL="nodes-mismatch"
# 3. candidate.waves 展平 = nodes（覆盖完整）
FLAT=$(jq -S '.candidate.waves | add | sort' "$DAG_JSON")
[ "$EXPECTED" = "$FLAT" ] || FAIL="candidate-coverage-broken"
# 4. files 是 dict，空 file list 容许（但记 warning）
jq -e '.files | type == "object"' "$DAG_JSON" >/dev/null || FAIL="files-shape"
```

**失败处理**：

- **首次失败**：重发该 sub-agent 一次，前缀 `STRICT SCHEMA — your previous output failed validation: <FAIL>. Re-emit only the JSON file + RESULT line.`。
- **再次失败**：
  - `--auto`：降级为**主 session 自抽**作为最后兜底，run.events.jsonl 记 `{"type":"v3.dag_extract_fallback","reason":"<FAIL>"}`，run 继续。
  - 交互模式：真停，要求用户介入（与 Step 0 的"环境前置"同级）。

**`--no-architect` 与 §4.0 的关系**：`--no-architect` 只跳过 §4.9 语义裁定；**`--no-architect` 不跳过 §4.0**——§4.0 抽取比主 session 自抽更省 token、更准确，没理由跳。如需调试可以另设 `--no-dag-sub-agent`，但默认上述行为。

### 4.1–4.8 输入契约（即 §4.0 sub-agent 的输出）

§4.0 已经把以下事情做了，主 session 只读 JSON、**不读 markdown**：

- **N（nodes）**：active changes 全集。
- **E（edges）**：v2 三规则（R1 spec 创建 / R2 显式引用 / R3 文件创建）抽出的 DAG 边，附 `edge_evidence`（出错时可快速复核）。
- **plan_order**：由 §4.0 内的 `npc plan waves` 调用拿到拓扑分层（Kahn）+ tie-break 决定顺序；`cycle` 字段非空表示破环点。
- **files**：每个 change 的代码影响集（含目录级展开），用于文件交集着色。
- **candidate.waves**：拓扑分层 × 文件交集贪心着色后的候选并行子波次。
- **candidate.split_reasons**：每次拆分的冲突对与共享文件（审计）。

主 session 绑变量：

```bash
DAG_JSON="<run_dir>/v3-dag-extract.json"
CAND_WAVES=$(jq -c '.candidate.waves' "$DAG_JSON")
CAND_SPLIT=$(jq -c '.candidate.split_reasons' "$DAG_JSON")
CYCLE=$(jq -c '.candidate.cycle' "$DAG_JSON")
```

**必须打印 *DAG Plan Summary***（基于 JSON 字段，无需读 markdown），无 summary 视为流程违规：

```
DAG Plan Summary
================
Nodes(N): <n>   Edges(E): <m>   Layers: <L>   Cycle: <none | [...]>
Files coverage: <count of CIDs with non-empty files> / <N>     dir-expansion: <how many CIDs needed Grep>
Source: v3-dag-extract.json (by dag-analyst sub-agent)  |  evidence count: <len(edge_evidence)>
```

**降级路径**（§4.0 失败后 `--auto` 才会触发）：主 session inline 读 N 份 proposal.md、按 v2 §4.1-4.7 规则抽 nodes/edges/files、拼 JSON 喂 `npc plan waves`。降级时上述 *DAG Plan Summary* 的 `Source:` 改为 `main-session fallback (reason=<FAIL>)`。仅在 `--auto` 下作为兜底，绝不作为首选路径。

### 4.9 架构师裁定（sub-agent，语义层）— 默认开启

§4.0 的候选只看 spec delta / 引用 / 文件，**抓不到语义耦合**（共享状态、跨 change 时序、同一子系统不变量、import 闭包）。**并行 spawn 两个只读分析 agent**，单轮，取更严格并集。这两个 agent 是 `Agent` 工具调用、自主返回，**不是人机询问**，`--auto` 下照常跑、不阻塞 fire-and-forget。

`--no-architect` 时跳过本节，直接用 `$CAND_WAVES` 作最终波次。两 agent 不可用/超时 → 同样退化为 `$CAND_WAVES`，run 日志标 `architect_analysis=skipped`。

**Agent A — `senior-system-architect`**（架构/语义耦合）

> 你是并行实施编排的架构裁定者，**只读分析，不写代码**。
> 工作目录：`<REPO_ROOT>`。下面是一批待实施的 OpenSpec changes 的*候选并行波次划分*（来自 §4.0 `dag-analyst` sub-agent 的输出 `v3-dag-extract.json`），请从架构语义层裁定它是否安全。
>
> 候选波次（有序，每个内层数组的成员预定并行实施）：`<CAND_WAVES>`
> 拆分理由（机械层已发现的文件冲突）：`<CAND_SPLIT>`
> 边集证据（出错时回溯）：`<DAG_JSON 的 .edge_evidence>`
>
> 对每个 change，读 `openspec/changes/<CID>/proposal.md`、`design.md`（若有）、`specs/**/spec.md`。重点判断**同一波次内成对 change 是否存在机械层看不到的串行耦合**：
> - 共享运行时状态 / 单例 / 全局配置的并发写
> - 跨 change 的初始化或调用时序约束（A 必须先建立某不变量 B 才成立）
> - 同一状态机 / 协议 / 数据模型的不变量被两个 change 同时改动
> - 一个 change 的 spec 语义实际前置另一个（R1/R2 漏标）
>
> **裁定规则（安全优先）**：发现耦合 → 把这对 change 拆到**不同子波次**（后者降级到更晚波次）。只有在你能明确断言"两者无任何共享写入/状态/时序耦合"时，才可保留并行或把候选里被拆开的提级合并；提级必须给理由。
>
> 输出严格 JSON（最后一行 `RESULT:` 引出）：
> `{"waves": [["cid",...],...], "changes": [{"pair":["a","b"],"verdict":"serialize|independent","reason":"..."}]}`
> 其中 `waves` 是你裁定后的最终有序子波次。
> 最后输出一行：`RESULT: {"phase":"architect-split","status":"ok","waves_path":"<把上面 waves 写到的绝对路径>"}`

**Agent B — `senior-code-developer`**（落地/构建/测试耦合，交叉视角）

> 你是并行实施的落地可行性核对者，**只读分析，不写代码**。工作目录：`<REPO_ROOT>`。
> 候选并行波次：`<CAND_WAVES>`（来自 §4.0 `dag-analyst`）。
> 文件影响集（每 cid 的 files）：`<DAG_JSON 的 .files>`，但 proposal 的 Affected Code 常写不全，**不要只信 .files**——用 Grep/Glob/Read 核对**同波次成对 change 在具体代码层面的冲突**：真实改动文件是否重叠（按代码实际推断、补足 .files 的盲区）、import/依赖闭包是否交叉、是否会动同一构建目标 / 同一测试夹具 / 同一资源文件。
> 发现冲突 → 要求把这对 change 串行化（拆不同子波次）。
> 输出同 Agent A 的 JSON schema，`changes[].reason` 给出你查到的具体文件/符号证据。
> 最后：`RESULT: {"phase":"code-split","status":"ok","waves_path":"<绝对路径>"}`

**合并（主 session 做）**

1. 读两个 agent 的 `waves`。构造"必须串行"对集合 `S` = A 判 serialize 的对 ∪ B 判 serialize 的对。
2. 从 `$CAND_WAVES` 出发，对 `S` 中每一对，若它们当前在同一子波次 → 把字典序较大者降级（移到该层后新增的子波次，保持拓扑序不破坏：降级目标仍在原层之后、下一层之前）。
3. 任何"提级合并"只有 A、B **都**判 independent 且给了理由时才采纳，否则忽略（安全优先）。
4. 得到 `FINAL_WAVES`。

### 4.10 打印 *Wave Plan Summary*（必须，留痕）

```
Wave Plan Summary
=================
Source: §4.0 dag-analyst (v3-dag-extract.json) | fallback: <none | main-session reason=...>
Candidate (npc plan waves): [3, 5, 3]   layers=3  file-splits=<n>
Architect verdict:    A=senior-system-architect  B=senior-code-developer  (or: skipped)
Final waves (execution order):
  wave 1  [design-system, diagnostics, session-orch]          parallel=3
  wave 2  [connection-mode, device-mgmt, egress-picker, ...]  parallel=5
  wave 3  [main-dashboard, menu-bar]                          parallel=2  (settings 降级 → wave 4)
  wave 4  [settings]                                          parallel=1  reason: A 判 settings↔main-dashboard 共享 SettingsRoot 状态
Demotions/Promotions:
  - serialize settings|main-dashboard  by=A  reason="..."
Cycles: <none | [...]>
```

把该 summary 原文写入 `<run_dir>/run.events.jsonl`（一条 `{"type":"plan.waves","final":[...],"candidate":[...],"verdicts":[...],"source":"dag-analyst|main-session-fallback"}`）供审计与自我迭代。

### 4.11 落地 plan

```bash
RUN_DIR=$(echo "$INIT_JSON" | jq -r '.run_dir')            # run_dir 来自 npc init 输出
# plan_order = FINAL_WAVES 展平（执行顺序）；npc 状态仍是线性 seq
PLAN_JSON=$(jq -nc --argjson w "$FINAL_WAVES" '$w | add')   # 展平
npc state init-run --plan-order "$PLAN_JSON"
# 把 FINAL_WAVES（含分组）存到 run 目录供执行循环按波次取
echo "$FINAL_WAVES" > "$RUN_DIR/v3-waves.json"
```

进入 Step 9：
- **`--auto`**：根本没进 plan 模式，直接进 Step 9，**无任何确认**。
- **交互模式**：`ExitPlanMode` 等用户批准后进 Step 9。

---

## Step 9 — 波次执行循环

按 `FINAL_WAVES` 顺序，一波一波推进。`SEQ` 仍是 npc 的全局线性序（展平后下标+1），保证 review/fix/archive 复用 v2 不变。

```
WAVE_BASE=$(git rev-parse HEAD)
FOR each WAVE in FINAL_WAVES:           # WAVE = [cid1, cid2, ...]（已确认可并行）
  T_WAVE_START=$(date +%s)

  ############ 10.1 并行 implement（worktree 隔离）############
  # 对 WAVE 中每个 CID，在同一条主 session 消息里并发 spawn 一个 Agent。
  FOR cid in WAVE (并发，单条消息多个 Agent 调用，受 --max-parallel 钳制):
    SEQ=<cid 在 plan_order 的位置>
    npc state add-change "$SEQ" "$cid"
    npc phase rotate --seq "$SEQ" --to implement
    # 复用 v2 的 npc 模板渲染（worktree 内可达 task_log 绝对路径）
    npc agent prompt render --phase implement --change-id "$cid"
    SPAWN=$(npc agent spawn-prompt --phase implement --change-id "$cid")
    GUIDE=$(echo "$SPAWN" | jq -r '.prompt')
    # Agent(subagent_type=senior-code-developer, isolation="worktree",
    #       prompt = GUIDE + 附加 worktree 契约，见下)

  # 附加到每个 implementer GUIDE 末尾的 worktree 契约：
  #   你在独立 git worktree 内工作，主分支安全。严格按渲染好的 prompt 实施并 commit。
  #   额外：把你写/改的文件绝对路径 + 你的 commit hash 写入 manifest JSON（files_written
  #   必须是对象数组，sha256 可选但建议给——npc verify manifest 会核对）:
  #     <run_dir>/v3-manifests/<cid>.json =
  #       {cid, worktree_path, commit, files_written:[{"path":"<绝对路径>","sha256":"<hex>"}, ...]}
  #   最后输出两行：先 npc 契约的 `RESULT: commit=<worktree_hash> tasks=.. tests=.. summary=.. notes=..`
  #   再 `MANIFEST: <绝对路径>`。

  # 收齐本波所有 implementer 返回后：
  FOR cid in WAVE:
    # plan-only 判定 + manifest 文件核对，一条命令（npc 1.4+）。RESULT 行接受 npc key=value
    # 与 legacy JSON 两种格式；--manifest 取自 implementer 的 MANIFEST 行（缺行即判 plan-only）。
    # 输出 {ok, verdict, reason, commit, files}；exit 0=有真实产出且核对通过。
    npc verify manifest --result "<该 cid 的 RESULT 行>" --manifest "<MANIFEST 行路径>" || {
      # verdict=plan_only → 重发；verdict=code 但 files_missing/sha_mismatch → 同样视为不可信，重发
      [ retries < MAXRETRY ] && 重发该 Agent（前缀 "IMPLEMENT NOW, do not plan."，新 worktree） && retry
      [ retries >= MAXRETRY ] && 标记 cid implementer-failed
    }
    # ★ webhook：每个 implementer 一返回就推（这就是"sub agent 完成后通过你主动推到 webhook"）
    notify --event implement-done --kv cid="$cid" --kv status="<ok|plan-only|failed>" \
           --text "implementer 完成 $cid（状态 <...>）"

  ############ 10.1b 串行整合（拓扑序，本波内按 plan_order 升序）############
  FOR cid in WAVE (按 SEQ 升序):
    SEQ=<...>; Wc=<manifest.commit>
    if git cherry-pick "$Wc"; then
      H=$(git rev-parse HEAD)               # 整合后 main 上的新 hash
      # hash 翻译：把 RESULT 行的 commit=Wc 换成 commit=H 再喂 npc。
      # 这是必要项而非防御：archive precheck / state_drift 用 merge-base --is-ancestor 校验
      # commit chain，worktree 原始 hash 不在 main 链上会被判 chain-broken。
      RESULT_H=$(echo "<RESULT 行>" | sed "s/commit=$Wc/commit=$H/")
      REC=$(npc implement record --seq "$SEQ" --result "$RESULT_H")
      [ "$(echo "$REC" | jq -r .ok)" != true ] && {
        DEC=$(npc auto-decide --seq "$SEQ" --trigger implementer-failed --apply); continue; }
      # 1.3 硬轨：不裸信 RESULT 自报 tests=pass，整合后在 main 上真实复跑
      npc verify tests; VT=$?
      if [ $VT -eq 1 ]; then
        git revert --no-edit "$H"           # 摘除坏 change，main 保持绿（revert 后 H 仍在链上，precheck 无碍）
        DEC=$(npc auto-decide --seq "$SEQ" --trigger implementer-failed --apply)
        ACTION=$(echo "$DEC" | jq -r '.action')
        # continue-retry → 串行重 implement（见下方冲突分支的同一路径）；skip → 记 failed 继续
        # run.events.jsonl 记 {"type":"v3.verify_tests_failed","cid":...,"reverted":"$H"}
        处理 ACTION; continue
      fi   # VT=3（探测不到测试命令）→ 记 warning 继续，不阻塞
    else
      git cherry-pick --abort
      # 子波次已预防，走到这里说明 Affected Code 漏标了重叠文件。
      # npc auto-decide 无 integrate-conflict trigger，复用 implementer-failed（语义=该 change 未能落地）。
      DEC=$(npc auto-decide --seq "$SEQ" --trigger implementer-failed --apply)
      ACTION=$(echo "$DEC" | jq -r '.action')
      # continue-retry → 在当前 main 上串行重 implement 该 cid。1.3 起优先用一行
      #   npc implement run --seq "$SEQ"   （headless coder：render → 后端 → 抽 RESULT → record）
      #   替代再开 worktree + Agent spawn；其 record 内建，成功后直接进 review。
      # skip → 记 failed，继续；并在 run.events.jsonl 记 {"type":"v3.integrate_conflict","cid":...}
      处理 ACTION; continue
    fi

  ############ 10.2–10.5 串行 review / fix / archive（逐字复用 v2）############
  FOR cid in WAVE (按 SEQ 升序，仅对已成功 implement record 的):
    # 与 new-plan-changes-v2 Step 9 的 §10.2 Round-0 Review、§10.3 分级、
    # §10.4 Fix Loop(≤20, npc phase rotate + agent + npc fix record + npc review run + stale/max-rounds auto-decide)、
    # §10.5 Archive(npc archive run) 完全一致。此处不复述，对照 v2。
    REVIEW=$(npc review run --seq "$SEQ" --round 0); ... ; npc archive run --seq "$SEQ"
    # ★ webhook：每个 change 走完 review/fix/archive 后推一条
    notify --event change-done --kv cid="$cid" --kv status="<archived|skipped|failed>" \
           --kv blocking="<最终 blocking 数>" --text "change 完成 $cid（<archived|skipped>）"

  # 本波收尾：记波次指标
  T_WAVE=$(($(date +%s) - T_WAVE_START))
  # 写一条 {"type":"v3.wave_done","wave":[...],"parallel":N,"wall_s":T_WAVE,
  #         "plan_only_retries":r,"cherry_pick_conflicts":c} 到 run.events.jsonl
  # ★ webhook：每波收尾推一条（含并行度与墙钟）
  notify --event wave-done --kv wave="<波次序号>" --kv parallel="$N" --kv wall_s="$T_WAVE" \
         --text "波次 <i> 完成：并行 $N，墙钟 ${T_WAVE}s"
  # ★ watch：刷新 watchable task 进度（best-effort）
  npc task update --id "npc-v3-$RUN_TS" --phase "wave-<i>" \
    --message "wave <i> done (parallel=$N)" --progress-current "<累计完成 change 数>" || true
  WAVE_BASE=$(git rev-parse HEAD)           # 下一波从已整合+已修复的 main 分叉
```

**要点**
- review/fix/archive **串行**：fix 会改 main 代码，本波 archive 完成后 main 前进，下一波 worktree 自然从新 main 分叉（靠 `worktree.baseRef=head`）。
- worktree commit 对象在共享 `.git`，cherry-pick 已在波内完成整合，Agent 事后清理 worktree 不产生悬空。
- 同波次 fix loop 仍串行：DAG 独立 ≠ 改动文件一定不交叉，但本波已通过 §4.9 + 文件着色确保不交叉，串行 fix 不会互相踩；并行 fix 留给未来 v3.1（需 npc worktree 感知）。

---

## Step 11 — 收尾（复用 v2）

```bash
FIN=$(npc state finalize)   # completed / completed-with-issues
npc summary render          # <run_dir>/run-summary.md
npc index append            # <task_log_dir>/index.jsonl
COST=$(npc cost --since "$RUN_T0")   # 按后端拆 token 成本，附进收尾报告
# ★ watch：终结 watchable task（best-effort）
npc task finish --id "npc-v3-$RUN_TS" \
  --status "$([ "$(echo "$FIN" | jq -r .status)" = completed ] && echo done || echo failed)" \
  --message "committed <n> / skipped <n>, <W> waves" || true
# ★ webhook：整个 run 收尾推一条总结
notify --event run-finalized \
       --kv status="$(echo "$FIN" | jq -r '.status // "done"')" \
       --kv committed="<n>" --kv skipped="<n>" --kv waves="<W>" \
       --text "new-plan-changes-v3 run 完成：<status>，committed <n> / skipped <n>，<W> 波"
```

**v3 额外**：summary 之外，run.events.jsonl 已含每波 `v3.wave_done` 指标。打印 v3 并行报告：

```
=== new-plan-changes-v3 完成 ===
Waves: <W>   Changes: <total>  (committed=<n> skipped=<n>)
Implement 墙钟(并行): Σ wave wall = <X>s
Implement 串行估算:   Σ implementer 单耗时 = <Y>s     Speedup ≈ <Y/X>x
plan-only 重试: <r>   cherry-pick 冲突: <c>   verify-tests 失败(revert): <v>   架构师降级: <d>
Token 成本(npc cost): <按后端一行摘要，来自 $COST>
```

---

## 通知（webhook，可选）

让 run 从"只能看"变成"主动推"。编排 session 在每个完成时刻调 `notify`（封装 `npc notify`，1.4+）向 webhook POST：

| 事件 | 触发时刻 | 关键字段 |
|---|---|---|
| `implement-done` | **每个 implementer sub-agent 一返回**（你要的"sub-agent 完成即推"） | cid / status(ok\|plan-only\|failed) |
| `change-done` | 单 change 走完 review/fix/archive | cid / status(archived\|skipped\|failed) / blocking |
| `wave-done` | 每波收尾 | wave / parallel / wall_s |
| `run-finalized` | 整个 run 收尾 | status / committed / skipped / waves |

- **启用**：`/new-plan-changes-v3 --auto --webhook=https://...`，或设环境变量 `NPC_WEBHOOK`（`NPC_V3_WEBHOOK` 仍兼容）。不设则所有 `notify` 静默 no-op。
- **形状**：`--webhook-format raw`（默认结构化 JSON）/ `slack`（`{text}`，适配 Slack incoming webhook）/ `feishu`（`{msg_type:"text",content.text}`，适配飞书自定义机器人）。
- **绝不打断**：`npc notify` 任何情况 exit 0（超时/拒连/4xx 只写 stderr 警告 + `delivered:false`）。webhook 挂了不影响 run，与 fire-and-forget 一致。
- 与桌面/手机的 `PushNotification` 工具互补：那是推到本机/手机，这是推到任意 HTTP 端点（CI、群机器人、自建服务）。本机观测另有 `npc task` + `npc watch`（Step 1 登记、每波 update、Step 11 finish）。

## 关键约束（速查）

- **webhook 通知 best-effort**：`npc notify` 永远 exit 0，webhook 不可用绝不打断 run；`npc task` 观测调用同样 `|| true`。
- **`--auto` = 真·fire-and-forget，零人工确认**：不进 plan 模式、不调 `ExitPlanMode`、不用 `AskUserQuestion`；所有运行中决策走 `npc auto-decide --apply`。单 change 失败（含 `npc` exit 4 / cherry-pick 冲突 / validate 失败）一律 skip+继续，汇总进收尾报告；**唯一真停**是编排基底坏掉（`npc` exit 3 / `auto-decide` abort）与 Step 0 环境前置失败。绝不为"让用户点确认"而停。
- **npc 由编排 session 经 Bash 调用**（非在 implementer sub-agent 内）；implementer/reviewer 才是 sub-agent。无论谁调，上面的失败策略不变。
- **只并行 implement，skill 零脚本**：review/fix/archive/state/telemetry/续跑全走 v2 既有 npc 命令；v3 专属的机械逻辑（波次划分/manifest 核验/webhook）也在 npc 1.4 内（`plan waves` / `verify manifest` / `notify`）。skill 只承担"DAG 抽取 sub-agent + 波次切分裁定 + worktree 并行 + cherry-pick 整合"的编排决策。
- **DAG 抽取走 sub-agent**（§4.0 `dag-analyst`）：主 session 不读 proposal/design/tasks/spec_delta，只接其严格 JSON 输出；N≥10 时这是上下文压力的关键阀门。失败一次允许重试，再失败 `--auto` 才降级回主 session 自抽并标 `dag_extract_fallback`，交互模式真停。
- **切分必经 sub-agent 裁定**（除非 `--no-architect`）：§4.0 候选 + 双架构师（`senior-system-architect` + `senior-code-developer`）安全优先取串行并集；裁定结果与理由写 *Wave Plan Summary* + run.events.jsonl 留痕。
- **`worktree.baseRef=head` 硬前置**：非 head 报错退出，绝不退化 fresh。
- **工作树必须 clean**：cherry-pick 整合前提。
- **整合 = cherry-pick + hash 翻译**：worktree hash → main hash 后才喂 `npc implement record`，commit chain 线性、续跑可对齐。
- **plan_order 必须来自 DAG 拓扑（v2 §4.1–4.7）**：tier 仅同层 tie-breaker；严禁字符序/mtime 序。
- **Implementer 一律 `Agent(senior-code-developer, isolation:"worktree")`**；分析 agent 一律只读；Reviewer/Fixer 走 npc（codex exec / senior-code-developer）。
- **RESULT 行 + MANIFEST 行是契约**：缺失、commit=-、files_written 为空、文件丢失或 sha 不符 → plan-only/不可信重试（`npc verify manifest` 一条命令全判）。
- **整合后必须 `npc verify tests` 真实复跑**（探测不到测试命令除外）：fail → revert + auto-decide，绝不把红的 main 带进 review。
- **commit message 严禁 AI 署名 trailer**；禁 `--no-verify` / 跳测试 / 跳签名。
- **运行轨迹外置**：state/event/summary/telemetry 落 `~/task_log/<PROJ_KEY>/`，工程目录零侵入（worktree 仅临时存在）。

---

## 已知陷阱速查

- **worktree.baseRef=fresh** → 每波从 origin 分叉，丢前序成果 → §0.3 硬校验报错。
- **主 session 不该读 N 份 proposal**（v2 老问题：N 个 change 时主上下文线性污染）→ §4.0 `dag-analyst` sub-agent 完成，主 session 只读 JSON；schema 校验失败重发一次，再失败 `--auto` 才降级回主 session 自抽。
- **§4.0 输出 schema 失效**（nodes 缺、files 漏、candidate 覆盖不全）→ 主 session 校验后拒收并重发；不允许沉默接收。
- **同波次文件交集漏判**（proposal Affected Code 写不全）→ §4.0 sub-agent 用 Grep/Glob 把目录级写法展开为具体文件 + §4.9 工程师视角再核 + 整合阶段 cherry-pick 冲突兜底（abort + auto-decide 串行重 implement）。
- **架构师 agent 为加速过度提级并行** → 合并规则安全优先（提级须双 agent 都判 independent + 给理由）；agent 不可用退化为 §4.0 候选（更保守）。
- **worktree 内 implementer 找不到 task_log 路径** → task_log 在 `~/task_log` 绝对路径，跨 worktree 可达；`npc agent prompt render` 写的 `$BASE/...` 用绝对路径，无碍。
- **续跑时 worktree 已被清理** → 未走到 `implement record` 的 change 退回 pending 重新并行（worktree commit 未整合即视为未完成）。
- **hash 翻译不是防御而是必要项**：`npc archive precheck` 与 state_drift 扫描用 `git merge-base --is-ancestor <c> HEAD` 校验 commit chain（`cat-file -e` 只查对象存在性），worktree 原始 hash 不在 main 链上会被判 chain-broken。逐个 cherry-pick 后立即以新 hash record 即满足。
- **npc 1.3+ `init --auto` 会写 `<repo>/.claude/settings.json`**（auto_auth）→ 弄脏工作树破坏 cherry-pick 前提；Step 1 已内置收编（tracked 则 commit，untracked 则本地 exclude），漏做会让下次 run 的 §0.1 clean 校验失败。
- **verify tests 失败的 change 已在 main 上**（record 前才复跑不现实——record 需要整合后 hash）→ 本设计 fail 即 `git revert`，main 始终绿；revert 不破坏 precheck（原 commit 仍在链上）。
- 其余 codex/fix/stale/timeout 类陷阱同 v2，由 npc 内建处理。

---

## 依赖的 npc 子命令（v3 专属，1.4+）

v3 原自带的四个 helper 脚本已全部下沉为 npc 子命令（契约见 `docs/cli.md` §8c），本 skill **零自带脚本**；行为不符时在 agent-spine 仓库（`src/npc`）修 npc 并在仓库根 `uv tool install --force --from . npc` 重装，不要回退到 skill 内写脚本。

- `npc plan waves [--input FILE]`（stdin JSON）— DAG 分层 + 文件交集拆子波次，出**候选**波次（单行 JSON；exit 2=输入不合法）。**由 §4.0 sub-agent 在 sub-agent 上下文里跑**，主 session 不再直接调用（降级路径除外）。
- `npc verify manifest --result '<RESULT行>' --manifest PATH` — plan-only 判定（npc key=value / legacy JSON 双格式）+ manifest 文件存在性与 sha256 核对，一条命令（exit 0=有真实产出且核对通过）。
- `npc notify --event KIND [--url URL] [--format raw|slack|feishu] [--kv k=v ...] [--text ...]` — best-effort webhook 推送，**总是 exit 0**。URL 空则依次读 `$NPC_WEBHOOK` / `$NPC_V3_WEBHOOK`，仍空则静默 no-op。
- 另用到的 1.3 命令：`npc doctor`（§0.2 体检）、`npc verify routing`（§0.2 路由不变量）、`npc verify tests`（整合后真实复跑）、`npc implement run`（冲突后串行重实施）、`npc task start/update/finish`（`npc watch` 可观测）、`npc cost`（收尾成本报告）。
