# coder-atomic-add-discipline

为 coder 加原子 add 纪律：spine-coder agent 契约（plugins/agent-spine/agents/spine-coder.md）Guardrails 增加「只 git add 自己明确改动的文件；禁止 git add -A / git add . / git stash / 任何破坏性 git 操作」，并要求 commit 的文件清单必须与 summary.md 里逐文件改动清单一致（自报一致性，可被 reviewer 核验）。同时在 implement/fix prompt 模板（src/npc/templates.py）中同步该纪律。依据 docs/optimization-proposals/2026-07-09-bun-migration-lessons.md 提案 4（Bun false start #1：并行 worker 互踩 git 状态）。
