## Why

战略维度（审计 B16「多个笼子造好未接入」的反面 + docs/principles.md 不变量 3）：不变量 3 说「硬轨应被 telemetry 指出的真实方差点位打出来……不预先过度设计」，其对偶命题是——**已建的硬轨若跨 run 从未触发，应成为删除候选**（harness 随模型进步做减法）。当前 `/spine-analyze`（plugins/agent-spine/commands/spine-analyze.md）只看 hotspots/agg，找「哪里该加」，没有任何维度回答「哪些笼子从未拦到东西」。stale、max-rounds、timeout-budget、verify routing/tests、auto-decide 各 trigger 的触发次数散在事件流里，无人汇总为减法信号。

## What Changes

- `npc telemetry` 新增子命令（如 `npc telemetry cages --since 90d`）：统计各硬轨跨 run 触发次数——stale 闸门、max-rounds 上限、agent timeout-budget/record-timeout、verify routing violation、verify tests 复跑覆盖、auto-decide 各 trigger 频次；输出单行 JSON（各笼子 name→count），0 次触发的列入 `untriggered` 数组。
- `plugins/agent-spine/commands/spine-analyze.md` 增加「未触发笼子」分析维度：Step 1 加拉取该指标，Step 2/3 把 `untriggered` 中观察窗口足够长（run 数达阈值）的笼子列为**候选删除项**写进优化建议（仍只建议不动手，人审后删）。
- 依赖关系说明：部分笼子的触发事件本身依赖其他 change 先落地（如 decision-telemetry、wire-verify-*）；未 emit 的笼子在输出中标注 `no-data` 与 0 触发区分，不误判。
- 补测试：telemetry cages 统计与 untriggered/no-data 分类。

## Capabilities

### New Capabilities

- `untriggered-cage-analysis`: 跨 run 硬轨触发统计与「0 触发笼子=删除候选」的减法分析契约。

### Modified Capabilities

## Impact

- `src/npc/telemetry.py`（cages 统计子命令）
- `src/npc/cli.py`（子命令注册）
- `plugins/agent-spine/commands/spine-analyze.md`（新增分析维度与建议格式）
- `tests/`（统计与分类用例）
