# Tasks — fix-coder-cwd-desync

## 1. Prompt 层

- [x] 1.1 `templates.py`：新增 `cwd_contract_md(repo_root)`（内容见 design D1），`TEMPLATE_VERSION` → 1.2.0
- [x] 1.2 5 个 render 函数（implementer/fixer/spec-interrogator/spec-writer/spec-fixer）在 Runtime Variables 段后注入契约
- [x] 1.3 测试：每个 render 输出含插值契约段且位于「必读输入」前（tests/test_templates*.py 或就近测试文件）

## 2. Record 层

- [x] 2.1 `pipeline.py` implement record：cat-file 门后加 `merge-base --is-ancestor` 门，错误码 `commit-not-on-run-branch`（返回体含 hint）
- [x] 2.2 `pipeline.py` fix record：同 2.1（progress 口径沿用该函数现有 needs-user-decision 惯例）
- [x] 2.3 测试：真实双 checkout fixture（主 repo + linked worktree，commit 落主分支）断言两个 record 均拒绝；HEAD 正常 commit 放行不回归

## 3. Provisioning

- [x] 3.1 `config.py`：`[worktree].provision_cmd`（str，默认 ""）
- [x] 3.2 `init_cmd.py`：worktree add 成功后执行 provision（shlex/无 shell/600s 超时/失败仅 warn），init JSON 加 `provision` 字段
- [x] 3.3 测试：未配置零变化 / `false` 失败不阻塞 / `true` 成功记录（复用 init 既有测试 fixture）

## 4. 文档与 plugin

- [x] 4.1 `plugins/agent-spine/agents/spine-coder.md`、`spine-spec-writer.md`：系统提示补契约摘要（cwd 不可信 + REPO_ROOT 锚定）
- [x] 4.2 `plugins/agent-spine/commands/spine-run.md`：补一句「Agent 子代理 cwd 不可靠，锚定契约由 prompt 承载」说明（不改流程）
- [x] 4.3 `docs/cli.md`：`commit-not-on-run-branch` 错误码 + `[worktree].provision_cmd` 键

## 5. 验证

- [x] 5.1 `uv run pytest -q` 全绿
- [x] 5.2 `openspec validate fix-coder-cwd-desync --type change --strict` 通过
