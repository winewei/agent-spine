---
name: spine-spec
description: 独立的 openspec change 生成入口。把一句话目标拆成一个完整的 openspec change（proposal/design/tasks/specs），并跑一道强制的独立语义评审 + 固定轮次上限的 fix 循环。不接管 /spine-run 的 Step 2B；产出的 change 可被 /spine-run <change-name> 直接消费。
category: Workflow
tags: [harness, openspec, spec-review, autonomous]
---

你是**spec 生成流程的编排者（主 session）**。你的唯一职责是**调度与决策**：spawn `spine-spec-writer` 执行体、读一行 JSON 做分支。**所有确定性机械动作委托给 `npc` CLI，所有撰写 artifact 的动作委托给 `spine-spec-writer` subagent。** 你自己不写 spec 正文、不解析自然语言日志。

**输入（`/spine-spec` 后的参数）**：一句话目标（如 `给认证模块加限流`），或一个已存在但需要补全/修复的 change-id。

**模式标志（与 `/spine-run` 同构）**：参数含 `--auto` → 全自主档；否则 → 交互档（关键闸口调用 `AskUserQuestion` 问用户）。判断逻辑与 `plugins/agent-spine/commands/spine-run.md` 完全一致——参数含 `--auto` → 全自主档，否则 → 交互档，两处保持字面一致，用户不必记两套心智模型。

**`--auto` 的硬规则**：auto 档下你**绝不调用 `AskUserQuestion`**，也**绝不调用 `npc spec interrogate decide`**——即便模式盘问产出的开放问题数 `.open_questions > 0`，也跳过用户裁决步骤，直接进入 write 轮（write 轮读到未裁决的开放问题后按指令原样序列化进 design.md）。

---

## 与 `/spine-run` 的关系（非目标边界）

本命令**不接管** `/spine-run` Step 2B（自由目标拆解仍由 `/spine-run` 主 session 自己完成）。`/spine-spec` 是独立入口：跑完后产出一个通过语义评审的 openspec change，可直接喂给 `/spine-run <change-name>` 走 implement→review→fix→archive。两条流水线的 findings/rubric 全程互不可见（见 `docs/principles.md` 不变量 1）。

---

## Step 0 — 前置检查

```bash
npc --version
git rev-parse --show-toplevel >/dev/null
command -v openspec >/dev/null
```

任一缺失，一句话告诉用户缺什么、怎么装，**不要继续**。

## Step 1 — 初始化运行环境

```bash
INIT=$(npc init)
WORKTREE_ROOT=$(printf '%s' "$INIT" | jq -r '.worktree_root // empty')
```

按 `INIT` 解析 `run_ts` / `worktree_root`（若有）。**`npc init` 默认从 HEAD 新建一个独立 worktree**（`--no-worktree` 才就地执行）——若 `WORKTREE_ROOT` 非空，**立即 `cd "$WORKTREE_ROOT"`**：

```bash
if [ -n "$WORKTREE_ROOT" ]; then cd "$WORKTREE_ROOT"; fi
```

本命令**后续所有步骤**（Step 2 判断 change-id 是否已存在 / `npc plan new-change` 建脚手架 / `npc spec write|review|fix run` / `spine-spec-writer` spawn）**必须**在此目录（`WORKTREE_ROOT`，若为空则原 checkout）下进行，不得散落在两处——否则脚手架文件会落在原 checkout 而未提交，`npc spec ...` 系列命令实际执行的 worktree 里看不到它们（round 4 finding F2：free-goal 分支曾在 `npc init` 之前就跑 `npc plan new-change`，脚手架建在了错误的 repo root）。

## Step 2 — 判断输入类型，确定 `CHANGE_ID` 与 `GOAL`

`/spine-spec` 后的参数只有两种合法形态；由你（主 session）判断走哪条分支——判断依据是参数字面量是否已经是（当前 cwd 下）`openspec/changes/` 下存在的 change-id，而不是猜测语义：

- **分支 A：一句话自由目标**（如 `给认证模块加限流`）——参数不是一个已存在的 change-id。
  - `GOAL` = 用户输入的原文，**一字不改、不摘要**（下游要原文传给 `spine-spec-writer`，语义丢失在这一步发生，不能靠后面补）。
  - 由你自己从 `GOAL` 的语义提炼一个简短、描述性的 kebab-case `CHANGE_ID`（如 `auth-rate-limit`）；提炼时不得引入本次评审的 rubric/category 措辞（不变量 1）。
  - 若 `openspec/changes/$CHANGE_ID/` 不存在：在当前 cwd（`WORKTREE_ROOT`）下 `npc plan new-change --change "$CHANGE_ID" --description "$GOAL"` 建脚手架——`--description` 会把 `GOAL` 原文落盘到脚手架 `README.md`，作为磁盘层面的第二道留痕（与 Step 3 传给 writer prompt 的路径互为冗余，不互相替代）。
  - 若目录已存在（同名 change 已有人起过头）：不重新建脚手架，`GOAL` 仍保留，进入 Step 3。
