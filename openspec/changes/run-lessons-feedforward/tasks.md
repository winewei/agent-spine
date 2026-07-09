# run-lessons-feedforward — Tasks

## 1. run-lessons-extraction（`npc lessons record`）

- [x] 1.1 新建 `src/npc/lessons.py`：`extract_and_append(p, seq)` 读取 `<base>/events.jsonl`，筛 `event=="fix.done" && phase~=^fix-r\d+$`（真实落盘形态；per-change events.jsonl 用 `event` 字段、`.done` 后缀编码成功退出，不含 `kind`/`status`），按 round 顺序取 `categories_scanned`/`regressions_added`/`notes`
- [x] 1.2 幂等：`lessons.md` 中已存在该 `change_id` 的 `## ` 段落时跳过追加；`state.lessons.entries_appended` 记录已处理 change_id 列表
- [x] 1.3 无 fix 轮（round 0 review 即 blocking==0）的 change 不追加条目；字段为空/`-` 时对应子项省略，全空时仍追加仅含 rounds 数的最简条目
- [x] 1.4 `src/npc/cli.py` 注册 `npc lessons record --seq N`；stdout `{ok, appended: bool, lessons_path, change_id}`；失败（events.jsonl 缺失/损坏）不抛栈，`ok:false` + 结构化 error，best-effort 调用方不阻塞
- [x] 1.5 测试：多轮 fix 条目正确拼接、无 fix 轮不追加、幂等重复调用不重复追加、字段为空的降级格式、events.jsonl 缺失时的错误契约

## 2. run-lessons-injection

- [x] 2.1 `src/npc/templates.py`：`render_implementer` 新增可选 `lessons_path: str | None` 形参，非 None 时在必读输入追加指针 bullet + 限定语（"仅供参考、不改变 tasks/spec 验收标准"）
- [x] 2.2 `src/npc/pipeline.py`：`npc implement run` 渲染前检查 `run_dir/lessons.md` 是否存在且非空，非空才传入 `lessons_path`；不存在/为空时不传，prompt 与现状逐字等价
- [x] 2.3 测试：lessons.md 不存在/为空时 prompt 无新增段落（回归现状快照）；存在且非空时 prompt 含指针 bullet 且不含 lessons 原文内容

## 3. pilot-rewrite-gate（`npc lessons gate` + 复用 spec write 三件套）

- [x] 3.1 `src/npc/lessons.py`：`gate_candidates(p, layer_idx)` 纯函数——候选集 = `progress[].dag_layer > layer_idx && status == "pending"`；`has_candidates` 额外要求 `lessons.md` 存在游标之后的新条目，否则短路 false
- [x] 3.2 `src/npc/state.py`：新增 `lessons` 顶层节 schema（`entries_appended`、`gate_processed_cursor`、`gate_decisions: [{layer_idx, targets, decision, ts}]`）；旧 state 缺该节按空值解释
- [x] 3.3 `src/npc/lessons.py`：`apply_gate_decision(p, layer_idx, targets, decision)`——`decision ∈ {rewrite, skip-rewrite}`；写入 `gate_decisions` 历史并推进 `gate_processed_cursor` 到当前 lessons.md 末尾条目
- [x] 3.4 `src/npc/cli.py` 注册 `npc lessons gate --layer-idx N [--apply --targets <csv> --decision rewrite|skip-rewrite]`
- [x] 3.5 `npc spec write run` 新增可选 `--lessons-path PATH`：存在时在 write prompt 注入独立段落（与 `--goal` 段落并列、不互相覆盖），不改变"生成侧不得预知本轮评判标准"边界；`spec write record` 装订路径零改动（越界拦截天然复用）
- [x] 3.6 测试：候选集判定（层号/status 过滤）、游标去重（同批 lessons 不重复触发）、`gate --apply` 状态落盘正确、`--lessons-path` 注入段落存在性、省略 `--lessons-path` 时 write prompt 与现状逐字等价

## 4. spine-run 编排契约改写

- [x] 4.1 `plugins/agent-spine/commands/spine-run.md` Step 3c：`npc archive run` 成功后紧邻 `npc spec-report render` 补 `npc lessons record --seq $SEQ`（非阻塞，同容错风格）
- [x] 4.2 `plugins/agent-spine/commands/spine-run.md` Step 3 层屏障之后：补 `npc lessons gate --layer-idx $LAYER_IDX` 分支——`has_candidates:false` 直接跳过；交互档 `AskUserQuestion` 摆候选 + lessons 摘要，用户选择后**先**调用 `gate --apply --decision rewrite --targets <csv>` 完成 targets 校验与决策落盘，成功后才对 targets 触发 write 三件套（失败则不触发 write 三件套）；auto 档直接 `gate --apply --decision skip-rewrite`，不调用 `AskUserQuestion`
- [x] 4.3 Guardrails 补：pilot-rewrite-gate 候选集边界（仅 `status==pending`）、auto 档默认不改写、`--lessons-path` 与 `--goal` 段落并列不冲突

## 5. 验证

- [x] 5.1 `openspec validate run-lessons-feedforward --type change --strict` 通过
- [x] 5.2 全量测试通过（`uv run pytest -q`），新增测试覆盖上述各点
