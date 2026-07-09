## 1. 配置

- [x] 1.1 `config.py`：`ReviewEngineConfig` 新增 `adversarial_round0: bool = True`，`__post_init__` 校验为 `bool`
- [x] 1.2 测试：默认值为 `True`；TOML 显式 `adversarial_round0 = false` 能正确加载；非法类型报 `ConfigError`

## 2. 对抗式 focus 模板

- [x] 2.1 `focus.py` 新增 `_adversarial_round_0_template(change_id: str) -> str`：只指示 `git --no-pager diff HEAD~1..HEAD`，不调用 `load_project_context`，不指示读 proposal/tasks/specs/design/project.md/CLAUDE.md
- [x] 2.2 模板文案含对抗式框架（假设 diff 必有 bug，唯一任务是找出它）与四个重点：资源释放/double-free、边界与符号处理、急切求值/短路语义、并发与生命周期
- [x] 2.3 模板文案含固定指令：无法判断 spec 关系时 `spec_attribution` 填 `spec-silent`；`in_scope` 默认 `true`
- [x] 2.4 输出要求文案改为参数化单一来源（如 `_output_requirements_block(authority_disclaimer: bool)`）：pass1/round-N 变体保持现文案不变；pass2 变体保留 JSON schema 输出要求与 `spec_attribution` 四值语义，但 MUST NOT 含「与 tasks.md / design.md 决策一致的实现不作为 finding 报告」免责条款及任何 tasks.md/design.md 字样（满足 2.5 负向测试）
- [x] 2.5 负向测试：渲染文本 MUST NOT 含 `proposal.md` / `tasks.md` / `specs/` / `design.md` / `openspec/project.md` / `CLAUDE.md` 字样

## 3. findings 合并去重

- [x] 3.1 `review.py` 新增 `merge_review_passes(pass1: dict, pass2: dict) -> tuple[dict, dict]` 纯函数（返回 `(merged, stats)`，`stats` 含合并期间统计的 `adversarial_blocking_count`）
- [x] 3.2 去重规则：`(file, line_range, category)` 精确匹配，pass1 优先保留
- [x] 3.3 合并顺序：pass1 全量（原序）+ pass2 去重后剩余（原序），重新分配 `id` 为 `F1..Fn`
- [x] 3.4 verdict 重算规则：存在 in_scope blocking → `changes-requested`；否则存在任意 finding → `passed-with-advisory`；否则 `approve`
- [x] 3.5 单元测试：无重复场景、有重复场景、pass2 为空 findings（退化为 pass1-only）、pass1 为空 findings、双方均无 findings（verdict=approve）

## 4. pipeline 双 pass 执行

- [x] 4.1 `run_review_round`：`round_n == 0 and review_cfg.adversarial_round0 is True` 时，pass1（既有逻辑，产物改写为 `round-0.review.pass1.json` + `round-0.focus.md`（不变）+ `round-0.events.jsonl`（不变））成功后顺序执行 pass2
- [x] 4.2 pass2 产物：`round-0.adversarial.focus.md` / `round-0.review.pass2.adversarial.json` / `round-0.adversarial.events.jsonl`；复用同一 `selected_engine` 与 `retries`
- [x] 4.3 pass2 成功：调用 `merge_review_passes`，merged 写入既有 `round-0.review.json` 路径，`stats.adversarial_blocking_count` 传给 telemetry，继续既有 `parse_review` 起的流程
- [x] 4.4 pass2 失败（重试耗尽）：不使整轮失败；`merge_review_passes(pass1_data, {"findings": []})` 写入 `round-0.review.json`；标记 `adversarial_pass_ran=false`
- [x] 4.5 `round_n >= 1` 或 `adversarial_round0 is False`：保持现有单 pass 逻辑不变（回归测试覆盖）
- [x] 4.6 集成测试（monkeypatch engine 调用）：双 pass 均成功场景、pass2 失败降级场景、round>=1 只调一次引擎场景、`adversarial_round0=false` 时 round-0 只调一次引擎场景

## 5. telemetry

- [x] 5.1 `EMIT_FIELD_CONTRACT["review.round"]` 新增 `adversarial_pass_ran` / `adversarial_blocking_count`
- [x] 5.2 `emit_review_round` 签名新增两个可选参数并写入 record
- [x] 5.3 `run_review_round` 全部三处 `emit_review_round` 调用点均按 design.md D6 状态矩阵（与 spec.md「telemetry 透出对抗通道运行状态」需求逐字一致）传入两个新字段：情形 1（双 pass 成功）传 `adversarial_pass_ran=True`、`adversarial_blocking_count=<int, >= 0>`；情形 2（pass2 失败降级）、情形 3（pass1 失败）、情形 4（`adversarial_round0=false`）、情形 5（`round_n>=1`）均传 `adversarial_pass_ran=False`（`bool` 字面量）、`adversarial_blocking_count=None`；禁止传 `None` 作为 `adversarial_pass_ran` 的值
- [x] 5.4 `test_structural_invariants.py` 相关断言随字段更新；新增/更新测试逐一覆盖 D6 状态矩阵五种情形（双 pass 成功 / pass2 失败降级 / pass1 失败 / `adversarial_round0=false` / `round_n>=1`），断言每种情形下 `adversarial_pass_ran` 与 `adversarial_blocking_count` 的精确取值与类型（尤其情形 3/4/5 下 `adversarial_pass_ran` 是 `False` 而非 `None`）

## 6. 文档同步

- [x] 6.1 `docs/cli.md` `npc review run` 一节：补充双 pass 行为说明、新产物文件清单、`[review].adversarial_round0` 配置项、两个新 telemetry 字段

## 7. 收尾

- [x] 7.1 `uv run pytest -q` 全量通过，无既有测试回归
- [x] 7.2 `openspec validate review-r0-adversarial-pass --type change --strict` 通过
