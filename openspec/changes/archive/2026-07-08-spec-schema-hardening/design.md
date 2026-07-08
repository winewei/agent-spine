## Context

`npc plan check`（`src/npc/plan.py:85-103`, `175-193`）不做任何独立的文件系统检查，它 shell out 到 `openspec status --change <id> --json`，从返回的 `applyRequires` 与 `artifacts[].status` 推导 `ready`。因此 harness 的「implement 前置门」的**实际语义完全由 openspec schema 定义**。

内置 `spec-driven` schema（`/opt/homebrew/lib/node_modules/@fission-ai/openspec/schemas/spec-driven/schema.yaml`）的 `apply` 段为：

```yaml
apply:
  requires:
    - tasks
  tracks: tasks.md
```

实测（openspec 1.1.1）确认：只写 `tasks.md` + spec delta、不写 `proposal.md` 的 change，`npc plan check` 返回 `{"ready":true,"missing":[]}`。

同一 schema 的 `artifacts[].instruction` 字段是 openspec 交给 AI 的 artifact 写作 prompt，内置版本已含 "Use SHALL/MUST... avoid should/may"、"Each scenario: `#### Scenario: <name>` with WHEN/THEN format"，以及 design.md 的 `**Open Questions**: Outstanding decisions or unknowns to resolve` 段落。这些规则**已被写明但从未被强制**。

## Goals / Non-Goals

**Goals**

- 以最小改面堵上「无 proposal 亦可 implement」的确定性漏洞。
- 把 spec 写作规范落到**生成时点**（schema instruction），而非只在事后校验。
- 使规范成为仓库内的单一事实源，可被 diff、可被测试。

**Non-Goals**

- 不改 Python。若本 change 需要改 `src/npc/`，说明方案选错了。
- 不实现语义层硬门（`TBD` / 含糊副词 / Scenario 结构校验）。
- 不向 openspec 上游提 PR（外部依赖，节奏不可控）。
- 不引入 spec 评分或跨 spec 对比。

## Decisions

**D1：fork schema，而非扩 `npc plan check`。**
备选方案是在 `plan.py` 内增加独立的 `proposal.md` 存在性检查。否决理由：`plan check` 的语义应保持「忠实反映 openspec 的就绪判定」这一单一职责；一旦它同时持有自己的一套 artifact 规则，就存在两套可能漂移的真相源。fork schema 把规则放回它本来所属的层，且 `plan check` 无需任何改动即自动生效。**已验证**：`openspec/config.yaml` 写入 `schema: agent-spine` 后，未传 `--schema` 的 `npc plan check` 返回 `{"ready":false,"missing":["proposal"],"apply_requires":["proposal","specs","tasks"]}`。

**D2：schema 选择走 `openspec/config.yaml` 的 `schema:` 键，而非给每处调用加 `--schema` 参数。**
备选方案是在 `plan.py` 的 `openspec status` 调用处补 `--schema agent-spine`。否决理由：`--schema` 需要在每个调用点重复，且 `openspec archive` / `openspec validate` 等其余调用点也需同步，易漏。`config.yaml` 是 openspec 官方的自动检测入口，作用于全部子命令。**已验证**：`status --json` 在 config.yaml 存在时返回 `schemaName: agent-spine`。

**D3：`apply.requires` 取 `[proposal, specs, tasks]`，不含 `design`。**
schema 中 `design` artifact 的 instruction 明确写道「create only if any apply（跨模块 / 新架构模式 / 新外部依赖 / 安全性能迁移复杂度 / 需先做技术决策的歧义）」，即 design.md 本就是条件产物。将其纳入硬门会强迫每个琐碎 change 都写 design.md，制造仪式性噪音。**已验证**：`tasks` artifact 虽声明 `requires: [specs, design]`，但当 `tasks.md` 文件已存在时其 status 直接为 `done`，`requires` 只影响未完成 artifact 的 `ready`/`blocked` 标注，故 design.md 缺失不会阻断 `apply.requires` 的满足。

