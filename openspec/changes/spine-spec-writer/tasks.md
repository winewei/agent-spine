## 1. spec review 输出契约（TDD）

- [x] 1.1 写测试（RED）：`SPEC_REVIEW_SCHEMA.properties.findings.items.properties.category.enum` 恰为七值 `["ambiguity","missing-scenario","implementation-leak","untestable","deferred-decision","contradiction","scope-creep"]`；`additionalProperties is False`；`required` 含 `line_range`
- [x] 1.2 写测试（RED）：`category == "style"` 的 finding 用 `jsonschema` 校验失败
- [x] 1.3 写测试（RED）：`SPEC_REVIEW_SCHEMA` 的 finding 必需键**不含** `in_scope`、**不含** `spec_attribution`（与 `REVIEW_SCHEMA` 相互独立）
- [x] 1.4 在 `src/npc/schema.py` 新增 `SPEC_REVIEW_SCHEMA` 与其落盘路径（复用已修好的 `ensure_schema` 语义相等重写逻辑，**不新写一份 write-once**）
- [x] 1.5 跑 1.1–1.3 确认 GREEN

## 2. 质量门顺序（TDD）

- [x] 2.1 写测试（RED）：Requirement 缺 `SHALL`/`MUST` → `npc spec review run` 返回 `.ok == false`、`.gate_failed == "openspec_validate"`，且**未产生 LLM 子进程**（monkeypatch 引擎调用并断言零次），且未写出 `round-0.spec-review.json`
- [x] 2.2 写测试（RED）：gate 命令返回 `ok == false` → `.gate_failed == "gate_cmd"`，LLM 引擎零调用。**MUST 用桩 gate 命令**（直接打印 `{"ok": false, "rule_hits": {}}` 的脚本），MUST NOT 依赖 `scripts/check_spec.py`——后者 v1 全为 warning，恒返回 `ok == true`（D3b2）
- [x] 2.2b 写测试（RED）：`gate_cmd = ["uv","run","scripts/check_spec.py"]` → 实际子进程 argv 恰为 `["uv","run","scripts/check_spec.py","--change","<id>"]`，且 `shell=False`
- [x] 2.2c 写测试（RED）：`gate_cmd` 未配置 → `.gate_skipped == true`、`.gate_failed is None`、LLM 引擎被调用
- [x] 2.2d 写测试（RED）：gate 命令 stdout 为 `not-json` → `.ok == false`、`.gate_failed == "gate_cmd"`、含 `gate_output_invalid`、LLM 零调用
- [x] 2.3 写测试（RED）：gate 命令返回 `ok == true`（仅 warning）→ LLM 引擎被调用一次，`round-0.spec-review.json` 被写出
- [x] 2.4 实现 `npc spec review run` 的门顺序：`openspec validate --strict` → `[spec_review] gate_cmd` → LLM 引擎；npc 只解析 gate 命令 stdout 的 `ok`/`rule_hits`，**不得**持有任何规则内容
- [x] 2.5 写**边界**测试（RED）：`npc spec review run` 实现模块源码不含任何规则名字符串（`deferred_decision_outside_open_questions` / `vague_adverb` / `scenario_missing_when_then` / `proposal_missing_non_goals`），`src/npc/` 下不存在延迟措辞词表或含糊副词表常量
- [x] 2.6 写测试（RED）：`gate_cmd` 未配置 → 跳过该门并在结果中标注，**不静默失败**
- [x] 2.7 跑 2.1–2.6 确认 GREEN

## 3. 评审结果解析（TDD）

- [x] 3.1 写测试（RED）：findings 的 `severity` 为 `critical`/`high`/`medium` → `.blocking == 2`、`.advisory == 1`
- [x] 3.2 写测试（RED）：blocking findings 的 `category` 为 `ambiguity`/`ambiguity`/`untestable` → `.blocking_categories` 集合等于 `{"ambiguity","untestable"}`
- [x] 3.3 写测试（RED）：`round-0.spec-review.json` 已存在时跑 `--round 1` → 新结果写入 `round-1.spec-review.json`，第 0 轮内容未被覆盖，`.pointer.spec_review_json` 以 `round-1.spec-review.json` 结尾
- [x] 3.4 实现纯函数 `parse_spec_review(spec_review_json) -> dict` 与轮次化落盘路径
- [x] 3.5 跑 3.1–3.3 确认 GREEN

