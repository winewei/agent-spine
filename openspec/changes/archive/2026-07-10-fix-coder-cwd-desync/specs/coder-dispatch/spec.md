# coder-dispatch — cwd 漂移防御

## ADDED Requirements

### Requirement: Agent prompt 必须携带工作目录契约

npc 渲染的全部 agent prompt（implementer / fixer / spec-interrogator / spec-writer / spec-fixer）SHALL 在 Runtime Variables 段之后包含以具体 `REPO_ROOT` 值插值的工作目录契约段，要求 agent：初始 cwd 视为不可信；动手前以 `git -C "<REPO_ROOT>" rev-parse --show-toplevel` 自检；每个 Bash 调用显式锚定 `REPO_ROOT`；文件工具只用 `REPO_ROOT` 前缀绝对路径；自检失败时不修改任何文件并以失败 RESULT（`notes=cwd-mismatch`）结束；含 commit 步骤的 prompt 还须在 commit 前重复断言。

#### Scenario: implementer prompt 含插值后的契约

- GIVEN `repo_root=/tmp/wt-x`
- WHEN 调用 `render_implementer(...)`
- THEN 输出包含 `## 工作目录契约` 标题、字面 `/tmp/wt-x` 的 `rev-parse --show-toplevel` 自检指令、`notes=cwd-mismatch` 失败口径，且契约段出现在「必读输入」之前

#### Scenario: 全部 5 个模板一致注入

- WHEN 分别调用 5 个 render 函数（相同 `repo_root`）
- THEN 每个输出都恰好包含一段 `## 工作目录契约`，且其中的路径均为传入的 `repo_root` 字面值

### Requirement: record 拒绝不在本 run 分支上的 commit

`npc implement record` 与 `npc fix record` SHALL 在既有 commit 对象存在性校验之后执行 `git merge-base --is-ancestor <commit> HEAD`（cwd=run 工作树）；不成立时 SHALL 返回 `{ok:false, error:"commit-not-on-run-branch"}`（附 `commit` 与 `hint` 字段），将该 phase 记为 failed，且 MUST NOT 继续 rerun-tests。

#### Scenario: 落在其他分支的 commit 被拒

- GIVEN 一个 run 工作树，且 RESULT 报告的 commit 存在于共享对象库但不在工作树 HEAD 祖先链上（例如提交在主 checkout 的 main 分支）
- WHEN `npc implement record --seq 1 --result "RESULT: commit=<该 hash> tasks=1 tests=pass summary=<有效路径> notes=-"`
- THEN 返回 `ok=false`、`error="commit-not-on-run-branch"`，state 中该 seq 状态为 failed，未执行测试复跑

#### Scenario: HEAD 上的正常 commit 照常放行

- GIVEN commit 即工作树 HEAD（coder 正常就地提交）
- WHEN implement record
- THEN ancestor 门通过，流程与既有行为一致（继续 summary/rerun-tests 校验）

#### Scenario: fix record 同样设门

- GIVEN fix 轮 RESULT 报告的 commit 不在工作树 HEAD 祖先链上
- WHEN `npc fix record --seq 1 --round 1 --result ...`
- THEN 返回 `ok=false`、`error="commit-not-on-run-branch"`

### Requirement: worktree 创建后可选 provisioning

`npc init` SHALL 读取 `[worktree].provision_cmd`（默认空字符串）；非空时在新 worktree 创建成功后于其中执行该命令（shlex 拆分、不经 shell、600s 超时），并在 init JSON 输出 `provision` 字段；命令失败或超时 SHALL 仅告警并置 `provision.ok=false`，MUST NOT 使 init 失败。

#### Scenario: 未配置时零行为变化

- GIVEN config 无 `[worktree].provision_cmd`
- WHEN `npc init`
- THEN 不执行任何 provisioning 命令，init JSON 的 `provision.ran == false`，其余输出与现状一致

#### Scenario: 配置后在 worktree 内执行且失败不阻塞

- GIVEN `provision_cmd = "false"`（必然失败的命令）
- WHEN `npc init`
- THEN init 正常返回 `worktree_root`，`provision == {ran:true, ok:false, ...}`（含 rc 与 tail）

#### Scenario: 成功执行记录成功态

- GIVEN `provision_cmd = "true"`
- WHEN `npc init`
- THEN `provision.ran == true` 且 `provision.ok == true`
