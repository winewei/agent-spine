## 1. 纯函数骨架（TDD：先测后写）

- [ ] 1.1 写测试（RED）：`strip_code_spans(text)` 剥离 fenced code block（三反引号围栏）与 inline code span（一对反引号），**保持行数与行号对应关系**（被剥离处以等长空白占位）
- [ ] 1.2 写测试（RED）：`section_of_line(lines, i)` 返回第 i 行所属的 `##` 级段落标题（无标题时返回 `None`）
- [ ] 1.3 新建 `scripts/check_spec.py`，实现上述两个纯函数
- [ ] 1.4 跑 1.1–1.2 确认 GREEN

## 2. 严重性与升级判据（本 change 的核心纪律）

- [ ] 2.1 写测试（RED）：一个同时触发全部四条规则的 change → `.errors == []`、`.ok == true`、退出码 `0`（**全部 warning，零阻断门**）
- [ ] 2.2 写测试（RED）：脚本内部产生一条 severity 为 `error` 的 finding 时 → `.ok == false`、退出码 `1`（`errors` 通道语义已定义，供未来升级零改动启用）
- [ ] 2.3 写测试（RED）：`scripts/check_spec.py` 的模块级 docstring 含子串 `正类样本 ≥ 3 个独立 change`
- [ ] 2.4 在脚本模块 docstring 写明升级判据：当 `spec_review.round` 或 code review 的 `spec_attribution` 聚合显示某规则命中与 `spec-silent`/`spec-ambiguous`/`spec-contradicted` 类 blocking 存在跨 change 的稳定关联（正类样本 ≥ 3 个独立 change）时，方可升为 `error`
- [ ] 2.5 跑 2.1–2.3 确认 GREEN

## 3. 延迟决策规则的判定语义（TDD）

- [ ] 3.1 写测试（RED）：`## Decisions` 段含裸露延迟措辞 → `warnings` 含 `deferred_decision_outside_open_questions`，`detail` 含该措辞，`line` 为 1-based 行号
- [ ] 3.2 写测试（RED）：同样的措辞位于 `## Open Questions` 段内 → 该规则 `rule_hits` 为 `0`
- [ ] 3.3 写**误报**测试（RED）：`## Decisions` 段中该措辞被一对反引号包裹 → 该规则 `rule_hits` 为 `0`
- [ ] 3.4 写**误报**测试（RED）：`## Decisions` 段含三反引号围栏块，块内某行含裸露措辞 → 该规则 `rule_hits` 为 `0`
- [ ] 3.5 写**行号**测试（RED）：命中行前方存在多行围栏块 → 对应 `warning` 项的 `line` 等于该行真实的 1-based 行号
- [ ] 3.6 写**词表约束**测试（RED）：`## Decisions` 段含 `接口留到后续 change，届时会有独立的 spec 覆盖`（时间副词）→ `rule_hits` 为 `0`；含 `用 CLI 参数还是 pointer 文件，届时决定`（决策谓语）→ 命中
- [ ] 3.7 写测试：change 目录无 `design.md` → 该规则不命中，退出码 `0`
- [ ] 3.8 实现该规则：先 `strip_code_spans`，再 `section_of_line` 定界，再匹配模块级词表常量；词表 MUST 只含决策谓语短语（`实施时定`/`届时决定`/`届时再定`/`实现时再定`/`后续再定`/`待定`/`暂定`/`后补`/`TBD`/`TODO`/`to be determined`/`decide later`），MUST NOT 含裸的时间副词
- [ ] 3.9 跑 3.1–3.7 确认 GREEN

## 4. 回归 fixture（快照，不引用活体目录）

- [ ] 4.1 把 `openspec/changes/spec-schema-hardening/` 的 `proposal.md`/`design.md`/`tasks.md` 快照进 `tests/fixtures/spec_lint/negative_self_reference/`（其中延迟措辞仅出现在反引号或引号列表内）
- [ ] 4.2 把 `openspec/changes/archive/2026-07-03-parallel-dag-scheduling/design.md` 快照进 `tests/fixtures/spec_lint/positive_long_tail/`
- [ ] 4.3 写测试（RED）：对负例 fixture 跑 `--dir` → 该规则 `rule_hits == 0`
- [ ] 4.4 写测试（RED）：对正例 fixture 跑 `--dir` → 该规则 `rule_hits == 2`（另有 2 处位于 `## Open Questions` 段内，应被放行）且 `.ok == true`
- [ ] 4.5 在 fixture 目录放 `README.md`，注明快照来源与快照时的 git commit，防止后人误以为可以随仓库同步更新
- [ ] 4.6 跑 4.3–4.4 确认 GREEN

## 5. 另外三条规则（TDD）

