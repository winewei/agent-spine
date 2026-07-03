## 1. hook 配置

- [x] 1.1 新建 `plugins/agent-spine/hooks/hooks.json`：SubagentStop → `bash ${CLAUDE_PLUGIN_ROOT}/hooks/verify-subagent-result.sh`，timeout 设定
- [x] 1.2 核对 plugin.json 是否需要显式声明 hooks（auto-discovery 约定）

## 2. 校验脚本

- [x] 2.1 `verify-subagent-result.sh`：stdin 读 hook JSON，识别 spine-coder（非则 exit 0 放行）
- [x] 2.2 校验最后消息含合法 `RESULT:` 行（implement/fix/失败三套 schema 的必需 key）
- [x] 2.3 `commit=<sha>` 非 `-` 时在 cwd 下 `git cat-file -e <sha>^{commit}` 验证存在
- [x] 2.4 不合规 exit 2 硬阻断，缺陷说明走 stderr；stdout 保持纯 JSON/空（不污染 parser）
- [x] 2.5 容错：脚本自身异常（非 git 目录、jq 缺失）放行不误伤

## 3. 测试

- [x] 3.1 合法 implement RESULT + 真实 sha → exit 0
- [x] 3.2 缺 RESULT 行 / key 缺失 → exit 2
- [x] 3.3 sha 不存在于 git → exit 2；`commit=-`（失败 schema）→ 放行
- [x] 3.4 非 spine-coder subagent → exit 0
- [x] 3.5 `pytest` 全绿（脚本用例经 subprocess 驱动）
