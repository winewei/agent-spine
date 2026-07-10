## Context

`run_archive`（`src/npc/pipeline.py` 985-1264 行）是 archive 一站式的唯一执行路径：precheck（commit chain）→ `openspec validate --strict` → `openspec archive --yes` → `git add` → `git commit` → 状态装订。第 3 步 `openspec archive --yes` 只按 `arc.returncode != 0` 判失败；`openspec archive` 在 abort 场景下把 `"Aborted. No files were changed."` 打到 stdout 却仍 exit 0，当前代码完全不读 `arc.stdout`，直接判"成功"进入下一步。因为归档目录实际没变，`git add` 无事可做，`git commit` 因"nothing to commit"以非零退出失败——用户看到的报错是次生的 `git-commit-failed`，根因（`openspec archive` 静默 abort）被吞掉。

本 change 只在 `openspec archive --yes` 返回之后、`git add` 之前插入一步独立的文件系统状态核验，不改变前后其余步骤的行为。

## Goals / Non-Goals

**Goals**

- `openspec archive --yes` 返回后，仅当既有 `returncode != 0` 判断未短路返回（即 `returncode == 0`）时，才独立核验归档副作用是否真的发生（change 目录消失 + `archive/` 下出现对应目录）——不采信"exit 0 = 已归档"这一散文假设，但也不重新审视 `returncode != 0` 分支本身（那条分支永远输出既有 `openspec-archive-failed`，见 D1/D3）。
- 核验失败时返回单行结构化 JSON，`error="openspec-archive-aborted"`，携带 `stdout_tail`（诊断信息源是 stdout，非 stderr），与既有 5 个失败分支字段命名风格一致。
- 延续 `archive-error-contract` 既有 Requirement，不新建 capability。

**Non-Goals**

- 不改变 `openspec-archive-failed`（非零退出）分支的判定逻辑或字段。
- 不改变 `git add`/`git commit`/`_git_head`/precheck 等其余既有失败分支。
- 不修改 `PHASE_EXIT_EXTRA_CONTRACT`/`PHASE_EXIT_EXTRA_LOCAL_ONLY` 及其 AST 扫描范围。
- 不修改 `openspec` CLI 本身的行为。

## Decisions

**D1：核验时机——插在 `arc.returncode != 0` 判断之后、`git add` 之前。**

若 `arc.returncode != 0` 已短路 return（既有 `openspec-archive-failed` 分支），则不会走到新核验，两者互斥、无需担心重复报错或语义冲突。新核验只在 `returncode == 0` 的路径上触发，确认这条"看似成功"的路径是否真的产生了副作用。

**D2：判定口径——双重确定性检查，两条同时满足才算归档已发生。**

(a) `openspec/changes/<change_id>/` 目录已不存在；(b) `openspec/changes/archive/` 下存在一个命名为 `<date-prefix>-<change_id>` 的目录，其中 `<date-prefix>` **仅由数字与连字符构成**（真实 `openspec archive` 会加日期前缀 `YYYY-MM-DD-<change_id>`）。任一不满足即视为静默 abort，短路返回 `error="openspec-archive-aborted"`，不再执行后续 `git add`/`git commit`。

**目录名匹配规则（round-0 F1 后修订）**：最初本 change 决定用裸后缀匹配 `name.endswith(f"-{change_id}")`，理由是「不假设固定日期前缀字符长度」以容忍非标准日期前缀（如 `2026-1-1-<change_id>`）。**round-0 review F1 推翻了裸 `endswith`**：它对连字符 change_id 不是身份安全的——`change_id="foo"` 会被历史归档 `2026-07-10-add-foo`（以 `-foo` 结尾）误命中，导致 change 实际未归档时误判副作用已发生。

改用正则 `re.compile(r"[0-9][0-9-]*-" + re.escape(change_id)).fullmatch(name)`：前缀仅限数字/连字符 + 边界连字符 + change_id 整体。此规则**同时满足两个曾被认为冲突的约束**：

- 容忍非标准日期前缀：不假设固定 10 字符长度，`2026-1-1-<change_id>` 前缀全是数字/连字符，仍匹配 ✓。
- 消除 suffix 碰撞：`2026-07-10-add-foo` 中 change_id=`foo` 之前的前缀 `2026-07-10-add` 含字母 `add`，fullmatch 失败，故 `change_id="foo"` 不被误命中 ✓。

关键洞察：**碰撞的假前缀必含字母，而日期前缀（标准或非标准）只含数字和连字符**——「前缀仅限数字/连字符」一条即同时排除字母碰撞、放行任意合法日期，无需假设固定长度。

**D3：错误码与既有失败分支区分开。**

新增 `openspec-archive-aborted`，与已有 `openspec-archive-failed`（子进程非零退出）区分：两者是不同失败模态——后者是子进程本身报错，前者是子进程"假成功"（exit 0 但副作用未发生）。诊断字段命名为 `stdout_tail`（区别于其余分支的 `stderr_tail`），如实反映信息源是 `arc.stdout` 而非 `arc.stderr`，避免下游误读去查 stderr。

