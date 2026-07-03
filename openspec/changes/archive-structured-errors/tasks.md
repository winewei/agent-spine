## 1. 异常捕获转结构化 JSON

- [x] 1.1 pipeline.py:831 `git add openspec/`：捕获 CalledProcessError/FileNotFoundError → `emit_error("git-add-failed", stderr 摘要, exit_code=1)`
- [x] 1.2 pipeline.py:737/854 `_git_head`：同样捕获 → `emit_error("git-head-failed", ...)`
- [x] 1.3 复查 run_archive 其余 subprocess 调用点，统一走同一捕获路径（commit 等）

## 2. 测试

- [x] 2.1 用例：注入失败 runner 模拟 `git add` 非零退出 → 断言 stdout 为单行合法 JSON、`ok=false`、`error=git-add-failed`、exit 1
- [x] 2.2 用例：`_git_head` 失败 → 同上断言
- [x] 2.3 用例：stdout 不含 traceback 字样（`Traceback` 不出现在 stdout）
- [x] 2.4 `pytest` 全绿
