## 1. 落点确定性枚举

本 change 涉及 ≥2 处调用点/文件（实现 + 现有测试需同步改造），枚举命令与结果如下：

```bash
grep -rn "def run_archive\|def test_run_archive" src/npc/pipeline.py tests/test_pipeline.py
```

匹配计数：11（`src/npc/pipeline.py` 1 处函数定义 + `tests/test_pipeline.py` 10 处以 `test_run_archive` 开头的测试函数）。

```bash
grep -n "openspec-archive-failed\|openspec-validate-failed" src/npc/pipeline.py
```

匹配计数：4（第 1088/1095/1129/1136 行，既有相邻错误分支，标定新分支应插入的既有代码上下文）。

- [x] 1.1 逐条核对上述命令输出，确认以下落点均在本次改动/测试覆盖范围内：
  - `src/npc/pipeline.py::run_archive`（新增核验步骤 + `openspec-archive-aborted` 分支）
  - `tests/test_pipeline.py::test_run_archive_success`（需同步补真实/模拟目录搬迁副作用）
  - `tests/test_pipeline.py` 其余 9 个 `test_run_archive_*` 用例（回归确认不受影响）

## 2. 归档副作用核验辅助函数（TDD）

- [x] 2.1 写测试（RED）：给定 `repo_root`/`change_id`，`openspec/changes/<change_id>/` 已不存在且 `openspec/changes/archive/` 下存在以 `-<change_id>` 结尾的目录 → 核验函数返回"已发生"（真值）
- [x] 2.2 写测试（RED）：`openspec/changes/<change_id>/` 仍存在（未被移动）→ 核验函数返回"未发生"（假值），即使 `archive/` 下恰好存在同名后缀目录（历史遗留）
- [x] 2.3 写测试（RED）：`openspec/changes/<change_id>/` 已不存在，但 `archive/` 下不存在任何以 `-<change_id>` 结尾的目录 → 核验函数返回"未发生"
- [x] 2.4 写测试（RED）：`archive/` 目录本身不存在（边界情况，如全新仓库从未归档过任何 change）→ 核验函数返回"未发生"，不抛异常
- [x] 2.5 写**回归**测试（RED）：目录名匹配不依赖固定日期前缀字符长度——构造一个非标准长度前缀的目录名（如 `2026-1-1-<change_id>`）仍应正确匹配。**注（round-0 F1 修订）**：最初本任务写「用 `endswith(f"-{change_id}")` 或等价 glob」，但 round-0 review F1 证明裸 `endswith` 有 suffix 碰撞（`change_id="foo"` 被 `2026-07-10-add-foo` 误命中）。改用正则 `re.fullmatch(r"[0-9][0-9-]*-" + re.escape(change_id), name)`：前缀仅限数字/连字符即同时（a）容忍非标准日期 `2026-1-1-<id>`（前缀全数字/连字符 → 匹配）、（b）防 suffix 碰撞（碰撞前缀含字母如 `add` → 拒绝）。两个方向都要留回归测试
- [x] 2.6 实现核验辅助函数（`src/npc/pipeline.py` 内，独立可测的纯函数，输入 `repo_root: Path`/`change_id: str`，返回布尔或结构化结果）
- [x] 2.7 跑 2.1–2.5 确认 GREEN

## 3. `run_archive` 集成新核验步骤与 `openspec-archive-aborted` 分支（TDD）

- [x] 3.1 写测试（RED）：`openspec archive --yes` 子进程 `returncode == 0`，但核验函数判定归档未发生（stdout 含 `"Aborted. No files were changed."`）→ `run_archive` 返回 `.ok == false`、`.error == "openspec-archive-aborted"`、`.stdout_tail` 含 abort 原文摘要；exit code 非零；**MUST NOT** 执行后续 `git add`/`git commit`（用 monkeypatch 断言未被调用）
- [x] 3.2 写测试（RED）：核验判定归档未发生时，`_do_phase_exit` 收到的 `extra` 含 `reason == "openspec-archive-aborted"`，`progress_updates.status == "failed"`
- [x] 3.3 写**回归**测试（RED）：`openspec archive --yes` 子进程 `returncode != 0`（既有 `openspec-archive-failed` 分支）时，行为与字段完全不变，且**不会**触发新核验步骤（两分支互斥）
- [x] 3.4 写**回归**测试（RED）：`openspec archive --yes` 成功且核验判定归档已发生（真实/模拟目录搬迁）→ 完整走完 `git add`/`git commit`/状态装订，返回 `.ok == true`，与本 change 之前完全一致
- [x] 3.5 实现：在 `run_archive` 的 `arc.returncode != 0` 判断之后、`git add` 之前调用第 2 节实现的核验函数；核验失败时走 `_do_phase_exit(status="failed", extra={"reason": "openspec-archive-aborted", "stdout": arc.stdout.strip()[:2000]}, progress_updates={"status": "failed", "reason": "openspec-archive-aborted"})`，并 `return {"ok": False, "seq": seq, "change_id": change_id, "error": "openspec-archive-aborted", "stdout_tail": arc.stdout.strip()[-1000:]}`
- [x] 3.6 跑 3.1–3.4 确认 GREEN

## 4. 既有成功路径测试同步改造

- [x] 4.1 改造 `tests/test_pipeline.py::test_run_archive_success`：为 fake 的 `openspec archive` 子进程调用补一步真实/模拟的目录搬迁副作用（把 `openspec/changes/<change_id>/` 移动/重建为 `openspec/changes/archive/<date>-<change_id>/`），使其通过新增的核验步骤
- [x] 4.2 跑 `test_run_archive_success` 确认仍 GREEN（改造后行为与本 change 之前一致：`.ok == true`）
- [x] 4.3 跑第 1 节枚举出的其余 9 个 `test_run_archive_*` 既有用例，确认均不受影响（GREEN，无需改动）

## 5. spec delta 与文档

- [x] 5.1 在 `openspec/changes/archive-verify-effect/specs/archive-error-contract/spec.md` 的 `## MODIFIED Requirements` 下，对既有 Requirement"archive 所有失败路径输出单行结构化 JSON"追加一条新 Scenario："openspec archive 静默 abort 仍返回 exit 0"
- [x] 5.2 跑 `openspec validate archive-verify-effect --type change --strict` 确认通过

## 6. 端到端与收尾

- [x] 6.1 跑全量 `uv run pytest -q`，确认无回归
- [x] 6.2 跑 `uv run pytest tests/test_pipeline.py -k run_archive -v`，确认所有 `run_archive` 相关用例（含新增用例）全绿