- [ ] 5.1 写测试（RED）：Scenario `rawText` 为 `It just works, trust me.` → `warnings` 含 `scenario_missing_when_then`，`.ok == true`，退出码 `0`
- [ ] 5.2 写测试（RED）：Requirement 正文 `The system SHALL handle input appropriately and quickly.` + 词表含 `appropriately`/`quickly` → `warnings` 含 `vague_adverb`，退出码 `0`
- [ ] 5.3 写测试（RED）：`proposal.md` 无 `Non-Goals` 亦无 `非目标` 段 → `warnings` 含 `proposal_missing_non_goals`，退出码 `0`
- [ ] 5.4 实现三条规则
- [ ] 5.5 跑 5.1–5.3 确认 GREEN

## 6. 复用 openspec 解析产物（TDD）

- [ ] 6.1 写测试（RED）：spec delta 含 2 个 Scenario（一个 WHEN/THEN、一个纯散文）→ `rule_hits["scenario_missing_when_then"] == 1`
- [ ] 6.2 实现：subprocess 调 `openspec show <id> --json --deltas-only`，**只取 stdout**（deprecation 警告在 stderr），解析 `deltas[].requirement.text` 与 `deltas[].requirement.scenarios[].rawText`
- [ ] 6.3 写测试（RED）：`openspec show` 向 stderr 打印警告时，stdout 仍能被 `json.loads` 成功解析
- [ ] 6.4 写测试（RED）：monkeypatch `shutil.which("openspec")` 返回 `None` → stdout 为合法 JSON，`.ok == false`，含稳定标识 `openspec_missing`，**无未捕获异常**
- [ ] 6.5 写**负向**测试：`rule_hits` 键集合不含任何名称包含 `shall`/`must_keyword`/`normative`/`missing_scenario`/`artifact_exists` 的规则
- [ ] 6.6 跑 6.1–6.5 确认 GREEN

## 7. 入口、路径边界与输出契约（TDD）

- [ ] 7.1 写测试（RED）：`--change` 取值含 `/`（如 `archive/2026-07-03-parallel-dag-scheduling`）→ `.ok == false`，稳定标识 `invalid_change_id`，退出码非零
- [ ] 7.2 写测试（RED）：`--change` 取值为 `../../etc` → `.ok == false`，稳定标识 `invalid_change_id`（拒绝路径穿越）
- [ ] 7.3 写测试（RED）：`--change` 指向不存在的目录 → `.ok == false`，稳定标识 `change_not_found`，退出码非零
- [ ] 7.4 写测试（RED）：`--dir <path>` 模式可直接检查任意含 `design.md` 的目录；该模式下 `scenario_missing_when_then` 与 `vague_adverb` 被跳过并在 `rule_hits` 中记 `0`
- [ ] 7.5 写测试（RED）：干净 change → stdout 单行合法 JSON，`.ok == true`，`.errors == []`，退出码 `0`
- [ ] 7.6 写测试（RED）：`rule_hits` 键集合恒等于全部规则名集合，干净 change 时全部值为 `0`（守减法信号，D7）
- [ ] 7.7 写测试（RED）：`errors[]`/`warnings[]` 每项含 `rule`/`file`/`line`/`detail` 四个键
- [ ] 7.8 写测试（RED）：任一 change 跑脚本 → `errors`/`warnings` 中不含 `file` 位于 `openspec/specs/` 下的项
- [ ] 7.9 实现 argparse 入口（`--change` 与 `--dir` 互斥，二选一必填）与 JSON 输出
- [ ] 7.10 跑 7.1–7.8 确认 GREEN

## 8. 边界守护（本 change 最容易被违反的一组）

- [ ] 8.1 写**负向**测试（RED）：解析 `scripts/check_spec.py` 的 AST import 语句，断言不含任何以 `npc` 开头的模块
- [ ] 8.2 写**负向**测试（RED）：`npc spec --help` 的子命令列表中不存在 `lint`
- [ ] 8.3 断言本 change 未修改 `src/npc/` 下任何文件
- [ ] 8.4 断言词表以模块级常量形式存在于脚本内，**未**引入 `.npc/config.toml` 的读取（D8）
- [ ] 8.5 断言脚本未 emit 任何 telemetry（不 import `npc.telemetry`，不写 `events.ndjson`）
- [ ] 8.6 断言 `plugins/agent-spine/commands/spine-run.md` 未被本 change 修改（未接入闸口）
- [ ] 8.7 断言脚本**未**实现 `npc spec analyze` 已覆盖的检查（`capability-no-spec` / `orphan-spec` / `no-tasks`）——避免两套真相源

## 9. 文档与收尾

- [ ] 9.1 在 `scripts/README.md` 记录 `uv run scripts/check_spec.py` 的契约，并**并列澄清**它与 `npc spec analyze`（artifact 间结构一致性，跨项目通用，在 npc 内）、`npc spec-report`（交付后的 agent 表现回执）三者的职责差异，防止命名混淆
- [ ] 9.2 跑全量 `uv run pytest -q`
- [ ] 9.3 **一次性人工验证**（不进永久测试）：对本仓库全部 active change 跑一遍脚本，确认零误报。若有误报，先修脚本，不修 spec
