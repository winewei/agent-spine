## 1. RESULT 契约扩展（TDD）

- [x] 1.1 写测试（RED）：`RESULT_REQUIRED_KEYS["spec_interrogate"]` 恰为 `{"change","artifacts","summary"}`
- [x] 1.2 写**回归**测试（RED）：`RESULT_REQUIRED_KEYS["spec_write"]` 仍为 `{"change","artifacts","validate","summary"}`；`["spec_fix"]` 仍为 `{"change","fixed","validate","summary"}`
- [x] 1.3 写测试（RED）：RESULT 行缺 `summary` 键 → `npc spec interrogate record` 返回 `.ok == false`，错误标识指明缺失键名 `summary`
- [x] 1.4 在 `src/npc/pipeline.py` 扩 `RESULT_REQUIRED_KEYS`
- [x] 1.5 跑 1.1–1.3 确认 GREEN

## 2. `npc spec interrogate run`（TDD）

- [x] 2.1 写测试（RED）：`npc spec interrogate run --change <id>` 恒返回 `.ok == true`、`.deferred == true`，含 `.spawn_prompt` 与 `.prompt_file`（`prompt_file` 以 `pattern-interrogation.prompt.md` 结尾）
- [x] 2.2 写测试（RED）：`[spec_writer] backend = "mimo"` → `npc spec interrogate run` 返回 `.ok == false`、含 `spec_routing_violation`，violations 含 `rule == "spec_mimo_in_session"`，且未渲染任何 prompt 文件（复用 `_spec_routing_violations`，不新增第二套白名单）
- [x] 2.3 写测试（RED）：`--goal "<text>"` 透传时，渲染出的 prompt 文件全文含该原文
- [x] 2.4 写**边界**测试（RED）：渲染出的 prompt 文件全文 MUST NOT 含 `scope-creep`、MUST NOT 含 `implementation-leak`、MUST NOT 含任何 `SPEC_REVIEW_SCHEMA.category` 枚举值、MUST NOT 含任何 `round-*.spec-review.json` 原文（不变量 1）
- [x] 2.5 实现 `src/npc/templates.py::render_spec_interrogator`：要求产物 `pattern-interrogation.md` 含 `## Analogs`（文件+函数级引用）、`## Assumptions`、`## Open Questions` 三个 H2 段
- [x] 2.6 实现 `src/npc/spec_pipeline.py::spec_interrogate_run`：routing 检查 → scope marker（`pre_head.interrogate.txt`）→ 渲染 → 返回
- [x] 2.7 注册 `npc spec interrogate run` 到 `src/npc/cli.py`（复用既有 `spec write run` 的参数解析形态：`--change`、可选 `--goal`）
- [x] 2.8 跑 2.1–2.4 确认 GREEN

## 3. `npc spec interrogate record` 与独立 open_questions 计数（TDD）

- [x] 3.1 写测试（RED）：`pattern-interrogation.md` 的 `## Open Questions` 段落含 3 条顶层 `- ` bullet → `.open_questions == 3`（npc 独立解析，非 RESULT 自报字段）
- [x] 3.2 写测试（RED）：`pattern-interrogation.md` 不存在 → `.ok == false`，错误标识 `pattern_interrogation_missing`
- [x] 3.3 写测试（RED）：`pattern-interrogation.md` 存在但不含 `## Open Questions` H2 标题 → `.ok == false`，错误标识 `pattern_interrogation_missing_section`（MUST NOT 静默按 0 处理）
- [x] 3.4 写测试（RED）：`## Open Questions` 段落下的 bullet 数为 0（标题存在、内容为空）→ `.open_questions == 0` 且 `.ok == true`（区分"0 条"与"没写这段"）
- [x] 3.5 写测试（RED）：spec writer 越界改了 `openspec/changes/<id>/` 之外的文件 → `npc spec interrogate record` 返回 `.ok == false`、含 `out_of_scope_changes`（复用 `_scope_guard_violation`）
- [x] 3.6 写测试（RED）：spec writer 产生了 git commit → `record` 返回 `.ok == false`、含 `unexpected_commit`
- [x] 3.7 实现共享辅助函数（`spec_pipeline.py` 内，不 import `scripts/check_spec.py`）：最小等价的 `##` 段落定界逻辑，暴露"给定 H2 标题，返回其段落是否存在 + 段内顶层 `- ` bullet 行数"。`spec_interrogate_record` 用它统计 `## Open Questions` 的 bullet 数；第 4 节的 `spec_write_run` 结构门用它检查三个必需标题是否存在（详见设计 D2/D3）——同一实现，两处调用
- [x] 3.7a 实现 `spec_interrogate_record`：`_parse_and_validate_result_line(result_line, "spec_interrogate")` → scope guard → 调用 3.7 的共享辅助函数统计 `## Open Questions` bullet 数
- [x] 3.8 注册 `npc spec interrogate record` 到 `src/npc/cli.py`
- [x] 3.9 跑 3.1–3.6 确认 GREEN