## 4. fix 循环终止条件（TDD）

- [x] 4.1 写测试（RED）：第 1 轮 `blocking == 0` → 循环终止、`status == "clean"`、spec fix **未**被调用
- [x] 4.2 写测试（RED）：`max_rounds == 3` 且第 0..3 轮 review 均有 blocking → spec fix 恰被调用 **3** 次、`npc spec review run` 恰被调用 **4** 次、`status == "needs-user-decision"`、**未触发任何 archive 动作**
- [x] 4.2b 写测试（RED）：`max_rounds == 0` 且第 0 轮有 blocking → spec fix **未**被调用，`status == "needs-user-decision"`
- [x] 4.3 写测试（RED）：第 0..3 轮 blocking 为 `2 → 4 → 1 → 3`（反弹）→ 因 **fix 次数上限**终止，`status == "needs-user-decision"`（证明未被 stale 误判）
- [x] 4.4 写**负向**测试（RED）：spec 循环实现模块源码**不含** `rounds_since_strict_decrease`，**未**从 code review 模块导入 stale 判定函数
- [x] 4.5 在 `src/npc/config.py` 的 `[spec_review]` 支持 `max_rounds`（默认 `3`，语义为「最多 N 次 fix」）与 `gate_cmd`（argv 数组，默认未配置）
- [x] 4.6 实现循环；跑 4.1–4.4 确认 GREEN

## 5. RESULT 契约（TDD）

- [x] 5.1 写测试（RED）：`RESULT_REQUIRED_KEYS["spec_write"]` 恰为 `{"change","artifacts","validate","summary"}`；`RESULT_REQUIRED_KEYS["spec_fix"]` 恰为 `{"change","fixed","validate","summary"}`
- [x] 5.2 写测试（RED）：RESULT 行缺 `validate` 键 → `npc spec write record` 返回 `.ok == false`，错误标识指明缺失键名 `validate`
- [x] 5.3 写测试（RED）：完整 RESULT → `.ok == true`
- [x] 5.4 写**回归**测试：`RESULT_REQUIRED_KEYS["implement"]` 仍为 `{"commit","tasks","tests","summary"}`；`["fix"]` 仍为 `{"commit","fixed","tests","summary","categories_scanned","regressions_added"}`
- [x] 5.5 扩 `RESULT_REQUIRED_KEYS`；复用既有 `_parse_and_validate_result_line`，**不另写一套解析**
- [x] 5.6 跑 5.1–5.4 确认 GREEN

## 6. 不变量 1 的渲染层防护（TDD，本 change 最关键的一组）

