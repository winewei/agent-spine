# review-r0-adversarial-pass

npc review run 在 round 0 增加第二个 diff-only 对抗式评审 pass：新 focus 模板不读 spec 文件、只看 git diff，指令为「假设这段 diff 必有 bug，唯一任务是找出它，重点关注资源释放/double-free、边界与符号处理、急切求值/短路语义、并发与生命周期」；两个 pass 的 findings 合并去重进同一 review.json；round>=1 保持现有单通道。依据 docs/optimization-proposals/2026-07-09-bun-migration-lessons.md 提案 1。
