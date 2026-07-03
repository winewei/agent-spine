## Why

审计 B7（高）：`npc init` 先建 worktree + 分支 + run.json/active.json（src/npc/init_cmd.py:223-271），Step 2 才由主 session 落 plan state（src/npc/state.py:299）。其间崩溃（含主 session 在 init 与 init-run 之间被杀）→ `*-plan-state.json` 不存在 → 续跑扫描（init_cmd.py:159，src/npc/resume.py:28-46）找不到 in-progress run → 判定非续跑 → 再建一个新 worktree，旧 worktree + spine 分支永久孤立；`clean.py` 也不回收 worktree。不变量 2 要求轨迹落盘是唯一真相——这个崩溃窗口里「worktree 已存在」这一事实没有任何可发现的落盘记录。

## What Changes

- `npc init` 在创建 worktree **前**先落可发现的意向记录（或直接在 init 内一并写出 plan-state 骨架：status=initializing、plan_order 空），使「worktree 建好但 plan state 未落」的中间态有落盘真相。
- 下次 `npc init` 的续跑扫描识别该中间态：worktree 完好且无 plan 进度 → **回收复用**（返回该 worktree_root 继续，而非新建）；worktree 已残破 → 标记孤儿。
- `npc clean` 感知孤儿 worktree：列出「有意向记录/骨架但无进度、且非 active」的 worktree，支持安全移除（`git worktree remove` + 删 spine 分支，账目保留）。
- 补崩溃窗口测试：模拟 worktree 建成后进程中断，断言下次 init 不新建、复用旧树；clean 能列出并回收孤儿。

## Capabilities

### New Capabilities

- `init-crash-recovery`: init 崩溃窗口的意向落盘与孤儿 worktree 回收契约。

### Modified Capabilities

## Impact

- `src/npc/init_cmd.py`（建树前意向记录/plan-state 骨架 + 扫描识别中间态）
- `src/npc/resume.py`（扫描逻辑感知 initializing 态）
- `src/npc/clean.py`（孤儿 worktree 列出与回收）
- `tests/`（崩溃窗口 + 回收用例）
