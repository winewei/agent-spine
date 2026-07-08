## 1. 生成并定制项目 schema

- [x] 1.1 在仓库根执行 `openspec schema fork spec-driven agent-spine`，确认产物落在 `openspec/schemas/agent-spine/{schema.yaml,templates/}`
- [x] 1.2 将 `schema.yaml` 的 `apply.requires` 由 `[tasks]` 改为 block 序列 `[proposal, specs, tasks]`（保持 `tracks: tasks.md` 与 `apply.instruction` 不变）
- [x] 1.3 在 `id: proposal` 的 `instruction` 中追加：proposal MUST 含「Non-Goals / 非目标」段落，显式列出本 change 不做什么
- [x] 1.4 在 `id: design` 的 `instruction` 中追加：未决决策 MUST 写入 `## Open Questions` 段落；MUST NOT 以「实施时定 / 届时决定 / TBD / to be determined」等措辞内联于 `## Decisions` 正文
- [x] 1.5 在 `id: specs` 的 `instruction` 中追加：每个 `#### Scenario:` 正文 MUST 含 `WHEN` 与 `THEN` 行；MUST NOT 使用含糊副词（appropriately / quickly / nicely / 适当 / 合理 / 尽快）；MUST NOT 在 spec 中指定具体函数名或文件路径（实现泄漏，属 design.md 职责）
- [x] 1.6 执行 `openspec schema validate agent-spine`，确认 exit 0 且输出含 `is valid`

## 2. 接线 config

- [x] 2.1 新建 `openspec/config.yaml`，内容为 `schema: agent-spine`
- [x] 2.2 验证 `openspec status --change <任一 change> --json`（不传 `--schema`）返回 `.schemaName == "agent-spine"` 且 `.applyRequires` 集合为 `["proposal","specs","tasks"]`

## 3. 回归测试（TDD：先写测试并确认 RED）

- [x] 3.1 新建 `tests/test_spec_artifact_gate.py`；顶部用 `pytest.mark.skipif(shutil.which("openspec") is None, reason="openspec not on PATH")` 保证缺依赖时 **skip 而非 pass**
- [x] 3.1b 写 fixture `minimal_repo(tmp_path)`：在 `tmp_path` 内 `git init`，写 `openspec/project.md`、`openspec/config.yaml`（`schema: agent-spine`），把本仓库的 `openspec/schemas/agent-spine/` 整目录复制进去，再建 `openspec/changes/<id>/`；以 `cwd=tmp_path` 调 `npc plan check`。
      **实证依据**：`npc plan check` 无 `--repo-root` 参数，`_resolve_repo_root` 走 `git rev-parse --show-toplevel`（`src/npc/plan.py`），`openspec status` 从 cwd 发现 `openspec/`。已实测：这样的最小 repo 中 `npc plan check --change <id>` 正常返回 `{"ok":false,"ready":false,"apply_requires":["proposal","specs","tasks"],"missing":["proposal"]}`，exit=1。
      **MUST NOT** 在真实的 `openspec/changes/` 下创建临时 change——那会污染 `openspec list` 并可能被并发的 `npc plan check` 看到。
- [x] 3.2 写测试：临时 change 仅含 `tasks.md` + `specs/<cap>/spec.md`（无 `proposal.md`）→ 断言 `npc plan check` 输出 `.ok == false`、`.ready == false`、`"proposal" in .missing`
- [x] 3.3 写测试：临时 change 含 `proposal.md` + `tasks.md`、`specs/` 缺失 → 断言 `.ready == false` 且 `"specs" in .missing`
- [x] 3.4 写测试：临时 change 三件齐备（无 `design.md`）→ 断言 `.ok == true`、`.ready == true`、`.missing == []`
- [x] 3.5 写测试：临时 change 三件齐备且 `design.md` 的 `## Decisions` 正文内联「实施时定」→ 断言 `.ready == true`（证明本 change 未引入语义层硬门）
- [x] 3.6 写测试：解析 `openspec/schemas/agent-spine/schema.yaml`，断言 `id=="design"` 的 `instruction` 含子串 `Open Questions`，`id=="proposal"` 的 `instruction` 含 Non-Goals 要求
- [x] 3.7 写负向测试（守不变量 1；参照 `tests/` 中 `SELFCHECK_RUBRIC_MD` 既有负向测试的写法）：断言全部 `artifacts[].instruction` 文本中——
      (a) 不含 `openspec/changes/` 下任何 active 或 archived change 的目录名；
      (b) 不含具体的**泄漏形态**，而非裸词：不含 `round-` 与 `.review.json` 相邻的路径记号、不含 `blocking_findings`、不含 `blocking == 0` / `blocking > 0` 形式的阈值、不含 `review focus`、不含 `severity` 与 `critical`/`high` 在同一行共现；
      (c) 不含 `{`/`}`/`$` 引导的模板插值占位符。
      **MUST NOT** 裸 grep `blocking` 或 `severity`——instruction 里若出现「不要写 review blocking findings」这类说明文本会被误伤（此为已识别误报模式）
- [x] 3.8 所有 fixture 均落在 pytest `tmp_path` 内，随 pytest 自动清理；测试结束后断言真实的 `openspec/changes/` 下无新增目录

## 4. 验证 archive 路径不回归

- [x] 4.1 实测：在项目 schema 生效下，对一个完整 change 跑 `openspec validate <id> --type change --strict`，确认仍为 `is valid`
- [x] 4.2 实测：确认 `openspec archive` 在项目 schema 下能正常把 `changes/<id>/specs/` 折进 `openspec/specs/`（在 3.1b 的 `minimal_repo` 里验证，不碰真实仓库）
- [x] 4.3 跑全量 `uv run pytest -q`，确认无既有测试回归（尤其 `npc plan check` 相关测试）

## 5. 收尾

- [x] 5.1 确认 `git status` 中新增文件仅为 `openspec/config.yaml`、`openspec/schemas/agent-spine/**`、`tests/test_spec_artifact_gate.py`
- [x] 5.2 确认**未修改任何 `src/npc/*.py`**（若有修改，说明方案跑偏，回到 design.md D1 复核）