两个错误码互斥、判定顺序唯一：先判 `returncode`——只要 `returncode != 0`（无论此时副作用是否碰巧已发生），一律走既有 `openspec-archive-failed` 分支，不触发新核验、不可能输出 `openspec-archive-aborted`；只有 `returncode == 0` 时才继续走新核验，核验不通过才输出 `openspec-archive-aborted`。因此不存在"`returncode != 0` 且副作用未发生该输出哪个错误码"的歧义——这种组合下输出的永远是 `openspec-archive-failed`（D1 已确立该判断先于新核验且两者短路互斥）。

**D4：不扩大结构不变量扫描范围。**

`PHASE_EXIT_EXTRA_CONTRACT`/`PHASE_EXIT_EXTRA_LOCAL_ONLY` 的 AST 扫描（`tests/test_structural_invariants.py`）目前只 parametrize `record_implement`/`record_fix`，不覆盖 `run_archive`（`_phase_family` 对 `"archive"` 原样返回，两个字典均无 `"archive"` key）。新增的 `extra` 字段因此不受该结构不变量约束，本 change 不修改这两个字典或其测试。

**D5：不新增 capability，延续 `archive-error-contract`。**

这是同一份"archive 所有失败路径输出单行结构化 JSON"契约在同一函数上的延伸场景，在 `specs/archive-error-contract/spec.md` 里对既有 Requirement 追加一条新 Scenario，而非新建 capability。

**D6：既有成功路径测试需要同步补齐真实副作用，或核验逻辑抽成独立可测函数。**

`test_run_archive_success` 当前用 monkeypatch 把 `openspec archive` 子进程 mock 成 `returncode=0` 但不产生任何真实文件系统副作用（不移动 `openspec/changes/<id>/` 目录）。引入本次核验后该测试会在新步骤处失败，除非同步处理。选择方案：把核验逻辑实现为独立纯函数（输入 `repo_root`/`change_id`，返回布尔或结构化结果），测试层可以单独覆盖"未发生搬迁 → abort"与"发生搬迁 → 放行"两条路径，同时让 `test_run_archive_success` 的 fake 补一步目录搬迁副作用，使其与真实行为一致。这与 `check_chain` 的先例（独立纯函数、返回结构化结果）风格一致，且对现有测试侵入最小。

## Risks / Trade-offs

- **[新增一次文件系统 stat 调用，理论上增加一次 I/O]** → 相比子进程调用本身的开销可忽略不计，且只在 `openspec archive` 返回后触发一次，不引入循环或轮询。
- **[`archive/` 目录匹配依赖 `openspec archive` 的日期前缀命名惯例，若该惯例变化匹配会失效]** → 现状是仓库内 40+ 个既有归档目录均遵循 `<date>-<change_id>` 模式，且用「前缀仅限数字/连字符」的正则匹配（非固定长度切片）已经是对该惯例既宽松（不限日期长度）又身份安全（防 suffix 碰撞）的假设；若未来 `openspec` 命名规则引入字母前缀，需要同步更新匹配逻辑，属已知的外部依赖风险，不在本 change 范围内规避。
- **[既有成功路径测试需要同步改造，若遗漏会导致该测试假阳性通过或误报失败]** → D6 已明确选择独立可测函数的方案，实施时需同步跑通 `test_run_archive_success` 并新增专门覆盖 abort 路径的用例。

## Migration Plan

1. `src/npc/pipeline.py`：`run_archive` 在 `openspec archive --yes` 子进程调用返回、`arc.returncode != 0` 判断之后，新增一次归档副作用核验（独立纯函数，输入 `repo_root`/`change_id`）；核验失败时走 `_do_phase_exit(status="failed", extra={"reason": "openspec-archive-aborted", "stdout": ...})` + `return {"ok": False, "error": "openspec-archive-aborted", "stdout_tail": ...}`，与既有 5 个失败分支同形态。
2. `tests/`：`test_run_archive_success` 补上真实/模拟的目录搬迁副作用；新增覆盖"`openspec archive` exit 0 但目录未搬迁 → `openspec-archive-aborted`"路径的用例，以及新增独立核验函数自身的单元测试。
3. 回滚：删除新增核验步骤调用与 `openspec-archive-aborted` 分支；`test_run_archive_success` 恢复原状（若已改造为需要真实副作用，改回原 mock 形式）；`specs/archive-error-contract/spec.md` 的新增 Scenario 随 change 撤销一并移除。无持久化状态迁移，不涉及 CLI 签名变更。

## Pattern Mapping

（原样誊抄自 `pattern-interrogation.md`；本轮盘问不含 `## User Decisions (Interactive)` 标题，故按无交互裁决分支处理，`## Open Questions` 与 `## Assumptions` 两段全文一并写入本段与下方 `## Assumptions` 段。）

