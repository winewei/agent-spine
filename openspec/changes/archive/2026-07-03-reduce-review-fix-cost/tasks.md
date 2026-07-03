# reduce-review-fix-cost — Tasks

> 本 change 只描述；以下任务为**实施获批后**的落地清单，当前不执行。

## 1. implement-selfcheck-rubric（建议 1，守生成⊥验证）

- [x] 1.1 在 `src/npc/templates.py`（或新常量模块）定义**单一事实源**的静态自检类目清单（validation/partial-failure/locking/test-coverage/edge-case/telemetry/concurrency…），与 review focus 类目同名但仅类目层级
- [x] 1.2 `src/npc/agent.py` implement prompt 组装处注入该 checklist；fix prompt 同样注入
- [x] 1.3 **负向保证**：断言 implement/fix prompt **不**含 `npc focus` 的 per-change 渲染文本 / 上轮 findings 原文（加测试锁死边界，防未来 drift 破坏不变量 1）
- [x] 1.4 测试：prompt 含通用 checklist 类目；prompt 不含 per-change review focus；类目清单单一来源被两处引用

## 2. plan-complexity-gate（建议 2，跨领域信号 + 软门）

- [x] 2.1 `src/npc/plan.py` 前置门计算复杂度信号：主=跨领域广度（touched 顶层模块数 / spec requirement 条目数），辅=文件数；阈值进 `[review]` 配置（`config.py`，带默认值与整数校验）
- [x] 2.2 超阈值输出结构化 warning（`{change_id, breadth, files, suggestion: split|large}`），**不阻断、不自动拆分**
- [x] 2.3 `large` 标记落 change plan-state（`state.py`）；review-fix 上限对 large 读 `[review].max_rounds_large`（`spine-run.md` 3b + npc 侧软上限同步）
- [ ] 2.4 阈值**回测校准**：用 27 个历史 change 跑，确保 ≤3 轮的 22 个不触发、10+ 轮的 2 个触发（`wire-incentive-queue-consumers` / `m1-eval-protocol`）
- [x] 2.5 测试：大跨领域触发告警；大但单领域不误伤（11 文件同模块用例）；软门 run 继续；large 抬高预算生效

## 3. cage-retention-policy（建议 3，保留不删）

- [x] 3.1 `src/npc/telemetry.py` `_CAGE_DEFS` 给 `verify-tests-rerun` 加 `retained: true` + 理由备注（守 coder 谎报 tests pass）
- [x] 3.2 `deletion_candidates` 计算排除 retained 笼子
- [x] 3.3 测试：`npc telemetry cages` 输出中 `verify-tests-rerun` 不再出现在 `deletion_candidates`；其余 `no_data` 笼子行为不变

## 4. 验证与灰度

- [ ] 4.1 checklist 上线后对比 `review-r0` approve 占比与总 `review_rounds`（区分低级遗漏轮 vs 深审轮，见 design D2；深审 change 轮数不应被压缩）
- [ ] 4.2 复杂度门灰度：新 change 触发告警时人/auto-decide 正确读到 split|large 建议
- [ ] 4.3 `/spine-analyze` 复跑，确认 `verify-tests-rerun` 移出删除候选、hotspot 指标可读
