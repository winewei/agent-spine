## Why

`/spine-run` Step 2B（自由目标 → 拆成 openspec change → 补齐 artifact）目前由**主 session 自己**完成，没有任何质量门、没有任何评审。而主 session 的职责契约是「只调度与决策，不写业务代码、不解析自然语言日志」。写 spec 是重度生成任务，放在主 session 里既污染编排 context，也让 spec 成为整条流水线上**唯一未经独立验证**的产物。

前置三个 change 已经把地基铺好：

- `spec-schema-hardening`：artifact 存在性硬门 + 生成时点的静态写作规范。
- `spec-routing-invariant`：`check_routing` 已能强制「spec 生成方 ⊥ spec 验证方」与「MiMo 不得用于 spec 验证」。
- `spec-attribution-telemetry`：code review 的每条 blocking finding 已带结构化 spec 归因，`spec_attributable_blocking_rate` 可观察——**尺子先于被量的东西**。
- `repo-spec-lint`：确定性静态语义门已就位（仓库本地脚本 `scripts/check_spec.py`，不在 npc 内）。

本 change 补上最后一块：一个专职的 spec 生成执行体，及其**强制的独立语义评审**。

静态门与语义门的收益必须分开记账。已核实：历史上可归因于 spec 的 blocking findings 中，「`design.md` 写了『实施时定』」可被静态规则命中；而「`tasks.md` 少了一条 CLI 暴露任务」「缺 abort 错误路径 scenario」属于**开放式语义判定**，静态规则不可判定（判定「一个应该存在的东西不存在」等价于理解 change 语义）。这类只能由 codex spec review 拦。

## What Changes

- **新增** subagent `spine-spec-writer`（`plugins/agent-spine/agents/spine-spec-writer.md`），职责单一：按 npc 渲染的 prompt 文件写/改一个 openspec change 的 artifact，回报一行结构化 RESULT。
- **新增** 用户入口命令 `/spine-spec "<目标>"`（`plugins/agent-spine/commands/spine-spec.md`）。
- **新增** `npc spec write run|record` 与 `npc spec fix run|record`：渲染 prompt、**恒 in-session 分发**（`deferred=true`）、装订 RESULT，形态与既有 `npc implement/fix` 同构。超时预算复用 `npc agent timeout-budget|record-timeout`（phase 名 `spec_write` / `spec_fix-r{N}`）。
- **新增** `npc spec review run --change <id> --round N`：以 codex 为引擎，对 change 的 artifact 做语义评审，输出结构化 **`round-{N}.spec-review.json`**（自有 category 枚举，含 `line_range`）。
- **MODIFIED** `RESULT_REQUIRED_KEYS` 新增 `spec_write` 与 `spec_fix` 两个 phase 的必需键集合。
- **新增** telemetry event kind `spec_review.round`。
- **新增** 质量门顺序：`openspec validate --strict` → 配置的确定性 gate 命令（`[spec_review] gate_cmd`，便宜）→ LLM 语义评审（昂贵）。前者失败不进入后者。
- **新增** `[spec_review] gate_cmd` 配置项，形态为 **argv 数组**（如 `["uv","run","scripts/check_spec.py"]`）；npc 追加 `--change <id>` 并以 `shell=False` 执行。npc **只**读其 `ok` 与 `rule_hits`，透传 `rule_hits` 进 telemetry；npc MUST NOT 持有任何 spec 写作规则内容。此模式与既有 `[verify] test = "uv run pytest -q"` 同构。
- **新增** 固定 fix 次数上限的循环：`[spec_review] max_rounds = N` 表示「最多 `N` 次 fix」，review 轮次索引 `0..N`（共 `N+1` 次 review），默认 `N=3`。**不复用** code review 的 stale 检测。
- **新增** 越界修改的确定性拦截：`npc spec write|fix record` 在装订前用 `git status --porcelain` 校验变更集仅限 `openspec/changes/<id>/`，越界即 `out_of_scope_changes` 拒绝装订（`spine-spec-writer` 持有 `Bash`，文案约束不足以防越界）。

**非目标（Non-Goals）**：

- **不接管 `/spine-run` Step 2B。** 本 change 只提供 `/spine-spec` 独立入口，产出的 change 可由 `/spine-run <change-name>` 直接消费。把 Step 2B 改为内部 spawn `spine-spec-writer` 属独立 change，需先由本 change 的 telemetry 验证 spec review 的收敛行为。
- **不复用 `npc review` 的 stale 检测**（`rounds_since_strict_decrease`）。其前提是「blocking 单调下降代表收敛」——代码缺陷是有限集会收敛，而 spec 的 ambiguity/scope-creep 每次改写可长出全新一批。阈值 `3` 是从 code review 的 telemetry 方差中标定的经验常数，spec review 目前**零样本**。按不变量 3，不得先立此硬轨。
- 不引入基于 `spec_attributable_blocking_rate` 的任何闸门。
- 不实现跨 spec 的质量评分、排名或对比。
- 不改变 `npc implement` / `npc fix` / `npc review` 任何既有行为。
- 不把 spec review 的 findings 或 rubric 用于任何 **code** 阶段的 prompt。

## Capabilities

- **New Capabilities**: `spec-writer` —— 专职 spec 生成执行体 + 强制的独立 spec 语义评审循环，与 coder/code-review 在结构上同构，并受同一套「生成 ⊥ 验证」不变量约束。

## Impact

- **受影响代码**：新增 `src/npc/spec_pipeline.py`、`plugins/agent-spine/agents/spine-spec-writer.md`、`plugins/agent-spine/commands/spine-spec.md`；`src/npc/cli.py`（注册 `npc spec write|fix|review`）；`src/npc/schema.py`（新增 `SPEC_REVIEW_SCHEMA`）；`src/npc/pipeline.py`（`RESULT_REQUIRED_KEYS`）；`src/npc/config.py`（`[spec_review] gate_cmd` / `max_rounds`）；`src/npc/telemetry.py` + `telemetry_schema_v1.json`（`spec_review.round`）；`plugins/agent-spine/.claude-plugin/plugin.json`。
- **兼容性**：纯新增路径。既有 `/spine-run` 行为不变；`.npc/config.toml` 未配 `[spec_writer]`/`[spec_review]` 时取安全默认（claude writer / codex review）。
- **不变量影响**：
  - **不变量 1（生成 ⊥ 验证）：边界为时点，非内容。** 依据：`src/npc/agent.py` 的 `_default_review_path` 恒解析 `round_n - 1`（生成侧只可能拿到**上一轮已签发**的 review），且该模块不 import `focus`（渲染 review rubric 的模块）。因此 `spine-spec-writer` 与 `spine-coder` 结构同构：**generate 轮 MUST NOT 见到本轮 spec-review 的 focus/rubric；fix 轮 MAY 读取上一轮已签发的 blocking findings**。此结构由 `spec-routing-invariant` 的 `check_routing` 在配置层强制不同源，由本 change 的负向测试在渲染层强制不泄漏。
  - 不变量 2（不信 LLM 散文）：`round-{N}.spec-review.json` 由 JSON Schema 强制，RESULT 行由 `RESULT_REQUIRED_KEYS` 强制。**满足**。
  - 不变量 3（新硬轨须被真实方差打出来）：本 change **不新增**基于历史数据的判据硬轨；固定 max-rounds 是**防失控兜底**而非质量判据，且明确拒绝移植 stale 阈值。**满足**。