## 4. `npc spec write run` 的硬前置门（TDD，本 change 最关键的一组）

- [x] 4.1 写测试（RED）：`pattern-interrogation.md` 不存在时执行 `npc spec write run --change <id>` → `.ok == false`，错误标识 `pattern_interrogation_missing`，且未写出任何 `spec-write.prompt.md` 文件
- [x] 4.2 写测试（RED）：`pattern-interrogation.md` 已存在且含全部三个必需 H2 标题（`## Analogs`/`## Assumptions`/`## Open Questions`）时执行 `npc spec write run` → `.ok == true`，行为与本 change 之前完全一致（`.deferred == true`、`.spawn_prompt`、`.prompt_file`）
- [x] 4.2a 写测试（RED）：`pattern-interrogation.md` 存在但缺少 `## Assumptions` 标题（结构缺陷半成品）→ `.ok == false`，错误标识 `pattern_interrogation_missing_section`，且未写出任何 `spec-write.prompt.md` 文件（覆盖 F1：单靠"文件存在"不足以兑现"结构化产物由代码门保证"）
- [x] 4.2b 写**回归**测试（RED）：分别遗漏 `## Analogs`、`## Open Questions` 两个标题时，同样各自返回 `pattern_interrogation_missing_section`（三个必需标题逐一覆盖，不止测一个）
- [x] 4.3 写**回归**测试（RED）：该硬门对分支 A（`--goal` 透传）与分支 B（无 `--goal`，补全既有 change）**同等生效**——两种调用形态在 `pattern-interrogation.md` 缺失时都返回 `pattern_interrogation_missing`
- [x] 4.4 实现：`spec_write_run` 在 routing 检查通过后、渲染 prompt 前，新增 `Path(base / "pattern-interrogation.md").exists()` 检查；文件存在时复用第 3 节实现的 H2 段落定界辅助函数，检查三个必需标题是否齐全，缺失任一即返回 `pattern_interrogation_missing_section`
- [x] 4.5 跑 4.1–4.3（含 4.2a/4.2b）确认 GREEN

## 5. `render_spec_writer` 的 write 轮指令扩写（TDD）

- [x] 5.1 写测试（RED）：渲染出的 write prompt 全文含 `pattern-interrogation.md` 作为必读输入项
- [x] 5.2 写测试（RED）：渲染出的 write prompt 全文含"是否含 `## User Decisions (Interactive)` H2 标题"这一机械判据原文，且**不含**要求 writer 判断某条 Open Question 是否"已被回应"/"resolved"的语义指令措辞
- [x] 5.2a 写测试（RED）：渲染出的 write prompt 全文含"含该标题时把 `## Open Questions` + `## User Decisions (Interactive)` 段原样写入 design.md 的 `## Pattern Mapping` 段"的指令原文
- [x] 5.2b 写测试（RED）：渲染出的 write prompt 全文含"不含该标题时把 `## Open Questions` + `## Assumptions` 段原样写入 design.md 的 `## Pattern Mapping` 与 `## Assumptions` 段"的指令原文
- [x] 5.3 写测试（RED）：渲染出的 write prompt 全文含"多落点清单需先跑确定性搜索命令（`grep`/`rg`/`git grep`）并把命令原文与匹配计数写入 tasks.md 对应段落"的指令原文
- [x] 5.4 写**边界**测试（RED）：扩写后的 write prompt 仍 MUST NOT 含 `scope-creep`/`implementation-leak`/`SPEC_REVIEW_SCHEMA` 枚举值（不变量 1 未被本次扩写破坏——回归既有负向测试）
- [x] 5.5 实现：扩写 `templates.render_spec_writer`
- [x] 5.6 跑 5.1–5.4 确认 GREEN

## 6. `npc spec interrogate decide`（TDD）

- [x] 6.1 写测试（RED）：`pattern-interrogation.md` 不存在 → `npc spec interrogate decide --change <id> --decisions-md "<text>"` 返回 `.ok == false`，错误标识 `pattern_interrogation_missing`
- [x] 6.2 写测试（RED）：正常调用一次 → 文件末尾追加 `## User Decisions (Interactive)` 段，内容恰为传入的 `--decisions-md` 原文；返回 `.ok == true`
- [x] 6.3 写测试（RED）：文件已含 `## User Decisions (Interactive)` 段时再次调用 → `.ok == false`，错误标识 `decisions_already_recorded`，文件内容未被改动（一次性、不覆盖）
- [x] 5.9 写测试（RED）：`interrogate record` 在缺 `## Analogs` / 缺 `## Assumptions` / 缺 `## Open Questions` 三种情形下均返回 `.ok == false` + `pattern_interrogation_missing_section` 且输出缺失标题名；并断言与 `spec write run` 硬门对同一 fixture 给出相同缺失标题集合（同一判据、无分歧）
- [x] 6.4 写**负向**测试（RED）：`decide` 的实现模块不对 `--decisions-md` 的内容做任何解析/改写（原样字符串比对通过）
- [x] 6.5 实现 `spec_interrogate_decide`；注册 `npc spec interrogate decide` 到 `src/npc/cli.py`
- [x] 6.6 跑 6.1–6.4 确认 GREEN

