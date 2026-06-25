## 1. scrubbed baseline env

- [x] 1.1 在 `src/npc/coder.py` 新增 helper（如 `_scrubbed_base_env()`），返回 `dict(os.environ)` 剔除 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`
- [x] 1.2 `_run_backend` claude 分支：把 `env=None` 改为传 scrubbed baseline（不再全盘继承）
- [x] 1.3 `_mimo_env`：以 scrubbed baseline 为底再 `update(parsed)`，确保 mimo.env 自带凭据正常覆盖、继承的 Anthropic key 被剔除

## 2. 测试

- [x] 2.1 claude 后端：环境含 `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` 时，传给 runner 的 env 不含这两个键，其余键保留
- [x] 2.2 mimo 后端：env = scrubbed baseline + mimo.env 覆盖；mimo.env 里的 `ANTHROPIC_API_KEY` 正常存在
- [x] 2.3 无 key 情形：scrubbed baseline 等价于继承当前环境（除两键外无差异）
- [x] 2.4 运行 `pytest` 全绿（新增 4 测试全通过，存量 failure 均为运行时 session 状态冲突，与本 change 无关）
