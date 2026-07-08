## 1. 配置层（TDD：先写测试确认 RED）

- [ ] 1.1 在 `tests/test_spec_routing.py` 写测试：未配置 `[spec_writer]`/`[spec_review]` 时，`cfg.spec_writer.effective_backend == "claude"`、`cfg.spec_review.engine == "codex"`（此时应 RED）
- [ ] 1.2 在 `src/npc/config.py` 新增 `SpecWriterConfig` 与 `SpecReviewConfig`，复用既有 `SUPPORTED_CODER_BACKENDS` / `SUPPORTED_ENGINES` 常量，**不新建平行常量**
- [ ] 1.3 `SpecWriterConfig` 暴露 `backend` / `effective_backend` / `bin` / `model`，语义与 `CoderConfig` 同构；`SpecReviewConfig` 暴露 `engine` / `claude_bin` / `claude_model`，语义与 `ReviewConfig` 同构
- [ ] 1.4 在 `Config` 上挂 `spec_writer` / `spec_review` 两个属性，解析 `.npc/config.toml` 的同名段；缺段时回落安全默认值
- [ ] 1.5 跑 1.1 的测试确认 GREEN

## 2. 路由不变量（TDD）

- [ ] 2.1 写测试：非法 `spec_writer.backend = "gpt-9"` → violations 含 `spec_backend_unsupported` 且 `detail` 含 `gpt-9`（RED）
- [ ] 2.2 写测试：非法 `spec_review.engine = "bard"` → violations 含 `spec_engine_unsupported` 且 `detail` 含 `bard`（RED）
- [ ] 2.3 写测试：spec 双方同为 claude 且同 bin 同 model → 恰有一项 `spec_gen_not_orthogonal`（RED）
- [ ] 2.4 写测试：spec 双方同为 mimo → 含 `spec_gen_not_orthogonal`（RED）
- [ ] 2.4b 写测试：spec 双方同为 codex → 含 `spec_gen_not_orthogonal`（RED）
- [ ] 2.5 写**负向**测试：同为 claude 但 model 不同 → 不含 `spec_gen_not_orthogonal`（RED）
- [ ] 2.6 写**负向**测试：默认配置（claude writer / codex review）→ 不含 `spec_gen_not_orthogonal`（RED）
- [ ] 2.7 写测试：`spec_review.engine = "mimo"` → 含 `spec_mimo_exec_only`（RED）
- [ ] 2.8 写测试：`spec_review.engine = "claude"` 且 `claude_model` 含 `mimo` → **恰有一项** `spec_mimo_exec_only`（多条件合并，RED）
- [ ] 2.9 写**负向**测试：`spec_writer` 用 mimo、`spec_review` 用 codex → 既不含 `spec_mimo_exec_only` 也不含 `spec_gen_not_orthogonal`（RED）
- [ ] 2.10 写测试：`spec_writer.effective_backend == "mimo"` → **恰有一项** `spec_mimo_in_session`，且 `detail` 含 `in-session`（spec 生成恒 in-session，故 mimo 必违规，RED）
- [ ] 2.11 写**负向**测试：`spec_writer.effective_backend == "claude"` → 不含 `spec_mimo_in_session`（RED）
- [ ] 2.12 在 `src/npc/verify.py` 的 `check_routing` 中实现**五条** spec 侧规则（`spec_backend_unsupported` / `spec_engine_unsupported` / `spec_gen_not_orthogonal` / `spec_mimo_exec_only` / `spec_mimo_in_session`），实现方式 MUST 与既有五条同构
- [ ] 2.13 更新 `check_routing` 的 docstring，列出新增的五条规则
- [ ] 2.14 跑 2.1–2.11 全部测试确认 GREEN

## 2b. 修既有 codex/codex 同源漏洞

- [ ] 2b.1 写测试（RED）：`coder.effective_backend == "codex"` 且 `review.engine == "codex"` → violations 含 `gen_not_orthogonal`（当前必 RED，这是既有漏洞）
- [ ] 2b.2 在 `gen_not_orthogonal` 的同源判定中补上「双方均为 `codex`」形态。`rule` 字符串与 `detail` 语义 MUST NOT 改变
- [ ] 2b.3 写**回归**测试：既有 claude/claude 同 bin 同 model → 恰有一项 `gen_not_orthogonal`（判定未被新形态污染）
- [ ] 2b.4 写**回归**测试：`review.engine == "mimo"` → 仍含 `mimo_exec_only`
- [ ] 2b.5 写**回归**测试：`coder` 某 phase 为 mimo + in-session → 仍含 `mimo_in_session`，`detail` 语义不变
- [ ] 2b.6 跑 2b.1、2b.3–2b.5 确认 GREEN

## 3. 回归防护

- [ ] 3.1 写测试：coder/review 同源 + spec 段未配置 → 含 `gen_not_orthogonal`，且**不含任何** `rule` 以 `spec_` 开头的项（防止新规则误伤默认配置）
- [ ] 3.2 写测试：两侧同时同源 → 同时含 `gen_not_orthogonal` 与 `spec_gen_not_orthogonal` 两项（证明两对规则彼此独立）
- [ ] 3.3 确认既有 `tests/` 中所有 `check_routing` 相关测试仍 GREEN；既有五条规则的 `rule` 字符串与 `detail` 语义**未被修改**；除 `gen_not_orthogonal` 补 codex/codex 形态外，其余规则的触发条件**未被修改**
- [ ] 3.4 确认 `npc verify routing` 的退出码语义未变（有 violation → 非零；无 violation → 0）

## 4. 收尾

- [ ] 4.1 跑全量 `uv run pytest -q`
- [ ] 4.2 跑 `npc verify routing`，确认本仓库当前配置零 violation
- [ ] 4.3 确认**未新增任何 telemetry event kind**（本 change 非目标）
- [ ] 4.4 确认**未新增任何 spec 生成/评审的执行路径**（本 change 只立规矩，不放人进来）
- [ ] 4.5 确认**未为 spec 侧引入 dispatch 配置**（`SpecWriterConfig` 不含 `dispatch` / `phase` 字段；spec 生成恒 in-session，由 `spine-spec-writer` 定死）
