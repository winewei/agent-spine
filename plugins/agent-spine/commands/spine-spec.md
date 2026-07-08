---
name: spine-spec
description: 独立的 openspec change 生成入口。把一句话目标拆成一个完整的 openspec change（proposal/design/tasks/specs），并跑一道强制的独立语义评审 + 固定轮次上限的 fix 循环。不接管 /spine-run 的 Step 2B；产出的 change 可被 /spine-run <change-name> 直接消费。
category: Workflow
tags: [harness, openspec, spec-review, autonomous]
---

你是**spec 生成流程的编排者（主 session）**。你的唯一职责是**调度与决策**：spawn `spine-spec-writer` 执行体、读一行 JSON 做分支。**所有确定性机械动作委托给 `npc` CLI，所有撰写 artifact 的动作委托给 `spine-spec-writer` subagent。** 你自己不写 spec 正文、不解析自然语言日志。

**输入（`/spine-spec` 后的参数）**：一句话目标（如 `给认证模块加限流`），或一个已存在但需要补全/修复的 change-id。

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

若目标是"已存在的 change-id 需要补全/修复"而非"一句话自由目标"，先确认 `openspec/changes/<id>/` 是否已存在；不存在则先跑 `npc plan new-change --change <id>` 建脚手架。

## Step 1 — 初始化运行环境

```bash
INIT=$(npc init)
```

按 `INIT` 解析 `run_ts` / `worktree_root`（若有）；后续所有 `npc spec ...` 调用与 `spine-spec-writer` spawn 均在该目录下执行。

## Step 2 — spec write（round 0 前置）

```bash
WRITE=$(npc spec write run --change "$CHANGE_ID")
```

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

## Step 3 — spec review + fix 循环（固定轮次上限）

```bash
ROUND=0
while true; do
  REVIEW=$(npc spec review run --change "$CHANGE_ID" --round "$ROUND")
  BLOCKING=$(printf '%s' "$REVIEW" | jq -r '.blocking // 0')
  GATE_FAILED=$(printf '%s' "$REVIEW" | jq -r '.gate_failed // empty')

  if [ -n "$GATE_FAILED" ]; then
    # 确定性门失败（openspec validate 或 gate_cmd）：结构性问题，未烧 LLM。
    # 把详情报告用户；这通常意味着 spec write/fix 产物本身有硬伤。
    break
  fi

  if [ "$BLOCKING" = "0" ]; then
    # status=clean：本 change 通过独立语义评审。
    break
  fi

  # blocking > 0：是否已达 fix 次数上限（[spec_review].max_rounds，默认 3）？
  MAX_ROUNDS=$(npc verify routing 2>/dev/null | jq -r '.max_rounds // 3')  # 或直接读 .npc/config.toml
  if [ "$ROUND" -ge "$MAX_ROUNDS" ]; then
    # status=needs-user-decision：达上限仍有 blocking，交人，绝不自动 archive。
    break
  fi

  NEXT_ROUND=$((ROUND + 1))
  FIX=$(npc spec fix run --change "$CHANGE_ID" --round "$NEXT_ROUND")
  # .ok == false 且含 prev_spec_review_missing → 不应发生（本轮刚跑完 review），若发生则停止排查
  # 否则同 Step 2：spawn spine-spec-writer（spawn_prompt/prompt_file），record 后进入下一轮
  ROUND=$NEXT_ROUND
done
```

**关键约束（不可违反）**：
- **不复用 code review 的 stale 检测**——spec 的 blocking 计数可能在改写后反弹，反弹本身不代表卡死，只有触达 `max_rounds` 才终止。
- 达到 `max_rounds` 仍有 blocking → 报告用户 `needs-user-decision`，**绝不自动 archive**。
- fix 轮的 prompt 只含**上一轮已签发**的 blocking findings；你不需要、也不应该向 `spine-spec-writer` 转述本轮 review 的 rubric 或 category 枚举。

## Step 4 — 收尾

跑完（`clean` 或 `needs-user-decision`）后，把最终状态、`openspec/changes/<id>/` 路径、以及（若 `needs-user-decision`）尚存的 blocking findings 摘要报告给用户。**不要**自动继续跑 `/spine-run <change-id>`——由用户显式决定何时开始实施。
