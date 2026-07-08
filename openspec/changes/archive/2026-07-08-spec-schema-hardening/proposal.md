## Why

`npc plan check` 在 implement 前判定 change 是否就绪，它**完全委托** `openspec status --change <id> --json`（`src/npc/plan.py:85-103`，自身零文件系统检查）。而 openspec 内置 `spec-driven` schema 的 `apply.requires` **只要求 `tasks`**。

实测后果（openspec 1.1.1，可复现）：一个**没有 `proposal.md`、没有 `design.md`**，只有 `tasks.md` + 一个 spec delta 的 change，同时通过

- `openspec validate <id> --type change --strict` → `is valid`，exit 0
- `npc plan check --change <id>` → `{"ok":true,"ready":true,"apply_requires":["tasks"],"missing":[]}`

即 harness 会照常 implement 一个**从未说明「为什么做」与「改什么」**的 change。这是当前 SDD 流程上游最大的确定性漏洞。

同一批实测还确认：`openspec validate` 只守三件事——delta header 合法、Requirement 正文含 `SHALL`/`MUST`、每条 Requirement 至少一个 `#### Scenario:`。且这三条是 **base validate** 就强制的，`--strict` 在全部探针中未产生任何可观测差异。语义层（`TBD`、含糊副词、只有 happy path、Scenario 正文为纯散文）与 `proposal.md`/`tasks.md`/`design.md` 三个 artifact 完全不设防。

根因位于 openspec 内置 schema 的一行 YAML（`apply: requires: [tasks]`）。openspec 提供 `openspec schema fork <source> [name]` 将 schema fork 到项目本地定制，且 `openspec/config.yaml` 的 `schema:` 键会被自动检测。因此该漏洞可**零 Python 代码**堵上：`npc plan check` 读取的 `openspec status` 本身就是 schema 驱动的。

本 change 同时利用 schema 的 `artifacts[].instruction` 字段——这些字段**就是 openspec 喂给 AI 的 spec 写作 prompt**——把「高质量 spec 写作规范」注入生成时点，而非事后校验。

## What Changes

- **新增** 项目本地 schema `openspec/schemas/agent-spine/`（由 `openspec schema fork spec-driven agent-spine` 生成后定制）。
- **新增** `openspec/config.yaml`，内容 `schema: agent-spine`，使 `openspec status` / `npc plan check` 自动采用项目 schema。
- **MODIFIED（schema 内）** `apply.requires` 由 `[tasks]` 改为 `[proposal, specs, tasks]`，堵上 E-2 漏洞。
- **MODIFIED（schema 内）** `artifacts[].instruction` 注入 change 无关的静态写作规范（延迟决策只许写进 `## Open Questions`、Scenario 必须 WHEN/THEN、禁止含糊副词与实现泄漏、proposal 必须写 Non-Goals）。
- **新增** 回归测试：断言项目 schema 被正确解析、`apply.requires` 生效、缺 `proposal.md` 的 change 被 `npc plan check` 判定 `ready:false`。

**非目标（Non-Goals）**：

- 不修改 `src/npc/plan.py` 或任何 Python 源码——本 change 的全部效力来自 schema + config 两个 YAML 文件。
- 不新增语义层静态校验（`TBD`/含糊副词/Scenario 结构）——schema 的 `instruction` 只作生成时软引导，硬门留给后续 `repo-spec-lint`（仓库本地脚本，不进 npc）。
- 不改变 `openspec validate` 的行为，不向上游 openspec 提 PR。
- 不引入跨 spec 的质量对比或评分。

## Capabilities

- **New Capabilities**: `spec-artifact-gate` —— 在 implement 前确定性判定 openspec change 的 artifact 完备性，并在生成时点提供 change 无关的静态写作规范。

## Impact

- **受影响代码**：无 Python 变更。新增 `openspec/config.yaml`、`openspec/schemas/agent-spine/**`。
- **受影响流程**：`npc plan check`（行为改变：更严）、`/spine-run` Step 2B（自由目标拆解后必须补齐 proposal + specs 才能进 implement）。
- **兼容性**：**BREAKING**（对流程而非 API）。此前只写 `tasks.md` 即可 implement 的 change 将被拒。仓库内现存 active change 数为 0，无迁移负担。
- **不变量影响**：
  - 不变量 1（生成⊥验证）：schema `instruction` 是 **change 无关的静态**生成时引导，与 `SELFCHECK_RUBRIC_MD` 同构，不含任何 per-change review focus / 上轮 findings / reviewer rubric 细则，**不违反**。
  - 不变量 3（新硬轨须被真实方差打出来）：`apply.requires` 收紧由 E-2 的可复现实测漏洞直接支撑，**满足**。