- **分支 B：已存在 change-id 需要补全/修复**——参数本身就是（当前 cwd 下）`openspec/changes/<id>/` 已存在的目录名。
  - `CHANGE_ID` = 该参数原文。
  - `GOAL` 留空（不强行编造目标文本；已有草稿本身就是上下文，`spine-spec-writer` 会读 `openspec/changes/<id>/` 下现有文件）。

## Step 3 — 模式盘问（先于 spec write，硬前置）

**这一步必须在 Step 4（spec write）之前完成**。核心编排顺序恒为：`npc spec interrogate run` → spawn `spine-spec-writer` 撰写 `pattern-interrogation.md` → `npc spec interrogate record` → 依据 `.open_questions` 分支裁决 → 才进入 `npc spec write run`。原因：后一步有一道硬前置门——`openspec/changes/<id>/pattern-interrogation.md` 不存在（或缺三个必需 H2 段落）时，`npc spec write run` 会直接 `ok=false`、拒绝渲染 write prompt。因此 **MUST NOT** 有任何在 `npc spec interrogate run`/`record` 完成前直接调用 `npc spec write run` 的路径。

```bash
if [ -n "$GOAL" ]; then
  INTERROGATE=$(npc spec interrogate run --change "$CHANGE_ID" --goal "$GOAL")
else
  INTERROGATE=$(npc spec interrogate run --change "$CHANGE_ID")
fi
```

- `.ok == false` 且含 `spec_routing_violation` → 停止，把 `violations` 报告用户（同 write 轮的路由处理，路由真相源唯一）。
- `.ok == true`：`.deferred == true` 恒成立。取 `.spawn_prompt` 与 `.prompt_file`，用 `Agent` 工具以 `spine-spec-writer` 身份 spawn 一次撰写 `pattern-interrogation.md`。超时预算复用既有四件套：`npc agent timeout-budget --change "$CHANGE_ID" --phase spec_interrogate`（超时则 `npc agent record-timeout --change "$CHANGE_ID" --phase spec_interrogate` 后重试，直到 `.exhausted`）。

subagent 结束后拿其 RESULT 行装订，并读出独立解析的开放问题数：

```bash
REC=$(npc spec interrogate record --change "$CHANGE_ID" --result "$RESULT_LINE")
# .ok == false：out_of_scope_changes / unexpected_commit → 越界，停止报告用户；
#   pattern_interrogation_missing / pattern_interrogation_missing_section → 盘问产物缺失或缺段，重试一次或停止。
OPEN_QUESTIONS=$(printf '%s' "$REC" | jq -r '.open_questions // 0')
```

**依据 `.open_questions` 与模式标志分支裁决**：

- **交互档（未传 `--auto`）且 `.open_questions > 0`**：先用 `AskUserQuestion` 把 `pattern-interrogation.md` 的每条开放问题摆给用户裁决，再把裁决原文（问题 + 用户选择/输入）拼装成一段文本，调用 `npc spec interrogate decide --change "$CHANGE_ID" --decisions-md "<裁决原文>"` 把它追加进 `pattern-interrogation.md` 的 `## User Decisions (Interactive)` 段，**然后才**进入 Step 4（write 轮）。
- **交互档且 `.open_questions == 0`**：MUST NOT 调用 `AskUserQuestion` 或 `npc spec interrogate decide`，直接进入 Step 4。
- **`--auto` 档**：无论 `.open_questions` 是否 `> 0`，MUST NOT 调用 `AskUserQuestion` 或 `npc spec interrogate decide`；在 interrogate 阶段（`run` + `record`）完成后直接进入 Step 4。

## Step 4 — spec write（round 0 前置）

```bash
if [ -n "$GOAL" ]; then
  WRITE=$(npc spec write run --change "$CHANGE_ID" --goal "$GOAL")
else
  WRITE=$(npc spec write run --change "$CHANGE_ID")
fi
```

- `.ok == false` 且含 `pattern_interrogation_missing`/`pattern_interrogation_missing_section` → 不应发生（Step 3 刚完成盘问），若发生则说明盘问产物有结构缺陷，回到 Step 3 排查。

`--goal`（分支 A 才传）把 `GOAL` 原文透传进 `spec-write.prompt.md`，使 `spine-spec-writer` 在撰写 artifact 时能看到用户的原始一句话目标，而不仅仅是一个 `CHANGE_ID` 字符串。

- `.ok == false` 且含 `spec_routing_violation` → 停止，把 `violations` 里的 `rule`/`detail` 告诉用户（配置问题，不是你能决定的）。
- `.ok == true`：`.deferred == true` 恒成立（v1 只支持 in-session）。取 `.spawn_prompt` 与 `.prompt_file`，用 Claude 的 `Agent` 工具以 `spine-spec-writer` 身份 spawn 一次，`prompt` 字段传 `.spawn_prompt`。

取超时预算并在 spawn 前后配合使用：

```bash
TB=$(npc agent timeout-budget --change "$CHANGE_ID" --phase spec_write)
# 用 .timeout_sec 作为本次 Agent 调用的 wall-clock 超时；超时则 npc agent record-timeout --change "$CHANGE_ID" --phase spec_write 后重试，直到 .exhausted
```

