# spine-run §recovery — 恢复闸门（禁止先清空集合）

按需读取：`needs_resume=true`、经历过 context compaction、或接手他人 session 时，在任何新派发之前读本节。

先暂停所有新派发和目标工作区写入，读取 `npc status --brief`、完整 `$STATE_JSON`、`$RUN_DIR/scheduler.json`（若存在）和 run.events.jsonl，并重新加载原 DAG 与 v4-waves.json。`npc resume detect` 只供每个 change 的 phase 定位参考，不能据此直接跳进空盘面。

1. 从 state 的 `plan_order`、`isolation.worktree`、`pending_coder`、`prepared`、`publication` 与 scheduler checkpoint 重建任务盘面，核对宿主任务句柄。保存的 worktree/receipt 是恢复依据，不重建、不 stash 后丢弃。
2. 原任务仍运行时重新连接，不能再派同一任务。转录 mtime 只能说明活动，不能证明业务推进；用新增提交、阶段迁移、测试/审查产物核对推进。未知状态先保留占位，核实后再补位。
3. `prepared` 存在 → 重试 `npc integrate --prepared --seq N`；已快进但未归档时会续归档，不重跑 implement/fix。`pending_coder` 存在 → 接回原原生 agent 或核验其 RESULT/manifest；相同 handoff 回执不是新的任务。
4. `npc change run --isolated --seq N` 自动恢复阶段：已完成 fix 不重复执行，已完成 review 按 blocking 进入 fix 或准备发布。存在未装订的提交或脏 worktree 时保留现场，主 agent 按需检查并补齐回执，不丢弃产物从头来。
5. 老版本已整合的 change 可沿用兼容命令完成；新隔离协议必须有可核验的启动目标。旧 run 没记录分支时，不能从当前 checkout 猜原目标。不要混用旧内环和新隔离内环来处理同一 change。
6. FINISHED 只包含确证的终态。DONE 必须有目标分支上的整合证据，不能只看 implement 完成。恢复后检查容量并续闸。
