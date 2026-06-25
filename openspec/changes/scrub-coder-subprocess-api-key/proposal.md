## Why

headless coder 经 `claude -p` 子进程执行，而 `-p` 非交互模式下**只要环境里存在 `ANTHROPIC_API_KEY` 就一定优先用它**，走按 token 计费的付费 API（社区 issue #37686：一个 Max 订阅用户因此两天烧 \$1,800+）。当前 `coder.py` 的 claude 后端以 `env=None` 启动子进程，全盘继承父进程环境——一旦环境里混入 `ANTHROPIC_API_KEY`，premium coder 会被静默翻成付费 API。这是当下就存在的真金白银风险。

## What Changes

- `coder.py` 启动 coder 子进程时，不再 `env=None` 全盘继承，而是显式构造一个 **baseline env = 当前环境剔除 `ANTHROPIC_API_KEY` 与 `ANTHROPIC_AUTH_TOKEN`**。
- claude 后端：用该 scrubbed baseline 启动（从而回退到订阅 OAuth，绝不误用付费 API）。
- mimo 后端：在 scrubbed baseline 之上叠加 `mimo.env`（MiMo 自带的 base_url / key 仍正常生效——MiMo 走第三方计费，不受影响）。
- 补齐单测覆盖三种情形。

## Capabilities

### New Capabilities
- `coder-subprocess-env`: coder 子进程的环境构造契约——剔除 Anthropic 计费凭据，按后端叠加所需 env。

### Modified Capabilities

## Impact

- `src/npc/coder.py`：`_run_backend` / `_mimo_env` / 新增 scrubbed-baseline helper。
- `tests/`：新增 coder 子进程 env 构造测试。
- 行为：无 key 时与现状一致（仍走订阅 OAuth）；有 key 时不再误付 API。
