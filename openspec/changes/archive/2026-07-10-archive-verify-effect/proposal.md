## Why

`npc archive run` 的归档步骤（`openspec archive <change_id> --yes`）在实测中出现过一种"假成功"：命令因 `openspec` 自身判定当前 change 不满足归档前置条件而 abort（打印 `"Aborted. No files were changed."`），但仍以 `returncode == 0` 退出。当前实现只按退出码判定该步骤成功/失败，完全不读该命令的输出内容，于是直接进入下一步 `git add openspec/`。因为归档目录实际上没有被移动、没有文件变更，`git add` 无事可做，紧随其后的 `git commit` 因"nothing to commit"以非零退出失败。用户看到的最终报错是 `git-commit-failed`，但真正的根因（`openspec archive` 静默 abort）已经被吞掉，误导下游诊断——这与本仓库既有 `archive-error-contract` capability 的核心契约精神冲突："archive 阶段任何失败必须是单行结构化 JSON，且不得误导性归因"。

子进程 exit code 不能完全代表副作用是否真的发生，这不是本 change 首次遇到的模式：`src/npc/git_chain.py::check_chain` 已经确立了"不满足于子进程 returncode，转而核对文件系统/git 真实状态"的先例。本次要把同一模式应用到 `openspec archive` 这一步：在判定"归档已发生"之前，独立核验归档目录是否真的从 `openspec/changes/<id>/` 消失并出现在 `openspec/changes/archive/` 下。

## What Changes

- **修改** `run_archive`（`openspec archive <change_id> --yes` 之后、`git add` 之前新增一步）：**判定顺序**——先看 `returncode`：若 `returncode != 0`，沿用既有 `openspec-archive-failed` 分支短路返回，不触发新核验（两分支互斥，行为不变）；仅当 `returncode == 0` 时，才额外做一次确定性核验——(a) `openspec/changes/<change_id>/` 目录已不存在；(b) `openspec/changes/archive/` 下存在以 `-<change_id>` 结尾的目录。两条同时满足才判定归档真的发生，任一不满足则视为静默 abort（`error="openspec-archive-aborted"`），短路返回，不再执行后续 `git add`/`git commit`。换言之，`returncode != 0` 时永远输出 `openspec-archive-failed`，不会与 `openspec-archive-aborted` 混淆；`openspec-archive-aborted` 只可能出现在 `returncode == 0` 且副作用未发生这一种组合下。
- **新增**错误分类 `openspec-archive-aborted`：与现有 `openspec-archive-failed`（子进程非零退出）区分开——两者是不同的失败模态，前者是"进程报活但副作用未发生"，后者是"进程本身报错"。失败时返回结构化单行 JSON，携带 `error="openspec-archive-aborted"` 与 `stdout_tail`（因为诊断信息在 `openspec archive` 的 stdout 而非 stderr）。
- **不新增 capability**：延续 `archive-error-contract` 既有 Requirement"archive 所有失败路径输出单行结构化 JSON"，给它追加一条新 Scenario，覆盖"`openspec archive` 静默 abort 仍返回 exit 0"这一失败模态。

**非目标（Non-Goals）**：

- 不改变 `openspec-archive-failed`（子进程非零退出）既有分支的行为或字段。
- 不改变 `git add` / `git commit` / `_git_head` 等既有失败分支的判定逻辑或字段命名。
- 不修改 `PHASE_EXIT_EXTRA_CONTRACT` / `PHASE_EXIT_EXTRA_LOCAL_ONLY` 结构不变量扫描范围（`run_archive` 本就不在其覆盖范围内，见 `pattern-interrogation.md` Analogs）。
- 不改变 `openspec archive` 本身的行为——本 change 只在其返回后新增一次独立的文件系统状态核验，不修改 `openspec` CLI。

## Capabilities

### Modified Capabilities

- `archive-error-contract`：在既有 Requirement"archive 所有失败路径输出单行结构化 JSON"下新增一条 Scenario，覆盖 `openspec archive` exit 0 但未发生真实归档副作用（静默 abort）的场景。

## Impact

- **受影响代码**：`src/npc/pipeline.py`（`run_archive` 新增归档副作用核验步骤与 `openspec-archive-aborted` 错误分支）。
- **受影响测试**：`tests/test_pipeline.py`（`test_run_archive_success` 需同步补上真实/模拟的目录搬迁副作用，否则会在新增校验步骤处失败；新增覆盖 abort 场景的用例）。
- **兼容性**：`openspec-archive-failed`（非零退出）与其余既有失败分支字段不变；新分支只在 `returncode == 0` 但副作用未发生时触发，不影响任何现有成功或失败路径的既有行为。
- **不变量影响**：
  - **不变量 2（不信子进程返回码 / 不信散文）**：新增核验步骤独立读取文件系统真实状态，不采信 `openspec archive` 的 `returncode`。
