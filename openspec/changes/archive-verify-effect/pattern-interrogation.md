## Analogs

- `src/npc/pipeline.py::run_archive`（第 985-1264 行）—— 本次改动的直接宿主函数。当前四步链路：
  1. `openspec validate <change_id> --strict`（1058-1097 行）：只按 `val.returncode != 0` 判失败，`error="openspec-validate-failed"`。
  2. `openspec archive <change_id> --yes`（1099-1138 行）：只按 `arc.returncode != 0` 判失败，`error="openspec-archive-failed"`。**这里就是缺陷所在**——`openspec archive` 在 abort 场景下打印 `"Aborted. No files were changed."` 到 **stdout**（非 stderr）却仍以 exit 0 返回，当前代码完全不读 `arc.stdout`，直接判定"成功"进入第 3 步。
  3. `git add openspec/`（1140-1165 行）：`check=True` + `CalledProcessError` 捕获，`error="git-add-failed"`。
  4. `git commit -m "chore: archive <change_id>"`（1166-1205 行）：`commit.returncode != 0` 判失败，`error="git-commit-failed"`——这就是用户描述的"倒在 nothing to commit"，对外报出的其实是上游 abort 的次生错误，而非根因。
  第 5 步（1206-1247 行）成功路径计算 `archive_commit` / `total_rounds`，用 `_do_phase_exit(..., status="done", extra={archive_commit, final_status}, progress_updates={status:"archived", ...})` 收尾，并调用 `_telemetry.emit_archive_done`。

- `src/npc/git_chain.py::check_chain` + `is_ancestor`（17-61 行）—— 仓库里**已有的"子进程结果确定性校验"先例**：`check_chain` 不满足于 `git merge-base --is-ancestor` 的 returncode 判定单条 commit，而是对 `progress_entry` 里期望在链上的**每个 commit** 逐一验证真实 git 状态（是否为 HEAD 祖先），返回 `{ok, expected, missing}` 结构。这与本次要做的"不满足于 exit code，转而核对文件系统真实状态（change 目录是否真的从 `openspec/changes/<id>/` 消失并出现在 `openspec/changes/archive/*-<id>/`）"是同一类模式：**子进程返回值不可信，需要独立读一次外部状态源核验副作用是否真的发生**。

- `openspec/specs/archive-error-contract/spec.md` + `openspec/changes/archive/2026-07-03-archive-structured-errors/`（proposal.md + tasks.md）—— 本 change 的**直接前身/姊妹 change**，同一个 `run_archive` 函数、同一种缺陷形态："子进程按 exit code 判定成功/失败，但 exit code 不能完全代表副作用是否发生"。该 change 修的是 `git add` / `_git_head` 的裸异常吞掉问题（转 `emit_error` 结构化 JSON）；本次要修的是**再往前一步**——`openspec archive` 本身 exit 0 但副作用未发生。两者共享同一份「archive 阶段任何失败必须是单行结构化 JSON、不得裸异常/不得误导性归因」的契约精神（`archive-error-contract` Requirement: "archive 所有失败路径输出单行结构化 JSON"），本次应在同一 capability 下新增/修改一条 Requirement + Scenario，而不是另起一个新 capability。

- `src/npc/pipeline.py::cli_archive_run`（1753-1773 行）—— CLI 入口只捕获 `FileNotFoundError` / `subprocess.CalledProcessError` / `ValueError`，其余 `run_archive` 内部已处理的失败路径全部通过其 `dict` 返回值经 `_emit_and_exit(result)` 序列化为单行 JSON。新增的 `openspec-archive-aborted` 错误分支应沿用 `run_archive` 内部 `_do_phase_exit(status="failed", extra=..., progress_updates=...)` + `return {"ok": False, "error": ..., "stderr_tail"/"stdout_tail": ...}` 的既有形态，不需要改动 `cli_archive_run` 的异常捕获面。

- `src/npc/pipeline.py` 第 247-288 行注释区（`PHASE_EXIT_EXTRA_CONTRACT` / `PHASE_EXIT_EXTRA_LOCAL_ONLY`）—— 结构不变量 R1 的"调用点 handoff 契约"，但其 AST 扫描（`tests/test_structural_invariants.py::test_extra_fields_are_all_registered_in_handoff_or_local_only`）只 parametrize 了 `record_implement` / `record_fix` 两个函数名，`run_archive` 不在扫描范围内（`_phase_family` 对非 `implement`/`fix` 直接原样返回 `phase` 字符串，而 `PHASE_EXIT_EXTRA_CONTRACT`/`LOCAL_ONLY` 字典均无 `"archive"` key）。这意味着本次给 `run_archive` 新增的 `extra={"reason": "openspec-archive-aborted", ...}` 字段**不受该结构不变量约束**，但仍应保持与其余 5 个失败分支一致的字段命名风格（`reason` 用于 state phase record，顶层 `return` 用 `error` + `*_tail`）。

