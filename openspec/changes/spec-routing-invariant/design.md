## Context

`check_routing(cfg)`（`src/npc/verify.py:214`）是不变量 1「生成 ⊥ 验证」与「MiMo 只许执行」在代码层的唯一强制点，当前含五条规则，只覆盖 `cfg.coder` ↔ `cfg.review` 这一对。

`spine-spec-writer`（写 spec）与 spec reviewer（批 spec）构成第二对生成/验证关系。它现在完全不在笼子里。

## Goals / Non-Goals

**Goals**

- 在放 spec agent 进来**之前**，先把「spec 生成方 ⊥ spec 验证方」编成确定性规则。
- 顺手补上 `gen_not_orthogonal` 的 codex/codex 漏洞——它与本 change 是同一个不变量的同一个洞。

**Non-Goals**

- 不实现任何 spec 生成/评审执行路径。
- 不为 spec 侧引入 dispatch 配置。
- 不新增 telemetry event kind。

## Decisions

**D1：`gen_not_orthogonal` 的 codex/codex 修复属于本 change，不另开。**
`SUPPORTED_CODER_BACKENDS = ("claude", "mimo", "codex")` 而 `SUPPORTED_ENGINES = ("codex", "claude")`。同源判定只覆盖 claude/claude（同 bin 同 model）与 mimo/mimo，因此 `coder.backend="codex"` + `review.engine="codex"` 当前静默通过——这正是本 change 要堵的那个洞，只不过发生在既有的那一对上。分成两个 change 会让「同一个不变量的两个缺口」被两次评审、两次实现，且中间态里笼子仍然漏。

这是**行为收紧**：此前该配置零 violation，之后报 `gen_not_orthogonal`。这是期望语义（该配置本就违反不变量 1）。收紧的边界严格限定为：只加一种同源形态，`rule` 字符串与 `detail` 语义不变，其余四条规则的触发条件一律不动。本仓库当前配置不受影响（实测 `npc verify routing` 零 violation）。

**D2：`spec_mimo_in_session` 的判定只看 backend，不看 dispatch。**
既有 `mimo_in_session` 需要遍历 `coder` 的 per-phase dispatch，因为 coder 既可 headless 也可 in-session。spec 生成没有这个自由度——`spine-spec-writer` 定死恒 in-session（见该 change 的 D5c）。因此 `spec_writer.effective_backend == "mimo"` 本身就蕴含「mimo + in-session」，无需 dispatch 配置即可判定。

这带来的直接收益：`SpecWriterConfig` 不必长出 `dispatch` / `phase` 字段，配置面积不膨胀。

**D3：本 change 是 spec 侧路由合法性的唯一裁定者；运行期消费它，不复制它。**
`check_routing` 的 spec 侧规则回答「这份配置合不合法」（配置期，`npc verify routing`）。但用户完全可能不跑 `npc verify routing` 就直接 `/spine-spec`，此时静态笼子形同虚设——所以运行期必须再判一次。

关键在于**运行期判的是同一份判定，而不是自己再写一遍**：`spine-spec-writer` 的 `npc spec write|fix run` MUST 在渲染 prompt 之前调用 `check_routing(cfg)`，命中任一 `spec_` 前缀 violation 即以命令级错误标识 `spec_routing_violation` 拒绝，并原样透出 `rule` / `detail`。它 MUST NOT 持有自己的后端白名单常量。

若不这样约束，`spec_pipeline.py` 里会长出一句「backend 是 mimo 就拒绝」——那是第二个白名单，与 `SUPPORTED_CODER_BACKENDS` 必然漂移。这正是本 change 在修的那个 bug 的形状（`codex` 进了 coder 白名单，`gen_not_orthogonal` 的同源判定却没跟上）。

因此：`spec_mimo_in_session` 是**规则名**，`spec_routing_violation` 是**命令级错误标识**，二者层级不同、不重复，且共用同一个判定函数。

**D4：spec 侧配置复用既有 SUPPORTED 常量，不新建平行常量。**
`SpecWriterConfig` 校验 `backend ∈ SUPPORTED_CODER_BACKENDS`，`SpecReviewConfig` 校验 `engine ∈ SUPPORTED_ENGINES`。新建 `SUPPORTED_SPEC_WRITER_BACKENDS` 会立刻制造第二个会漂移的真相源——而漂移正是本 change 在修的那类 bug（codex 进了 coder 白名单，同源判定却没跟上）。

## Risks / Trade-offs

- **[行为收紧可能打破下游配置]** → 配 codex/codex 的仓库会开始报 violation。缓解：这是期望行为；`rule` 字符串不变，下游 telemetry 与测试不受影响；本仓库实测零 violation。
- **[新增五条规则拉长 `check_routing`]** → 该函数已近 100 行。缓解：spec 侧规则抽为独立辅助函数（与既有 `_check_mimo_in_session` 同构），`check_routing` 只做编排。
- **[安全默认值掩盖未配置状态]** → 未配 `[spec_writer]` 时静默取 `claude`，用户可能误以为已生效。缓解：本 change 不引入执行路径，默认值只影响 violation 判定；真正的执行入口在 `spine-spec-writer`，由它负责暴露实际解析到的后端。

## Migration Plan

1. `src/npc/config.py`：加 `SpecWriterConfig` / `SpecReviewConfig`，复用既有 SUPPORTED 常量。
2. `src/npc/verify.py`：加五条 spec 侧规则的辅助函数；为 `gen_not_orthogonal` 补 codex/codex 形态。
3. 回归：断言既有五条规则的 `rule` 字符串与 `detail` 语义未变；`npc verify routing` 退出码语义未变。
4. 回滚：还原 `verify.py` 与 `config.py` 两个文件。无持久化状态、无 telemetry 变更。

## Open Questions

无。四个决策点（把 codex/codex 修复并入本 change、`spec_mimo_in_session` 只看 backend、配置期与运行期的两层关系、复用 SUPPORTED 常量）均已定稿，各自给出代码级依据。