- [x] 6.1 写**负向**测试（RED）：`npc spec write run` 渲染的 prompt 文件全文**不含** `scope-creep`、**不含** `implementation-leak`、**不含**任何 `round-{N}.spec-review.json` findings 原文
- [x] 6.2 写测试（RED）：`round-0.spec-review.json` 存在含 `ambiguity` 的 blocking → `npc spec fix run --round 1` 渲染的 prompt **含**该 finding 的 `detail` 原文
- [x] 6.3 写**关键**测试（RED）：磁盘同时存在 `round-0.spec-review.json` 与 `round-1.spec-review.json`（findings detail 互不相同）→ `npc spec fix run --round 1` 的 prompt **含第 0 轮** detail、**不含第 1 轮** detail（时点边界，参照 `agent.py:_default_review_path` 恒取 `round_n - 1`）
- [x] 6.3b 写测试（RED）：`round-0.spec-review.json` 不存在时跑 `npc spec fix run --round 1` → `.ok == false`，含稳定标识 `prev_spec_review_missing`（不得静默降级为「无 findings」）
- [x] 6.4 写**跨链负向**测试（RED）：`round-0.spec-review.json` 含 `untestable` finding → `npc implement run` 渲染的 prompt **不含** `untestable`、**不含**该 finding 的 `detail`
- [x] 6.4b 写**跨链负向**测试（RED）：`round-0.review.json` 的 finding 含 `spec_attribution == "spec-ambiguous"` → `npc spec write run` 渲染的 prompt **不含** `spec_attribution`、`spec_attributable_blocking_rate`、四个枚举值、以及该 code finding 的 `detail`
- [x] 6.5 写**跨链负向**测试（RED）：code `round-N.review.json` 存在 → `npc spec write run` 渲染的 prompt **不含**其 findings 原文
- [x] 6.6 实现渲染逻辑（write 轮不注入任何 review 内容；fix 轮只注入 `round_n - 1` 的 blocking findings）
- [x] 6.7 跑 6.1–6.5 确认 GREEN

## 7. telemetry（TDD）

- [x] 7.1 写测试（RED）：monkeypatch `emit_event` 捕获**真实** emit 的事件 → `kind == "spec_review.round"`，键集合等于 `EMIT_FIELD_CONTRACT["spec_review.round"]`
- [x] 7.2 写测试（RED）：gate 门失败 → 事件仍被 emit，`gate_failed == "gate_cmd"`，`verdict is None`（**不得为 `"changes-requested"`**）
- [x] 7.3 写**回归**测试：`EMIT_FIELD_CONTRACT["review.round"]` **不含** `gate_failed`，且仍含 `blocking_categories` 与 `spec_attribution_counts`
- [x] 7.2b 写测试（RED）：`gate_cmd` stdout 为 `{"ok":true,"rule_hits":{"foo_rule":2,"bar_rule":0}}` → emit 事件的 `gate_rule_hits` 恰等于该映射（原样透传）
- [x] 7.4 加 `spec_review.round` 到 `EMIT_FIELD_CONTRACT`，键集合恰为：`proj_key`/`canonical_proj_key`/`run_ts`/`change_seq`/`change_id`/`phase`/`round`/`status`/`duration_ms`/`verdict`/`blocking_count`/`blocking_categories`/`engine`/`retry_count`/`outcome_reason`/`tokens`/`pointer`/`gate_failed`/`gate_skipped`/`gate_rule_hits`；同步 `telemetry_schema_v1.json`
- [x] 7.5 核查 emit 调用点无「算了但没 emit」的漏传
- [x] 7.6 跑 7.1–7.3 确认 GREEN

## 8. subagent 与入口

