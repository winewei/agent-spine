## 1. spine-run.md 编排默认切换

- [x] 1.1 Step 3a：claude 后端默认走 `npc implement run` → 读 `deferred`/`spawn_prompt`/`prompt_file` → `Agent(subagent_type=spine-coder, prompt=spawn_prompt)` → 抽 RESULT → `npc implement record`
- [x] 1.2 Step 3b：fix 循环同款三步（含 round）；mimo/headless 保留一行跑完写法
- [x] 1.3 成本路由表 + guardrails：补 in-session（premium）/ headless（mimo）分发说明与计费理由

## 2. principles.md

- [x] 2.1 不变量 4 增补一句：premium coder 经 in-session subagent 对冲 headless `claude -p` 被切出订阅的风险（in-session 属交互式、官方豁免）

## 3. 一致性校验

- [x] 3.1 文档自洽：spine-run.md 描述的 `deferred` 字段名与 `coder-dispatch-routing` 的指令契约一致（spawn_prompt / prompt_file / deferred）
- [x] 3.2 无残留矛盾表述（旧"默认 claude headless 一行跑完"措辞已更新）
