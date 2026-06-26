# spine 核心不变量（宪法）

本文件固化 agent-spine harness 的**不可违反原则**。改 skill / agent / npc 时，凡与本文冲突者，以本文为准。这些不变量是从生产级自主系统 aidevos 的零信任架构里**蒸馏出的、适配"人驾驭 skill"定位的最小集**——只取便宜且让人的活更轻的，不搬重机器。

定位前提见 README / `docs/design.md`：**spine 是人驾驭的 skill，职责是「从 spec 到结果交付」，不是无人值守的生产系统。**

---

## 不变量 1 — 生成 ⊥ 验证（执行者永不给自己盖章）

写代码的角色，绝不是评判它的角色。

- coder（spine-coder / MiMo 后端）只**生成**；是否合格由**独立的 review**（`npc review run`，codex 或 Claude 引擎）判定。
- review 引擎**绝不可与 coder 同源**。尤其：coder 路由到 MiMo 时，review **必须**仍走 codex/Claude——否则就是"自己评自己"，验证形同虚设。
- archive 闸门只认 review 的 `blocking==0`（或人类显式 override），不认 coder 自报"我写好了"。

> 这是 aidevos 用 HMAC + append-only ledger 守的那条底线的**人可用蒸馏版**：spine 用"独立 review + 人在回路"替代那套重机器，但同一条底线不动摇。

## 不变量 2 — 轨迹与结构化契约是唯一真相，不信 LLM 散文

系统状态以落盘的结构化数据为准，绝不以 LLM 的自然语言自述为准。

- 主 session 只读 npc 子命令返回的**一行 JSON 的关键字段**做分支；不读 prompt 模板 / review.json / summary.md 原文。
- 角色间交接走**结构化契约**：coder→主 session 只回一行 RESULT；npc→主 session 只回 JSON。
- 全轨迹落 `~/task_log/<PROJ_KEY>/` + 跨 run 指标落 `_telemetry/`，是复盘与 `/spine-analyze` 的唯一依据。
- **反模式**：让主 session 去读 summary 原文做决策、把模板搬进 context——这把"智能"退化成"数据搬运"，正是 npc 存在要消灭的。

## 不变量 3 — 确定性"笼子" ∝ 1/(人在回路)

需要多少硬轨，与人参与的程度**成反比**。

- spine 有人驾驭（人即编排者 + 验证锚），所以**笼子最小化**：不照搬 aidevos 的 policy 闸门 / 不可变 ledger / 日历锁。
- 加任何新硬轨前先问："**这是因为去掉人了吗？**" 不是，就别加。
- 硬轨应被 `npc telemetry hotspots` 指出的真实方差点位"打"出来，或在定位真的转向无人值守时才加——不预先过度设计。
- `--auto` 是"少打断人"的便利档，**不等于**把 spine 变成无人产品；人随时可介入。

## 不变量 4 — 成本分层：廉价层只许执行，不许决策与分析

便宜模型（如 MiMo `mimo-v2.5-pro`）**只可用于 coder 层（生成/执行）**，**绝不**用于决策（主 session 编排）与分析/验证（`npc review run`、`/spine-analyze`）——后者恒留 premium（Claude / codex）。

- **premium coder（claude 后端）默认经 in-session subagent 执行**，而非 headless `claude -p` 子进程——这对冲了 headless `claude -p` 被切出订阅的计费风险（in-session Task 工具 subagent 属交互式、官方豁免）。MiMo（廉价层）恒走 headless，不受此约束。
- **MiMo 默认不启用**（MiMo 较慢，按需开）。开启方式（`[coder]` 配置，显式）：
  - 全局 `[coder].backend = "mimo"`；或
  - **per-phase** `[coder.phase].fix = "mimo"`（如只把 fix 给 MiMo，implement 仍 claude）；或
  - 临时 `npc implement/fix run --backend mimo`。
  - 无配置 → 默认 `claude`。`~/.config/npc/mimo.env` 是否存在**不再**自动触发路由。
- 不变量约束（由 `npc verify routing` 在代码层强制）：review 永不与 coder 同源；review 引擎/bin/model 含 mimo 即 violation；mimo + in-session 亦是 violation。
- MiMo 密钥存仓库外 `~/.config/npc/mimo.env`（chmod 600，绝不入 git）；backend=mimo 时由 `npc implement/fix run` 注入到子进程 env。

---

## Roadmap（设计了、暂不做——遵不变量 3 的"按需加"）

- **复跑测试硬轨**：`npc record` 由 npc 真实复跑测试、不裸信 RESULT 的 `tests=pass`。仅在 spine 真去掉人、走无人值守时才升级为必需。详见 `docs/optimization-proposals/2026-06-22.md`。
- **风险分级人在回路**：把 `--auto`/交互二元开关细化为按 change 爆炸半径决定"哪里问人"（aidevos risk→execution-mode 的轻量版）。
- **fix 阶段成本升级**：fix 早期轮用 MiMo，连续 stale / 反复失败时自动升级到 Claude coder（成本感知 + 质量兜底）。
