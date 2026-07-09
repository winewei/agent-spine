## ADDED Requirements

### Requirement: 原子 git add 纪律

`spine-coder` 在任何阶段（implement 或 fix）暂存改动时 SHALL 只 `git add` 自己明确改动的文件，逐一枚举文件路径；`spine-coder` MUST NOT 使用 `git add -A`、`git add .`，也 MUST NOT 使用 `git add` 配合会隐式匹配未审视文件的通配路径。`spine-coder` MUST NOT 执行任何破坏性 git 操作，包括但不限于 `git stash`、`git reset --hard`、以及会丢弃未提交改动的 `git checkout`/`git restore` 调用。

#### Scenario: 逐文件枚举暂存

- **WHEN** `spine-coder` 完成 implement 或某轮 fix 的代码改动，准备提交
- **THEN** 暂存命令逐一列出本次改动涉及的文件路径，不使用 `-A` 或 `.` 通配

#### Scenario: 拒绝整仓暂存

- **WHEN** `spine-coder` 需要提交改动
- **THEN** `spine-coder` MUST NOT 运行 `git add -A` 或 `git add .`

#### Scenario: 拒绝破坏性 git 操作

- **WHEN** `spine-coder` 在完成任务过程中遇到未预期的 git 状态（如残留改动、冲突文件）
- **THEN** `spine-coder` MUST NOT 运行 `git stash`、`git reset --hard`，或任何会丢弃未提交改动的 `git checkout`/`git restore` 调用来"清理"该状态

#### Scenario: 无法归因的工作区状态必须停止并上报，而非静默处理

- **GIVEN** 工作区存在无法归因给本次任务的改动或冲突文件（既非 `spine-coder` 本次明确产生，也无法确认是否与本次任务相关）
- **WHEN** `spine-coder` 因此无法只暂存自己明确改动的文件、也无法确认本次 commit 的边界
- **THEN** `spine-coder` MUST NOT 继续提交、MUST NOT 忽略该状态径自完成任务；必须停止提交流程，改用**本次任务所渲染 prompt 文件中该阶段（implement 或 fix）既有的失败态 RESULT schema** 上报，MUST NOT 新增、删除或改写该 schema 的任何 key；`notes` 只写一行简短阻塞原因（如"工作区存在无法归因的改动，见 summary"），完整的文件路径清单与逐项状态描述必须写入 `summary.md`，交由 reviewer / 编排者 / 人工介入处理

#### Scenario: implement 阶段阻塞时的失败态 RESULT

- **GIVEN** `spine-coder` 处于 implement 阶段且遇到无法归因的工作区状态
- **WHEN** 其停止提交流程并上报失败
- **THEN** RESULT 行 MUST 是 implement 阶段既有的失败态形式 `commit=- tasks=<已完成数> tests=fail summary=<路径或-> notes=<一行阻塞原因>`，key 集合与 implement 阶段既有失败态 schema 完全一致，MUST NOT 出现该 schema 之外的任何 key

#### Scenario: fix 阶段阻塞时的失败态 RESULT

- **GIVEN** `spine-coder` 处于 fix 阶段且遇到无法归因的工作区状态
- **WHEN** 其停止提交流程并上报失败
- **THEN** RESULT 行 MUST 是 fix 阶段既有的失败态形式 `commit=- fixed=0 tests=fail summary=<路径或-> categories_scanned=- regressions_added=- notes=<一行阻塞原因>`，key 集合与 fix 阶段既有失败态 schema 完全一致（含 `fixed` / `categories_scanned` / `regressions_added`），MUST NOT 为表达"阻塞"这一新语义而新增、删除或改写任何 key；本 change MUST NOT 修改 implement 或 fix 任一阶段 RESULT schema 的 key 集合

#### Scenario: 提交前 index 已含无法归因的 staged 改动

- **GIVEN** 在 `spine-coder` 准备 commit 时，git index 中已存在无法归因给本次任务的 staged 文件或 hunk（例如 spawn 之前遗留的暂存内容）
- **WHEN** `spine-coder` 即将执行 `git commit`
- **THEN** `spine-coder` MUST 在 commit 前核验 index 内容（如 `git diff --cached --name-only`）；若发现无法归因的 staged 条目，MUST NOT 直接 commit。允许的处置仅限**非破坏性 unstage**（`git restore --staged <path>` 或等价的 `git reset -- <path>`，二者均只改 index、不触碰工作区内容），且 unstage 后 MUST 重新核验 index 只剩本次任务明确改动的文件；MUST NOT 使用 `git reset --hard`、`git stash` 或任何丢弃工作区改动的手段"清理" index。若无法通过非破坏性 unstage 达成干净 index，MUST 停止提交流程并按既有失败态 RESULT schema 上报，把 staged 文件路径与其状态写入 `summary.md`

#### Scenario: 同一文件内混有无法归因的未暂存改动时禁止整文件暂存

- **GIVEN** `spine-coder` 需要修改并提交的某个文件，在其未暂存的工作区改动中同时包含无法归因给本次任务的 hunk（既非本次任务产生，也无法确认是否与本次任务相关）
- **WHEN** `spine-coder` 准备暂存该文件
- **THEN** `spine-coder` MUST NOT 对该文件执行整文件 `git add`，因为这会把无法归因的 hunk 一并纳入本次 commit；`spine-coder` 只能在能够精确核验的前提下使用 hunk 级暂存（如 `git add -p` 并逐 hunk 确认，或 `git diff` 核对后生成精确补丁）只暂存自己明确改动的 hunk；若无法达成这种精确核验的把握，MUST NOT 强行暂存，必须停止提交流程并按上一条 Scenario 的失败态 RESULT schema 上报，将该文件路径与无法归因 hunk 的描述写入 `summary.md`

### Requirement: commit 文件清单与 summary.md 改动清单一致

`spine-coder` 提交的 commit 所包含的文件清单 SHALL 与其在 `summary.md` 中逐文件列出的改动清单（"改了什么" / "Files Modified" 段）保持一致。此一致性为**自报口径**：由 `spine-coder` 在生成 summary.md 时自行核对并保证，供 reviewer（语义评审或人工复盘）事后核验；不构成 npc 的确定性校验信号。

#### Scenario: summary.md 改动清单覆盖 commit 实际改动文件

- **WHEN** `spine-coder` 生成 `summary.md` 的改动清单段落
- **THEN** 该清单列出的文件集合与本次 commit 实际改动的文件集合一致（无遗漏、无多余条目）

#### Scenario: 一致性不作为确定性 gate 消费

- **WHEN** `npc implement record` 或 `npc fix record` 装订 `spine-coder` 的 RESULT
- **THEN** npc MUST NOT 依赖对 summary.md 自由文本的解析结果作为通过/拒绝装订的确定性判据；该一致性要求仅约束 `spine-coder` 的自报行为
