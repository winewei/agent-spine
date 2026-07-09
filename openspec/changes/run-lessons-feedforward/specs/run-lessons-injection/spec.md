# run-lessons-injection

## ADDED Requirements

### Requirement: implement prompt 条件性注入 lessons.md 指针
`npc implement run` 渲染 implement prompt 时，SHALL 检查该 run 的 `<run_dir>/lessons.md` 是否存在且文件大小 > 0；存在且非空时，SHALL 在渲染出的 prompt 的"必读输入"段落追加一条指向该文件绝对路径的 bullet，附带限定语说明其仅供参考、不构成新的验收标准；不存在或为空时 MUST NOT 渲染该段落，此时渲染出的 prompt MUST 与 `run-lessons-feedforward` 之前的行为逐字等价。

#### Scenario: run 内第一个 change 无 lessons 注入
- **WHEN** `<run_dir>/lessons.md` 不存在（run 内尚无 change 完成 archive）
- **THEN** `npc implement run` 渲染出的 prompt 不含任何 lessons 相关段落

#### Scenario: 后续 change 收到 lessons 指针
- **WHEN** 前一个 change 已 archive 并通过 `npc lessons record` 追加了非空 `lessons.md`
- **THEN** 下一个 change 的 `npc implement run` 渲染出的 prompt 的必读输入中含指向 `lessons.md` 绝对路径的 bullet

### Requirement: 只注入指针，不内联内容
渲染出的 prompt MUST NOT 直接内联 `lessons.md` 的正文内容；coder 需自行用 Read 工具读取该文件。

#### Scenario: prompt 不含 lessons 原文
- **WHEN** `lessons.md` 已含多条失败模式段落
- **THEN** 渲染出的 implement prompt 字节内容中不出现 `lessons.md` 内的具体 `categories_scanned` / `notes` 文本，仅出现其文件路径

### Requirement: lessons.md 为空文件时不视为存在
`lessons.md` 存在但文件大小为 0（例如仅创建未追加过任何条目）时，视同不存在，MUST NOT 触发注入段落。

#### Scenario: 空文件不触发注入
- **WHEN** `<run_dir>/lessons.md` 存在但大小为 0 字节
- **THEN** `npc implement run` 渲染出的 prompt 不含 lessons 相关段落
