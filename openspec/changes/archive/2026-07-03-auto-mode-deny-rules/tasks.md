## 1. deny 写入

- [x] 1.1 settings_auth：`--auto` 自备权限时向 `.claude/settings.local.json` 的 `permissions.deny` 追加 `Bash(git push --force*)`、`Bash(git reset --hard*)`、`Edit(.git/**)`
- [x] 1.2 合并语义：与用户已有 deny 取并集、去重、不删不改既有条目；幂等重跑不重复
- [x] 1.3 坏 JSON 不覆盖、写失败不阻塞 init（沿用现有容错路径）

## 2. 文档

- [x] 2.1 spine-run.md Guardrails 自备权限段补充 deny 底线及其「compaction 后仍生效」的原理（权限系统在工具调用时强制，不依赖 context）
- [x] 2.2 docs（design.md 或 principles 相关段）同步

## 3. 测试

- [x] 3.1 空 settings → 三条 deny 写入
- [x] 3.2 用户已有 deny → 并集保留用户条目
- [x] 3.3 重复运行 init --auto → 无重复条目
- [x] 3.4 坏 JSON → 不覆盖、init 不失败
- [x] 3.5 `pytest` 全绿