- `tests/test_pipeline.py::test_run_archive_success`（768-825 行）—— 现有成功路径测试用 `monkeypatch` 直接把 `openspec archive` 子进程 mock 成 `returncode=0` 且**不产生任何真实文件系统副作用**（不移动 `openspec/changes/<id>/` 目录）。这是一个关键的测试基础设施缺口：一旦引入"归档目录真的消失/出现"的确定性校验，该测试会在新校验步骤处失败，除非同步给 fake 补上目录搬迁副作用（或改为 mock 校验函数本身）。

## Assumptions

- **归档副作用的判定口径**：采用用户描述的双重确定性检查——(a) `p.repo_root / "openspec/changes/<change_id>"` 目录不再存在；(b) `p.repo_root / "openspec/changes/archive/"` 下存在以 `<change_id>` 结尾的目录（因为真实 `openspec archive` 会加日期前缀 `YYYY-MM-DD-<change_id>`，见 `openspec/changes/archive/` 下现存 40+ 个目录的命名模式，以及 `.claude/commands/opsx/archive.md` 第 71/78 行显式写明 `mv openspec/changes/<name> openspec/changes/archive/YYYY-MM-DD-<name>`）。两条同时满足才判定"归档已发生"；任一不满足即视为 abort，不得继续 git 操作。
- **日期前缀匹配用 glob 而非精确 basename**：`archive/` 下目录名不等于 `change_id` 本身，必须用 `archive/*-<change_id>` 之类的后缀匹配（例如 `Path(archive_dir).glob(f"*-{change_id}")` 或对每个子目录做 `name.endswith(f"-{change_id}")`），而不能假设固定日期格式长度（虽然目前观察到的都是 `YYYY-MM-DD-` 10 字符前缀，但把这一细节写死为字符串切片而非 glob/endswith 更脆弱，倾向不假设固定长度）。
- **新增错误码命名**：延续现有蛇形命名惯例（`openspec-validate-failed` / `openspec-archive-failed` / `git-add-failed` / `git-commit-failed`），新增 `openspec-archive-aborted`，与"exit 0 但 returncode 判定为失败"的 `openspec-archive-failed`（非零退出）区分开——两者是不同的失败模态（后者是子进程本身报错，前者是子进程"假成功"）。
- **携带的诊断信息来源于 stdout 而非 stderr**：用户已实测确认 abort 信息（`"Aborted. No files were changed."`）走 `arc.stdout`。因此新分支应读取并截断 `arc.stdout.strip()[:2000]`（对齐现有其余分支 `[:2000]` 落 state / `[-1000:]` 回传的截断惯例），返回字段命名为 `stdout_tail`（区别于其余分支的 `stderr_tail`，因为信息源不同，字段名如实反映来源，避免误导消费方以为要去读 stderr）。
- **校验时机**：在 `arc.returncode != 0` 判断之后、`git add` 之前插入新的确定性校验（即使 `arc.returncode == 0` 也要过这一关）。若 `arc.returncode != 0` 已经短路 return，则不会走到新校验，两者互斥、无需担心重复报错。
- **不新增 capability，复用 `archive-error-contract`**：因为这是同一份"archive 所有失败路径输出单行结构化 JSON"契约在同一函数上的延伸场景，倾向在 `openspec/changes/archive-verify-effect/specs/archive-error-contract/spec.md` 里对既有 Requirement 追加一条新 Scenario（`openspec archive 静默 abort 仍返回 exit 0`），而不是新建一个 capability。
- **不需要改动 `PHASE_EXIT_EXTRA_CONTRACT`/`PHASE_EXIT_EXTRA_LOCAL_ONLY`**：因为 AST 扫描不覆盖 `run_archive`（见 Analogs 第 5 条），新增的 `extra` 字段不会触发 `test_structural_invariants.py` 的现有断言失败；不需要在本 change 里同步修改那两个字典或其测试。
- **测试基础设施需要同步扩展**：`test_run_archive_success` 当前不产生真实目录搬迁副作用，本 change 落地时必须要么（a）在该测试里补一步真实/模拟的目录搬迁（让 fake `openspec archive` 副作用与真实行为一致），要么（b）新增校验函数改为可注入/monkeypatch 的独立函数，测试层单独覆盖"未发生搬迁 → abort" 与 "发生搬迁 → 放行" 两种路径，而不强制所有现有成功路径测试都去真实创建目录结构。倾向后者（独立可测函数，如 `_archive_effect_occurred(repo_root, change_id) -> bool`），因为它对现有测试侵入最小、且与 `check_chain` 的先例（独立纯函数、返回结构化结果）风格一致。

## Open Questions

- 无。以上假设均可从仓库既有代码、既有目录命名事实（`openspec/changes/archive/YYYY-MM-DD-<name>/`）、以及用户原始目标里的实测描述（stdout 携带 abort 信息、abort 时仍 exit 0）直接推导或验证，暂无需用户拍板的开放问题。若后续 write 轮在落笔 Requirement/Scenario 细节时发现日期前缀格式与观察不一致（例如某些历史目录并非严格 `YYYY-MM-DD-` 前缀），将在 write 轮的 summary 里重新升级为开放问题。