### Open Questions（原文）

- 无。以上假设均可从仓库既有代码、既有目录命名事实（`openspec/changes/archive/YYYY-MM-DD-<name>/`）、以及用户原始目标里的实测描述（stdout 携带 abort 信息、abort 时仍 exit 0）直接推导或验证，暂无需用户拍板的开放问题。若后续 write 轮在落笔 Requirement/Scenario 细节时发现日期前缀格式与观察不一致（例如某些历史目录并非严格 `YYYY-MM-DD-` 前缀），将在 write 轮的 summary 里重新升级为开放问题。

### Assumptions（原文）

- **归档副作用的判定口径**：采用用户描述的双重确定性检查——(a) `p.repo_root / "openspec/changes/<change_id>"` 目录不再存在；(b) `p.repo_root / "openspec/changes/archive/"` 下存在以 `<change_id>` 结尾的目录（因为真实 `openspec archive` 会加日期前缀 `YYYY-MM-DD-<change_id>`，见 `openspec/changes/archive/` 下现存 40+ 个目录的命名模式，以及 `.claude/commands/opsx/archive.md` 第 71/78 行显式写明 `mv openspec/changes/<name> openspec/changes/archive/YYYY-MM-DD-<name>`）。两条同时满足才判定"归档已发生"；任一不满足即视为 abort，不得继续 git 操作。
- **日期前缀匹配用 glob 而非精确 basename**：`archive/` 下目录名不等于 `change_id` 本身，必须用 `archive/*-<change_id>` 之类的后缀匹配（例如 `Path(archive_dir).glob(f"*-{change_id}")` 或对每个子目录做 `name.endswith(f"-{change_id}")`），而不能假设固定日期格式长度（虽然目前观察到的都是 `YYYY-MM-DD-` 10 字符前缀，但把这一细节写死为字符串切片而非 glob/endswith 更脆弱，倾向不假设固定长度）。
- **新增错误码命名**：延续现有蛇形命名惯例（`openspec-validate-failed` / `openspec-archive-failed` / `git-add-failed` / `git-commit-failed`），新增 `openspec-archive-aborted`，与"exit 0 但 returncode 判定为失败"的 `openspec-archive-failed`（非零退出）区分开——两者是不同的失败模态（后者是子进程本身报错，前者是子进程"假成功"）。
- **携带的诊断信息来源于 stdout 而非 stderr**：用户已实测确认 abort 信息（`"Aborted. No files were changed."`）走 `arc.stdout`。因此新分支应读取并截断 `arc.stdout.strip()[:2000]`（对齐现有其余分支 `[:2000]` 落 state / `[-1000:]` 回传的截断惯例），返回字段命名为 `stdout_tail`（区别于其余分支的 `stderr_tail`，因为信息源不同，字段名如实反映来源，避免误导消费方以为要去读 stderr）。
- **校验时机**：在 `arc.returncode != 0` 判断之后、`git add` 之前插入新的确定性校验（即使 `arc.returncode == 0` 也要过这一关）。若 `arc.returncode != 0` 已经短路 return，则不会走到新校验，两者互斥、无需担心重复报错。
- **不新增 capability，复用 `archive-error-contract`**：因为这是同一份"archive 所有失败路径输出单行结构化 JSON"契约在同一函数上的延伸场景，倾向在 `openspec/changes/archive-verify-effect/specs/archive-error-contract/spec.md` 里对既有 Requirement 追加一条新 Scenario（`openspec archive 静默 abort 仍返回 exit 0`），而不是新建一个 capability。
- **不需要改动 `PHASE_EXIT_EXTRA_CONTRACT`/`PHASE_EXIT_EXTRA_LOCAL_ONLY`**：因为 AST 扫描不覆盖 `run_archive`（见 Analogs 第 5 条），新增的 `extra` 字段不会触发 `test_structural_invariants.py` 的现有断言失败；不需要在本 change 里同步修改那两个字典或其测试。
- **测试基础设施需要同步扩展**：`test_run_archive_success` 当前不产生真实目录搬迁副作用，本 change 落地时必须要么（a）在该测试里补一步真实/模拟的目录搬迁（让 fake `openspec archive` 副作用与真实行为一致），要么（b）新增校验函数改为可注入/monkeypatch 的独立函数，测试层单独覆盖"未发生搬迁 → abort" 与 "发生搬迁 → 放行" 两种路径，而不强制所有现有成功路径测试都去真实创建目录结构。倾向后者（独立可测函数，如内部核验辅助函数），因为它对现有测试侵入最小、且与 `check_chain` 的先例（独立纯函数、返回结构化结果）风格一致。

## Assumptions

（与上方 `## Pattern Mapping` 段的 Assumptions 原文一致，见上。Open Questions 原文亦见上方 Pattern Mapping 段。）

## Open Questions

无（见上方 Pattern Mapping 段 Open Questions 原文——盘问阶段已判定暂无需用户拍板的开放问题）。