**D4：instruction 只做软引导，语义层硬门留给后续 change。**
理由是不变量 3：新硬轨须被真实方差打出来。当前仅「延迟决策措辞」有可复现的方差证据（全仓 97 个 `.md` 中仅 `archive/2026-07-03-parallel-dag-scheduling/design.md` 命中该措辞 4 处，而该 change 恰为唯一长尾 `total_rounds=6`，其 r0 blocking 正落在该留白上；精确率 1/1，误报率 96/97≈0）。「Non-Goals 必须存在」「Scenario 必须 WHEN/THEN」「禁含糊副词」三条**无任何方差证据**，因此本 change 只把它们写进 instruction 作为生成引导，硬门与否由后续 `repo-spec-lint`（仓库本地脚本）依观察数据决定。

**D5：instruction 必须是 change 无关的静态文本。**
schema instruction 位于**生成侧**（openspec 用它引导 AI 写 artifact）。按不变量 1，生成侧 MUST NOT 见到 per-change review focus / 上轮 findings 原文 / reviewer rubric 细则。这与 `src/npc/templates.py` 的 `SELFCHECK_RUBRIC_MD` 是同一类物件、同一条边界，故复用其既有约束表述与负向测试模式。

**D6：回归测试的 fixture 是 `tmp_path` 内的最小 git repo，不是真实的 `openspec/changes/`。**
`npc plan check` 没有 `--repo-root` 参数，`_resolve_repo_root` 走 `git rev-parse --show-toplevel`，`openspec status` 从 cwd 发现 `openspec/`。因此 fixture 必须是一个**自带 `openspec/config.yaml` 与 `openspec/schemas/agent-spine/` 的完整最小 repo**——只在 `tmp_path` 里放一个孤立的 change 目录，`openspec` 找不到它。

已实测确认此路可行：在 `tmp_path` 内 `git init` + 复制 schema 目录 + 写 `config.yaml` 后，对一个缺 `proposal.md` 的 change 跑 `npc plan check`，返回 `{"ok":false,"ready":false,"apply_requires":["proposal","specs","tasks"],"missing":["proposal"]}`、exit=1。字段名与本 change 的 Scenario 断言一致。

反面方案「在真实 `openspec/changes/` 下建临时 change 再 teardown」被否决：它会污染 `openspec list`，且测试崩溃时留下残留。

## Risks / Trade-offs

- **[openspec schema 子命令标记为 experimental]** → 每次调用打印 `Note: Schema commands are experimental and may change.`（走 stderr，不污染 stdout JSON）。缓解：schema.yaml 与 config.yaml 均为纯声明式文件，即便 `schema` 子命令变更，只要 `status` 仍读 config.yaml 即不受影响；测试直接断言 `npc plan check` 的最终行为，而非断言 `openspec schema` 的 CLI 输出。
- **[fork 后与上游 schema 漂移]** → 上游 `spec-driven` 后续版本的 instruction 改进不会自动流入。缓解：schema.yaml 入库受版本控制，升级 openspec 后可 diff 上游 `schemas/spec-driven/schema.yaml` 与本地副本，作为例行维护项。
- **[BREAKING：流程收紧]** → 仅写 `tasks.md` 的 change 将被拒。缓解：仓库现存 active change 数为 0（全部已 archive），无迁移负担；`/spine-run` Step 2B 的自由目标拆解本就应产出完整 artifact。
- **[archive 路径未覆盖]** → 本 change 未验证 `openspec archive` 在项目 schema 下的行为。缓解：tasks 中列入显式验证任务；archive 的语义（把 `changes/<id>/specs/` 折进 `openspec/specs/`）不依赖 `apply.requires`，风险低但必须实测而非假定。

## Migration Plan

1. `openspec schema fork spec-driven agent-spine` 生成 `openspec/schemas/agent-spine/`。
2. 定制 `apply.requires` 与 `artifacts[].instruction`。
3. 写入 `openspec/config.yaml`（`schema: agent-spine`）。
4. `openspec schema validate agent-spine` 通过。
5. 回归测试通过后合入。回滚 = 删除 `openspec/config.yaml`（openspec 立即回落到内置 `spec-driven`），无需回滚 schema 目录。

## Open Questions

无。本 change 的两个候选决策点（D1 fork-vs-扩 plan check、D2 config.yaml-vs-`--schema` 参数）均已在本文档定稿，且已通过真实 openspec 二进制实测确认，不存在留待实施时决定的机制。
