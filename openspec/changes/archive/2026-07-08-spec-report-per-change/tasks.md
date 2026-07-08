## 1. 派生渲染核心（`spec_report.py`）

- [x] 1.1 从 `STATE_JSON` 按 `--seq` 定位单个 change 的 `progress` 记录 + phases/commit/categories/blocking_trend
- [x] 1.2 汇总耗时：各 phase `duration_ms` + 总时长
- [x] 1.3 汇总资源：`estimated_tokens_by_backend`——复用 `cost` heuristic 口径，标注估算，不含货币费用
- [x] 1.4 收敛质量：review 轮数、blocking_trend、`one_shot`（首轮 review blocking=0 → true；有 fix → false；缺 review 数据 → null）
- [x] 1.5 返工画像：category 分布 + 每类 fix 轮数
- [x] 1.6 叙事一句话：从该 change 的 `implement.summary.md`/`archive.summary.md` 抽 headline + notes

## 2. 自报核验（C，确定性）

- [x] 2.1 读 coder 自报：每个 `fix-rN` 轮的 `regressions_added`、change 级 `categories_scanned`（来自 fix RESULT / state 记录）
- [x] 2.2 `regressions_added` 对账：以该轮 commit range 的 diff 判定是否触及测试文件（通用启发式：路径/文件名含 test/spec 等）；缺 commit range → unverifiable
- [x] 2.3 `categories_scanned` 对账：与 `categories_seen` 对照（明确以 categories_seen 为观测源）
- [x] 2.4 每条产出 verdict ∈ {ok ✓, warn ⚠, unverifiable}，缺数据不误报为 warn；不扩展到项目特定测试规范

## 3. 三产物落盘（同源）

- [x] 3.1 `spec-report.json`：契约源，字段齐全，落该 change 的 log base（base 缺失走 `_paths.base_for` 兜底）
- [x] 3.2 `spec-report.md`：从派生对象渲染的人读视图，固定指标标题段、行数不超上限、不含 phase summary 原文
- [x] 3.3 emit 一条 `kind=spec.report` telemetry 事件：`common_metrics` 子集 + pointer（report_json/change_seq/change_id/run_ts/proj_key/status），不复制全量报告
- [x] 3.4 `common_metrics` 子集在 md/json/event 三视图取值一致

## 4. CLI + 触发接线

- [x] 4.1 `cli.py` 注册 `npc spec-report render --seq <N>`；archived 成功返回 `ok:true`+三产物 pointer；非法输入（seq 不存在/非 archived/state 损坏）返回 `ok:false`+稳定 error code，不产半成品
- [x] 4.2 `spine-run.md`：archived 成功后新增一步以**非阻塞 wrapper** 调用 `npc spec-report render --seq $SEQ`；即使 `ok:false` 也不回滚 archive
- [x] 4.3 产物落盘/telemetry 写入失败为 best-effort，不阻塞主流程、不污染 stdout JSON（与既有 telemetry 容错语义一致）
- [x] 4.4 `docs/cli.md` 补 `spec-report` 子命令契约

## 5. 测试

- [x] 5.1 单 change archived 后三产物落盘、pointer 路径正确
- [x] 5.2 json 字段齐全（交付/收敛/返工/耗时/estimated_tokens_by_backend/自报核验/叙事）
- [x] 5.3 md 可测约束：含固定指标标题段、行数不超上限、不含 phase summary 原文
- [x] 5.4 C 核验：regressions_added 声明但该轮 diff 无测试文件 → ⚠；对得上 → ✓；缺数据 → unverifiable
- [x] 5.5 `spec.report` telemetry 事件落盘、`common_metrics` 与 json 一致、带 pointer
- [x] 5.6 `one_shot`：首轮 blocking=0 → true；有 fix → false；缺 review 数据 → null
- [x] 5.7 边界：非法 seq → ok:false；非 archived 终态不生成；base 缺失走兜底；同一 seq 重跑幂等（不重复 emit 或明确允许）；不同 seq 并发不串写
- [x] 5.8 产物目录/telemetry 不可写时不阻塞主流程、archive 结果不受影响
- [x] 5.9 `pytest` 全绿