- [x] 8.1 新建 `plugins/agent-spine/agents/spine-spec-writer.md`：frontmatter（name/description/model/tools）+ 正文列出 `spec_write`/`spec_fix` 两个 phase 的 RESULT 必需键 + 「第一步永远是 Read npc 渲染的 prompt 文件绝对路径」
- [x] 8.2 在其正文明确职责边界：只写 `openspec/changes/<id>/` 下的 artifact，**MUST NOT 运行 `git commit`**、**MUST NOT 修改该目录之外的任何文件**（RESULT 无 `commit` 键，无处安放）
- [x] 8.2b 写测试（RED）：spec writer 改了 `src/npc/cli.py` → `npc spec write record` 返回 `.ok == false`、含 `out_of_scope_changes`、越界路径列表含 `src/npc/cli.py`
- [x] 8.2c 写测试（RED）：spec writer 只改 `openspec/changes/<id>/` → `record` 返回 `.ok == true`
- [x] 8.2d 写测试（RED）：spec writer 产生了 git commit（`HEAD` 变化）→ `record` 返回 `.ok == false`、含 `unexpected_commit`
- [x] 8.2e 实现：`record` 装订前用 `git status --porcelain` 取变更集 + 比对 record 前后 `HEAD`（确定性硬轨，不依赖 prompt 文案）
- [x] 8.3 新建 `plugins/agent-spine/commands/spine-spec.md`：`/spine-spec "<目标>"` 入口，调用 `npc spec write run` → in-session spawn `spine-spec-writer` → `npc spec review run` → fix 循环
- [x] 8.3b 写测试（RED）：`npc spec write run` 恒返回 `.deferred == true`，且含 `.spawn_prompt` 与 `.prompt_file`
- [x] 8.3c 写测试（RED）：`[spec_writer] backend = "mimo"` → `npc spec write run` 返回 `.ok == false`，含 `spec_routing_violation`，violations 列表含 `rule == "spec_mimo_in_session"`，且**未渲染任何 prompt 文件**
- [x] 8.3c2 写测试（RED）：`spec_writer` 与 `spec_review` 同源 → `npc spec write run` 返回 `spec_routing_violation` + `rule == "spec_gen_not_orthogonal"`，LLM 引擎零调用（路由检查先于 prompt 渲染）
- [x] 8.3c3 写**边界**测试（RED）：`src/npc/spec_pipeline.py` 源码不含任何 `SUPPORTED_SPEC_` 前缀常量，不含字符串 `spec_writer_backend_unsupported`（路由真相源唯一，D5c）
- [x] 8.3d 写测试（RED）：`npc agent timeout-budget --change <id> --phase spec_write` 返回 `.ok == true` 且 `.timeout_sec` 为正整数（复用既有四件套）
- [x] 8.4 在 `plugins/agent-spine/.claude-plugin/plugin.json` 注册新 agent 与 command
- [x] 8.5 写测试：`spine-spec-writer.md` 正文含两个 phase 的 RESULT 必需键；含 Read prompt 文件的指令

## 9. 非目标守护（防止实现期越界）

- [x] 9.1 写测试：`plugins/agent-spine/commands/spine-run.md` **不含** `spine-spec-writer` 字样（未接管 Step 2B）
- [x] 9.2 写测试：`npc auto-decide` 的 `VALID_TRIGGERS` **不含**任何以 `spec-` 开头的项
- [x] 9.3 grep 断言代码中不存在基于 `spec_attributable_blocking_rate` 的比较/阈值/分支
- [x] 9.4 断言 `npc implement|fix|review|archive` 的既有输出字段与退出码语义未变（跑既有测试套件）

## 10. 端到端与收尾

- [x] 10.1 端到端 A（`gate_cmd` 失败路径）：`gate_cmd` 指向**桩脚本**（stdout 恒为 `{"ok": false, "rule_hits": {}}`）→ 断言 `npc spec review run` 止于 `.gate_failed == "gate_cmd"`，LLM 引擎零调用。
      **MUST NOT 用 `scripts/check_spec.py` 构造本场景**——它交付时四条规则全为 `warning`，恒返回 `ok == true`（见 `repo-spec-lint` 的 D2），拿它构造失败会写出一个永远失败的测试
- [x] 10.1b 端到端 B（`rule_hits` 透传路径）：`gate_cmd` 指向真实的 `scripts/check_spec.py`，对已归档的 `parallel-dag-scheduling` 语料跑 `npc spec review run` → 断言 `.ok == true`、`.gate_failed is None`、emit 事件的 `gate_rule_hits["deferred_decision_outside_open_questions"] == 2`、且**继续进入 LLM 语义门**
- [x] 10.2 端到端：对一个干净 change 跑完整 `/spine-spec` 流程（可用 mock 引擎），断言 `status == "clean"` 且 `spec_review.round` 事件被 emit
- [x] 10.3 跑 `npc verify routing`，确认 `spec_writer`/`spec_review` 默认配置零 violation
- [x] 10.4 跑全量 `uv run pytest -q`
- [x] 10.5 在 `docs/cli.md` 记录 `npc spec write|fix|review` 契约，并与 `scripts/check_spec.py`（仓库本地 lint）/ `npc spec analyze` / `npc spec-report` 并列澄清职责
