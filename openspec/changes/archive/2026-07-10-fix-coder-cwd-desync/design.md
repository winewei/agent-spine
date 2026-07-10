# Design — fix-coder-cwd-desync

## D1 CWD_CONTRACT_MD 的内容与注入位置

单一常量，f-string 渲染时用 `{repo_root}` 具体化（不让 agent 自己解析变量名）：

```markdown
## 工作目录契约（最高优先级，违反视为失败）

- 你的 shell 初始 cwd **不可信**：它可能是另一个 checkout（如主仓库），那里有一模一样的文件。本任务唯一合法的工作树是 `<repo_root>`。
- **动手前自检**：先执行 `git -C "<repo_root>" rev-parse --show-toplevel`，输出必须等于 `<repo_root>`；不等（或命令失败）则立即停止，不改任何文件，按失败 RESULT 汇报 `notes=cwd-mismatch`。
- **每个 Bash 调用**都显式锚定：以 `cd "<repo_root>" && …` 开头，或对 git 用 `git -C "<repo_root>" …`。shell cwd 不保证跨调用持久（harness 可能随时重置），绝不依赖上一条命令留下的 cwd。
- Read / Write / Edit 一律使用以 `<repo_root>` 开头的绝对路径；禁止相对路径，禁止其他 checkout 的绝对路径。
- 运行 `git commit` 前最后断言一次 `git -C "<repo_root>" rev-parse --show-toplevel` 输出等于 `<repo_root>`。
```

注入位置：5 个 render 函数中「Runtime Variables」段之后、「必读输入」之前——契约先于一切任务指令。spec-writer/spec-fixer/interrogator 没有 commit 步骤，但读写路径锚定与自检同样适用（它们的「只写 change 目录」边界本身就依赖 cwd 正确）。

实现形态：模块级函数 `cwd_contract_md(repo_root: str) -> str`（非纯常量，因需插值），5 个 render 各调用一次。`TEMPLATE_VERSION` bump 到 `1.2.0`。

## D2 ancestor 门的语义

放在既有 `git cat-file -e`（对象存在性）之后：

```python
ancestor = runner(["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=p.repo_root)
```

- rc==0 → commit 在本 run worktree 的 HEAD 祖先链上（含 HEAD 自身）→ 放行。
- rc!=0 → `commit-not-on-run-branch`：phase_exit failed（implement：`progress=failed/reason=commit-not-on-run-branch`；fix：`needs-user-decision` 同现有 commit-not-found 口径），返回体带 `hint`。
- 多 commit 场景：RESULT 报的是最后一个 commit（=HEAD），is-ancestor 含 HEAD，成立。
- `--no-worktree` 模式：coder 就地提交，commit 必为 HEAD 链上 → 门恒过，零行为变化。
- rebase/amend 边缘：coder 自己 amend 后报新 hash，仍是 HEAD → 成立。

不做「commit 是否在 canonical repo」反向探测——检测到不在本分支即失败，去向留给人/auto-decide 排查（hint 里说明最可能是 cwd 漂移）。

## D3 provision_cmd

- config：`[worktree] provision_cmd = "pnpm install --frozen-lockfile --prefer-offline"`（字符串，默认 `""`）。走 `shlex.split` 后以列表 exec（不经 shell），cwd=新 worktree。
- 执行时机：`git worktree add` 成功之后、init JSON 返回之前。
- 失败语义：**不阻塞**——init JSON 增加 `provision: {ran, ok, rc, cmd, tail}`（未配置时 `{ran: false}`）；rc!=0 时 stderr tail 截 500 字符入 JSON 并打 `[npc:warn]`。理由：provision 只是把「coder 在 worktree 跑不了测试」的概率降下来，不是正确性前提；record 的 rerun-tests 才是硬轨。
- 超时：600s，超时按失败处理（同上不阻塞）。
- resume/`--no-worktree`/per-change worktree：per-change worktree（并行层）同样执行；resume 复用旧 worktree 不重跑。

## D4 为什么不改编排 skill 文档为主修

`spine-run.md` 已写「所有 spawn 必须在 worktree 内执行」，但 Agent 工具没有 cwd 参数、shell cwd 会被 harness 静默重置——编排者侧根本没有可靠手段控制 subagent 的 cwd。可靠的锚只有 prompt 里的绝对路径 + agent 自身的自检。skill 文档只补一句「Agent 子代理 cwd 不可靠，契约由 prompt 承载」的说明。

## Pattern Mapping

- ancestor 门仿既有 `cat-file` 门的结构（同函数、同 phase_exit 口径、同测试组织：`tests/test_pipeline.py` 的 record 失败分支簇）。
- provision 仿 `init_cmd` 里 auto_auth / auto_local_dirs 的「尽力而为、失败入 JSON 不阻塞」惯例。
- CWD_CONTRACT_MD 仿 `ATOMIC_ADD_DISCIPLINE_MD` 的共享常量注入模式。

## Assumptions

- Claude Code 的 Bash 在 cd 到 permissions.additionalDirectories 之外目录时会把 cwd 重置回 primary working directory；Agent 子代理初始 cwd 亦为 primary working directory。两者均为观测行为，不依赖其具体机制——契约按「cwd 完全不可信」设计。
- `git merge-base --is-ancestor` 在共享对象库的 linked worktree 中语义正确（按 HEAD 引用而非对象库判定）。
