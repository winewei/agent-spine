## Why

`npc verify routing` 是核心不变量「生成 ⊥ 验证」与「MiMo 只许执行」在代码层的唯一强制点。其纯函数 `check_routing(cfg)`（`src/npc/verify.py:214`）目前**只覆盖 `cfg.coder` 与 `cfg.review` 这一对**，含**五条**规则：

- `backend_unsupported` / `engine_unsupported`：后端/引擎有效性
- `gen_not_orthogonal`：coder 与 review 解析到同一执行身份 → 自己评自己
- `mimo_exec_only`：review 路由到 MiMo（engine / claude_bin / claude_model 含 `mimo`）→ 违反 MiMo 仅限执行
- `mimo_in_session`：coder 某 phase 后端为 mimo 且 dispatch 为 in-session → 违反 MiMo 只许 headless

**同时发现一个既有漏洞**：`SUPPORTED_CODER_BACKENDS = ("claude", "mimo", "codex")`（`src/npc/config.py:36`）含 `codex`，而 `gen_not_orthogonal` 的同源判定只覆盖 (a) 双方 claude 且同 bin 同 model、(b) 双方 mimo。因此 **`coder.backend = "codex"` 且 `review.engine = "codex"` 当前不会被判为同源**——不变量 1 在这条路径上被静默绕过。本 change 一并修复。

后续要引入的 `spine-spec-writer`（生成 spec）与 spec reviewer（验证 spec）构成**第二对生成/验证关系**，但 `check_routing` 对它没有任何约束。若两者被配成同一执行身份，spec 就是自己写自己批——不变量 1 在 spec 层被静默绕过，且无任何硬轨报警。

这是一个**在写 spec agent 之前就必须堵上的缺口**：先立规矩，再放人进来。

按 `CLAUDE.md` 的 npc 边界规则，npc 只放「生命周期钩子、telemetry、state 读写、**路由不变量**」四类。本 change 落在第四类正中，落点无争议。

## What Changes

- **新增** `[spec_writer]` 与 `[spec_review]` 两个 `.npc/config.toml` 配置段，含安全默认值（`spec_writer.backend = "claude"`、`spec_review.engine = "codex"`），未配置时不产生 violation。
- **MODIFIED** `check_routing(cfg)` 增加**五条**与既有规则同构的 spec 侧规则：`spec_backend_unsupported`、`spec_engine_unsupported`、`spec_gen_not_orthogonal`、`spec_mimo_exec_only`、`spec_mimo_in_session`。
- **MODIFIED（修既有漏洞）** `gen_not_orthogonal` 的同源判定新增 (c) 双方均为 `codex` 的形态。`rule` 字符串与 `detail` 语义不变。
- **MODIFIED** `npc verify routing` 的输出在存在 spec 侧 violation 时一并报告，退出码语义不变。
- **新增** 单元测试覆盖每条新规则的正例与反例。

**非目标（Non-Goals）**：

- 不实现 `spine-spec-writer` agent 本身，不新增任何 spec 生成/评审的执行路径。本 change 只立规矩。
- **不为 spec 侧引入 dispatch 配置**。后续 `spine-spec-writer` 定死 spec 生成恒 in-session，因此 `spec_mimo_in_session` 的判定只看 `spec_writer.effective_backend`，无需 per-phase dispatch。
- 除 `gen_not_orthogonal` 补上 codex/codex 形态（修既有漏洞，见 What Changes）外，**不改动既有五条规则的任何其他行为**；不改 `npc verify tests`。
- 不改动任何规则的 `rule` 字符串与 `detail` 语义（下游 telemetry 与测试依赖它们的稳定性）。
- 不引入新的 telemetry event kind。
- 不做跨 change 的路由历史审计。

## Capabilities

- **New Capabilities**: `spec-routing-invariant` —— 把「spec 生成方 ⊥ spec 验证方」与「MiMo 不得用于 spec 验证」编成确定性笼子。

## Impact

- **受影响代码**：`src/npc/verify.py`（`check_routing`）、`src/npc/config.py`（新增两个配置段与 SUPPORTED 常量复用）、`tests/`。
- **兼容性**：spec 侧规则向后兼容——未配置 `[spec_writer]`/`[spec_review]` 的既有仓库解析到安全默认值，零新增 violation。
  `gen_not_orthogonal` 的 codex/codex 修复是**行为收紧**：此前配 `coder.backend=codex` + `review.engine=codex` 的仓库将开始报 violation。这是期望行为（该配置本就违反不变量 1）。本仓库当前配置不受影响（`npc verify routing` 零 violation）。
- **不变量影响**：
  - 不变量 1（生成⊥验证）：**强化**。把既有的 coder/review 约束同构扩展到 spec 层。
  - 不变量 2（不信 LLM 散文）：violation 仍为结构化 `{"rule","detail"}`，无变化。
  - 不变量 3（新硬轨须被真实方差打出来）：本 change 新增的是**对尚不存在的执行路径的前置约束**，而非针对历史缺陷的事后硬轨。其正当性不来自 telemetry 方差，而来自「不变量 1 已是既定核心约束，spec 层是它的未覆盖面」——属于把既有不变量补全，不属于新立硬轨。此区分必须在 review 时明确，不得被误判为违反不变量 3。
