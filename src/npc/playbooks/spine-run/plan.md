# spine-run §plan — 多 change 的真实依赖与整合风险

按需读取：Step 2 中 `|NODES| > 1` 时读本节。

多个 change 时按复杂度选择分析深度；已有可信 DAG 时增量核对，不固定重派分析团队：

1. 简单变更由主 session 直接核对 DAG；复杂目标才 spawn `dag-analyst`（Explore，只读）：按需读变更文档 → 抽 nodes/edges/files（目录级条目用 Grep 展开）→ 跑 `npc plan waves` → 写 `<run_dir>/v3-dag-extract.json` → 回一行 RESULT。校验 nodes 完整、candidate.waves 展平=NODES；失败重发一次，再失败 `--auto` 才降级自抽（记 `dag_extract_fallback`），交互档真停。
2. 仅在耦合复杂或证据冲突时 spawn 架构审查（可并行，只读；`--no-architect` 跳过）：A=senior-system-architect 查语义耦合（共享状态/时序/不变量），B=senior-code-developer 查落地冲突（真实文件/import/构建）。输出具体依赖对及理由：真正需要上游代码/契约的关系放入 edges；必须先后执行的语义约束放入 ordering_edges。共享注册表、同文件不同修改只列为整合风险，不能自动升级成依赖。主 agent 核对证据后裁定，得 FINAL_WAVES 展示计划。

共享注册表可以由 agent 合并不同注册项，只有反复冲突且收益明确时再改成分片结构。
