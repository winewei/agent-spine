## 1. dispatch 配置与 resolve

- [x] 1.1 `config.py`：解析 `[coder].dispatch` 与 `[coder.phase]` 同款 per-phase 覆盖；提供 `dispatch_for_phase(phase, backend)`
- [x] 1.2 内置默认表：claude ⇒ in-session，mimo ⇒ headless，codex ⇒ headless
- [x] 1.3 resolve 优先级：CLI override → per-phase → 全局 → 默认

## 2. in-session 分发（coder.py）

- [x] 2.1 `run_implement`：dispatch=in-session 时 phase_enter + render，返回 deferred 指令（`deferred=true`/`dispatch`/`spawn_prompt`/`prompt_file`），不 spawn 子进程、不 record
- [x] 2.2 `run_fix`：同上，含 `round`
- [x] 2.3 headless 分支（mimo/显式）维持现有 spawn→抽 RESULT→record 行为
- [x] 2.4 CLI（`--dispatch` override，可选）透传到 resolve

## 3. verify routing 扩展

- [x] 3.1 `verify.py`：in-session 绝不与 mimo 同源（mimo+in-session ⇒ violation）

## 4. 测试

- [x] 4.1 dispatch resolve：默认表 + 覆盖优先级
- [x] 4.2 implement in-session：返回 deferred 指令且不调用 runner（用假 runner 断言未被调用）
- [x] 4.3 fix in-session：含 round 的 deferred 指令
- [x] 4.4 headless 回归：mimo / 显式 headless 仍 spawn→record
- [x] 4.5 verify routing：mimo+in-session 判 violation
- [x] 4.6 `pytest` 全绿