## 7. `scripts/check_spec.py` 第五条规则（TDD）

- [x] 7.1 写测试（RED）：`tasks.md` 某段落含 3 条引用不同文件路径（反引号包裹）的列表项，段内无任何围栏代码块 → `.warnings` 含 `rule == "touchpoint_list_missing_search_command"`；`.ok == true`；退出码 `0`（shadow mode）
- [x] 7.2 写测试（RED）：同一段落但围栏代码块内含 `grep -rn ...` → 该规则 `.rule_hits` 计数为 `0`
- [x] 7.3 写测试（RED）：段落内引用不同文件路径的列表项只有 2 条（未达阈值 3）→ 不命中该规则
- [x] 7.4 写**回归**测试（RED）：`ALL_RULE_NAMES` 长度为 5，含 `touchpoint_list_missing_search_command`；`.rule_hits` 键集合含全部五条规则名（含零命中项）
- [x] 7.5 写**回归**测试（RED）：既有四条规则的行为/severity 不受影响（复用既有 fixture 跑一次，断言其余四条 `rule_hits` 不变）
- [x] 7.6 实现 `RULE_TOUCHPOINT_LIST_MISSING_SEARCH_COMMAND` 与其检测函数（复用 `section_of_line`/`strip_code_spans`）；更新模块 docstring 为"五条规则"
- [x] 7.7 跑 7.1–7.5 确认 GREEN

## 8. `/spine-spec` 命令与 subagent 文档

本节落点由以下确定性搜索枚举得出：

```bash
grep -rn -- "--auto" plugins/agent-spine/commands/spine-spec.md plugins/agent-spine/commands/spine-run.md
grep -rn "RESULT" plugins/agent-spine/agents/spine-spec-writer.md
```

- [x] 8.1 写测试：`plugins/agent-spine/commands/spine-spec.md` 含 `--auto` 标志的判断逻辑段落，且与 `plugins/agent-spine/commands/spine-run.md` 的 `--auto` 语义描述一致（"参数含 `--auto` → 全自主档；否则 → 交互档"）
- [x] 8.2 写测试：`spine-spec.md` 含新增 Step "模式盘问"，且该 Step 在 Step "spec write" 之前
- [x] 8.3 写测试：`spine-spec.md` 交互档分支含调用 `AskUserQuestion` 与 `npc spec interrogate decide` 的说明；auto 档分支明令不调用 `AskUserQuestion`
- [x] 8.4 编辑 `plugins/agent-spine/commands/spine-spec.md`：新增 `--auto` 解析、Step 顺序调整（模式盘问 → spec write → review/fix 循环 → 收尾）
- [x] 8.5 写测试：`plugins/agent-spine/agents/spine-spec-writer.md` 正文列出 `spec_interrogate` phase 的 RESULT 必需键，且描述撰写 `pattern-interrogation.md` 的三段式结构要求
- [x] 8.6 编辑 `plugins/agent-spine/agents/spine-spec-writer.md`

## 9. 非目标守护

本节守护落点由以下确定性搜索枚举得出：

```bash
grep -rn "RESULT_REQUIRED_KEYS" src/npc/
grep -rn "EMIT_FIELD_CONTRACT" src/npc/telemetry.py
grep -rn "def spec_fix_run\|def parse_spec_review" src/npc/spec_pipeline.py
```

- [x] 9.1 写测试：`RESULT_REQUIRED_KEYS["implement"]`/`["fix"]`/`["review"]` 等既有 phase 键集合未被改动
- [x] 9.2 写测试：`npc spec review run`、fix 循环相关函数（`spec_fix_run`/`parse_spec_review`）源码未被本 change 修改（跑既有测试套件全绿即可，不需要新增专门断言）
- [x] 9.3 写测试：`EMIT_FIELD_CONTRACT`、telemetry schema 未新增任何键（本 change 不涉及 telemetry）

## 10. 端到端与收尾

本节涉及文件由以下确定性搜索枚举得出：

```bash
grep -rn "npc spec write\|npc spec fix\|npc spec review" docs/cli.md
```

- [x] 10.1 端到端：对一个全新 change 跑 `interrogate → write` 两轮（mock spine-spec-writer 输出），断言 write 轮在 interrogate 未完成时被拒、完成后成功
- [x] 10.2 端到端：模拟交互档——`interrogate record` 返回 `.open_questions == 2` → 断言编排逻辑会走 `AskUserQuestion` 分支（命令文档描述层面的断言，非运行时集成测试）
- [x] 10.3 跑 `uv run scripts/check_spec.py --change spec-writer-pattern-interrogation`，确认新规则不误报本 change 自身的 artifact
- [x] 10.4 跑全量 `uv run pytest -q`
- [x] 10.5 在 `docs/cli.md` 记录 `npc spec interrogate run|record|decide` 契约，与既有 `npc spec write|fix|review` 并列
