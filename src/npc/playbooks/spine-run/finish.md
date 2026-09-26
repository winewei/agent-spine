# spine-run §finish — 收尾与最终汇报

按需读取：FINISHED 覆盖全部 NODES（或中止）时读本节。

## Step 4 — 收尾

先对照原始目标（`$STATE_JSON` 的 `goal`）核对组合结果，把**你的裁定**结构化记录进 finalize——不是写进散文：
覆盖完整 → `--goal-complete`；有缺口 → 每个缺口一条 `--goal-gap "<缺口>"`（逐 change 全过 ≠ 组合达标）。

```bash
npc state finalize --goal-complete   # 或：--goal-gap "<缺口1>" --goal-gap "<缺口2>"
npc summary render && npc index append
# 已核验并 finish/cancel 所有执行体，list --open 为空后停止监控
npc monitor list --open
npc monitor stop
npc cost --since "$RUN_T0"
```

`finalize` 同时原子写出 `$RUN_DIR/result.json`（EngineeringResult：状态、提交、review/测试证据、目标覆盖、job 关联 id）。外部启动方只读它或 `npc result show`，**不需要也不要另写 DELIVERY.md 之类的完成信号**。

中止而非完成（用户叫停、不可恢复的外部阻塞）：`npc run abort --reason "<原因>"`（终态 aborted）；等外部输入（凭据、授权、人工裁定）用 `--blocked`，同 job 的下一个 attempt 会重开同一 run。二者都会写出 result.json，不会被报告为成功。

`finalize` 若因 `needs-user-decision` 返回 exit 1：先把悬而未决的 change 按 3d 的决策分支处理掉再重跑 finalize。收尾汇报对照 run-summary.md 的 Goal Coverage 段提示用户核对缺口（逐 change 全过 ≠ 组合达标），缺口给出新 change 建议清单。耗时汇报区分实测和估算：资源下界之外，关键路径、尾部收尾、限流与组合验证也要计入。

## 最终汇报

```
## Spine Run 完成：<final_status>

**模式**：auto | interactive        **调度**：pipeline（max-parallel N）| serial
**计划**：N changes / L 层           **结果**：archived A / failed F / skipped S
**用时**：<duration>                 **并发峰值**：<max parallel 实际达到>

### 各 change
- change-a  archived @ <commit>  (review 2 轮, 层 1)
- change-b  archived @ <commit>  (review 0 轮, 层 1)
- change-c  skipped — <reason>

### Goal Coverage 缺口
- <run-summary.md 指出的组合缺口与建议新 change>

### 轨迹与日志（供后续分析）
- 结构化结果：<run_dir>/result.json（`npc result show`）
- 状态：<state_json 路径>
- 汇总：<run-summary.md 路径>
- 跨 run 指标：~/task_log/_telemetry/
- 想优化本 harness？跑 `/spine-analyze`
```