subagent 结束后拿到其最后一条消息的 RESULT 行，装订：

```bash
npc spec write record --change "$CHANGE_ID" --result "$RESULT_LINE"
```

`.ok == false`：
- `out_of_scope_changes` / `unexpected_commit` → 这是 `spine-spec-writer` 违反了职责边界，停止并把违规详情报告用户（不要自动重试掩盖）。
- 其它（`result-missing-keys` 等）→ 视情况重试一次或停止询问用户。

## Step 5 — spec review + fix 循环（固定轮次上限）

```bash
ROUND=0
while true; do
  REVIEW=$(npc spec review run --change "$CHANGE_ID" --round "$ROUND")
  OK=$(printf '%s' "$REVIEW" | jq -r '.ok')
  GATE_FAILED=$(printf '%s' "$REVIEW" | jq -r '.gate_failed // empty')

  if [ -n "$GATE_FAILED" ]; then
    # 确定性门失败（openspec validate 或 gate_cmd）：结构性问题，未烧 LLM。
    # 把详情报告用户；这通常意味着 spec write/fix 产物本身有硬伤。
    break
  fi

  if [ "$OK" != "true" ]; then
    # ok=false 且 gate_failed 为空：LLM 语义评审本身没有真正跑完——
    # dependency_missing（portable_timeout/codex/claude 二进制缺失）、
    # <engine>-exec-failed（引擎超时/非零退出/产物缺失）、
    # invalid_spec_review_schema（引擎输出不符 schema）均属此类。
    # 此时 `.blocking` 键必然缺失，绝不能把它当 0 处理进入 clean 分支
    # （round 4 finding F1：曾经只读 `.blocking // 0`，非门失败会被误判为 clean）。
    # 停止，把 `.error`/`.detail` 原样报告用户——这是配置/环境问题，不是可自动重试掩盖的。
    break
  fi

  BLOCKING=$(printf '%s' "$REVIEW" | jq -r '.blocking // 0')

  if [ "$BLOCKING" = "0" ]; then
    # status=clean：本 change 通过独立语义评审。
    break
  fi

  # blocking > 0：是否已达 fix 次数上限（[spec_review].max_rounds，默认 3）？
  # 注意：MAX_ROUNDS 必须从本轮 $REVIEW 里读——`npc verify routing` 只 emit 路由字段
  # （ok/coder_backend/review_engine/violations），从不含 [spec_review].max_rounds；
  # `npc spec review run` 已经加载了同一份 config，success 分支会把 max_rounds 原样
  # 透传出来，这是唯一确定性真相源。
  MAX_ROUNDS=$(printf '%s' "$REVIEW" | jq -r '.max_rounds // 3')
  if [ "$ROUND" -ge "$MAX_ROUNDS" ]; then
    # status=needs-user-decision：达上限仍有 blocking，交人，绝不自动 archive。
    break
  fi

  NEXT_ROUND=$((ROUND + 1))
  FIX=$(npc spec fix run --change "$CHANGE_ID" --round "$NEXT_ROUND")
  # .ok == false 且含 prev_spec_review_missing → 不应发生（本轮刚跑完 review），若发生则停止排查
  # 否则同 Step 4：spawn spine-spec-writer（spawn_prompt/prompt_file），record 后进入下一轮
  ROUND=$NEXT_ROUND
done
```

**关键约束（不可违反）**：
- **`.ok` 必须在 `.blocking` 之前判定**——`.ok == false` 时（无论是否带 `gate_failed`）都不得读 `.blocking` 做分支决策；只有 `.ok == true` 的成功评审结果才谈得上 `.blocking` 是否为 0。
- **不复用 code review 的 stale 检测**——spec 的 blocking 计数可能在改写后反弹，反弹本身不代表卡死，只有触达 `max_rounds` 才终止。
- 达到 `max_rounds` 仍有 blocking → 报告用户 `needs-user-decision`，**绝不自动 archive**。
- fix 轮的 prompt 只含**上一轮已签发**的 blocking findings；你不需要、也不应该向 `spine-spec-writer` 转述本轮 review 的 rubric 或 category 枚举。

## Step 6 — 收尾

跑完后按最终状态分三种情况报告给用户，并附上 `openspec/changes/<id>/` 路径：

- **`clean`**（`.ok == true` 且 `.blocking == 0`）：通过独立语义评审，可交给用户决定何时 `/spine-run <change-id>`。
- **`needs-user-decision`**（达 `max_rounds` 仍有 blocking）：附上尚存的 blocking findings 摘要。
- **评审未完成**（`.ok == false` 且无 `gate_failed`，或 `gate_failed` 非空）：附上 `.error`/`.detail`/`.gate_failed`，说明这是环境/配置问题或 artifact 结构性硬伤，而非语义评审给出的结论——不要暗示 change 已"通过"或"未通过"评审。

**不要**自动继续跑 `/spine-run <change-id>`——由用户显式决定何时开始实施。
