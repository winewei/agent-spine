"""coder 模块测试：implement run / fix run + backend 抽象 + env 解析。

全程不真实调用 claude/codex/网络——backend 子进程通过注入的假 runner 提供预设
stdout（含合规 RESULT 行 + 真实在 tmp git repo 里 commit 的 hash）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from npc import coder as _coder
from npc import state as _state
from npc.config import Config, CoderConfig


# ============================================================
# Helpers / fixtures
# ============================================================


def _bootstrap_run(make_args, capsys, *change_ids: str, p=None) -> None:
    """初始化 run state，使用 p（Paths）的 task_log_dir 强制定位到 tmp 目录。

    若传入 p，则把 task_log_dir 加入 args。这样 load_paths step 3 会在 tmp 目录里
    找不到 active.json，继而回退到 step 4 NPC_* env 变量（由 env_setup 注入），
    避免 load_paths 经真实 active.json 解析到真实 state_json。
    """
    extra = {}
    if p is not None:
        extra = {"task_log_dir": str(p.task_log_dir)}
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids)), **extra))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None, **extra))
        capsys.readouterr()


def _paths_with_repo(p, fake_repo: Path):
    return type(p)(**{**p.__dict__, "repo_root": fake_repo})


def _real_commit(fake_repo: Path, fname: str = "f.txt", content: str = "x") -> str:
    (fake_repo / fname).write_text(content)
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"feat: {fname}"], cwd=fake_repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()


def _fake_runner(stdout: str, exit_code: int = 0):
    """构造假 runner，记录被调用的 argv/cwd/env，返回预设 stdout。"""
    calls: list[dict] = []

    def runner(*, argv, cwd, env=None, timeout=None):
        calls.append({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
        return _coder.CoderRunResult(stdout=stdout, exit_code=exit_code)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ============================================================
# resolve_backend（纯函数）
# ============================================================


def test_resolve_backend_override_wins():
    cfg = Config()
    assert _coder.resolve_backend(cfg, "implement", "codex") == "codex"
    # 即使配置非默认，override 也优先
    cfg2 = Config(coder=CoderConfig(backend="mimo"))
    assert _coder.resolve_backend(cfg2, "fix", "claude") == "claude"


def test_resolve_backend_from_global_config():
    cfg = Config(coder=CoderConfig(backend="mimo"))
    assert _coder.resolve_backend(cfg, "implement", None) == "mimo"


def test_resolve_backend_default_claude_no_config():
    # 裸跑无配置 → 默认 claude
    assert _coder.resolve_backend(Config(), "implement", None) == "claude"


def test_resolve_backend_mimo_not_auto_enabled_when_env_present(tmp_path: Path):
    # 关键：mimo.env 存在也不再自动启用 mimo（MiMo 默认不启用，按需开）
    env_file = tmp_path / "mimo.env"
    env_file.write_text("export ANTHROPIC_BASE_URL=https://x\n")
    cfg = Config(coder=CoderConfig(mimo_env_file=str(env_file)))
    assert _coder.resolve_backend(cfg, "implement", None) == "claude"


def test_resolve_backend_per_phase_routing():
    # 只把 fix 路由到 mimo，implement 仍默认 claude
    cfg = Config(coder=CoderConfig(phase_backends=(("fix", "mimo"),)))
    assert _coder.resolve_backend(cfg, "fix", None) == "mimo"
    assert _coder.resolve_backend(cfg, "implement", None) == "claude"


def test_resolve_backend_phase_overrides_global():
    # 全局 mimo，但 implement 显式回退 claude
    cfg = Config(coder=CoderConfig(backend="mimo", phase_backends=(("implement", "claude"),)))
    assert _coder.resolve_backend(cfg, "implement", None) == "claude"
    assert _coder.resolve_backend(cfg, "fix", None) == "mimo"


# ============================================================
# parse_env_file（纯函数）
# ============================================================


def test_parse_env_file_export_and_bare():
    text = (
        "# comment\n"
        "\n"
        "export ANTHROPIC_BASE_URL=https://mimo.example/api\n"
        "export ANTHROPIC_AUTH_TOKEN=secret-token\n"
        "ANTHROPIC_MODEL=mimo-v2.5-pro\n"
    )
    out = _coder.parse_env_file(text)
    assert out["ANTHROPIC_BASE_URL"] == "https://mimo.example/api"
    assert out["ANTHROPIC_AUTH_TOKEN"] == "secret-token"
    assert out["ANTHROPIC_MODEL"] == "mimo-v2.5-pro"


def test_parse_env_file_strips_quotes_and_ignores_junk():
    text = 'export K1="quoted value"\nexport K2=\'single\'\nnot_a_kv_line\n# nope\n'
    out = _coder.parse_env_file(text)
    assert out["K1"] == "quoted value"
    assert out["K2"] == "single"
    assert "not_a_kv_line" not in out
    assert len(out) == 2


# ============================================================
# implement run
# ============================================================


def test_run_implement_success(env_setup, make_args, capsys, fake_repo: Path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    pr = _paths_with_repo(p, fake_repo)

    commit = _real_commit(fake_repo)
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "implement.summary.md"
    summary.write_text("# impl summary\n")

    stdout = (
        "some coder chatter\n"
        f"RESULT: commit={commit} tasks=3 tests=pass summary={summary} notes=ok\n"
    )
    runner = _fake_runner(stdout, exit_code=0)

    result = _coder.run_implement(pr, 1, "add-foo", backend="claude", dispatch="headless", runner=runner)

    assert result["ok"] is True
    assert result["commit"] == commit
    assert result["backend"] == "claude"
    assert "model" in result
    assert result["coder_exit"] == 0

    # phase 装订正确
    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["implement"]["status"] == "done"
    assert entry["status"] == "reviewing"
    assert entry["implement_commit"] == commit

    # prompt 文件已落盘；runner 在 repo_root 跑
    assert (base / "implement.prompt.md").is_file()
    assert runner.calls[0]["cwd"] == fake_repo
    argv = runner.calls[0]["argv"]
    assert "-p" in argv and "--permission-mode" in argv and "bypassPermissions" in argv


def test_run_implement_no_result_line_fails(env_setup, make_args, capsys, fake_repo: Path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    pr = _paths_with_repo(p, fake_repo)

    # coder 输出里完全没有 RESULT 行 → 合成失败 RESULT → record failed
    runner = _fake_runner("blah blah no result here\n", exit_code=0)

    result = _coder.run_implement(pr, 1, "add-foo", backend="claude", dispatch="headless", runner=runner)

    assert result["ok"] is False
    assert result["backend"] == "claude"
    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "failed"
    assert s["progress"][0]["phases"]["implement"]["status"] == "failed"


def test_run_implement_mimo_injects_env(env_setup, make_args, capsys, fake_repo: Path, tmp_path: Path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    pr = _paths_with_repo(p, fake_repo)

    commit = _real_commit(fake_repo)
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "implement.summary.md"
    summary.write_text("# s\n")

    env_file = tmp_path / "mimo.env"
    env_file.write_text(
        "export ANTHROPIC_BASE_URL=https://mimo.example\n"
        "export ANTHROPIC_AUTH_TOKEN=tok\n"
    )
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(
        '[coder]\nbackend = "mimo"\n[coder.mimo]\nenv_file = "%s"\n' % env_file
    )

    stdout = f"RESULT: commit={commit} tasks=1 tests=pass summary={summary} notes=-\n"
    runner = _fake_runner(stdout, exit_code=0)

    result = _coder.run_implement(pr, 1, "add-foo", config_path=cfg_path, runner=runner)

    assert result["ok"] is True
    assert result["backend"] == "mimo"
    assert result["model"] == _coder.DEFAULT_MIMO_MODEL
    # 注入的 env 含 mimo token + base url
    injected = runner.calls[0]["env"]
    assert injected["ANTHROPIC_BASE_URL"] == "https://mimo.example"
    assert injected["ANTHROPIC_AUTH_TOKEN"] == "tok"
    # model 进了 argv
    argv = runner.calls[0]["argv"]
    assert _coder.DEFAULT_MIMO_MODEL in argv


# ============================================================
# fix run
# ============================================================


def test_run_fix_success(env_setup, make_args, capsys, fake_repo: Path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    pr = _paths_with_repo(p, fake_repo)

    # 先安排 implement_commit
    impl_commit = _real_commit(fake_repo, "impl.txt", "i")

    def mutate(s):
        s["progress"][0]["implement_commit"] = impl_commit

    _state.update_state(p.state_json, p.state_md, mutate)

    fix_commit = _real_commit(fake_repo, "fix.txt", "z")
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "round-1.fix.summary.md"
    summary.write_text("# fix summary\n")

    stdout = (
        f"RESULT: commit={fix_commit} fixed=2 tests=pass summary={summary} "
        f"categories_scanned=validation regressions_added=- notes=-\n"
    )
    runner = _fake_runner(stdout, exit_code=0)

    result = _coder.run_fix(pr, 1, "add-foo", 1, backend="claude", dispatch="headless", runner=runner)

    assert result["ok"] is True
    assert result["commit"] == fix_commit
    assert result["backend"] == "claude"
    assert result["coder_exit"] == 0

    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["fix-r1"]["status"] == "done"
    assert entry["status"] == "in-fix-loop"
    assert (base / "round-1.fix.prompt.md").is_file()


# ============================================================
# 依赖缺失 → exit 4
# ============================================================


def test_cli_implement_run_missing_claude_bin(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    # which('claude') → None；并把 load_paths 锚到 fake_repo
    monkeypatch.setattr(_coder.shutil, "which", lambda name: None)
    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))

    # dispatch=headless 强制走子进程路径，触发 claude 二进制查找
    args = make_args(seq=1, change_id="add-foo", backend="claude", dispatch="headless", timeout=None, config=None)
    with pytest.raises(SystemExit) as ei:
        _coder.cli_implement_run(args)
    assert ei.value.code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "dependency_missing"


def test_cli_implement_run_success_emits_backend(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    commit = _real_commit(fake_repo)
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "implement.summary.md"
    summary.write_text("# s\n")

    stdout = f"RESULT: commit={commit} tasks=1 tests=pass summary={summary} notes=-\n"

    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))
    monkeypatch.setattr(
        _coder, "run_implement", lambda *a, **k: {"ok": True, "backend": "claude", "model": None, "coder_exit": 0, "seq": 1}
    )

    args = make_args(seq=1, change_id="add-foo", backend="claude", timeout=None, config=None)
    _coder.cli_implement_run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["backend"] == "claude"


# ============================================================
# codex backend：留 TODO（NotImplemented → 友好退出）
# ============================================================


def test_run_implement_codex_not_implemented(env_setup, make_args, capsys, fake_repo: Path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    pr = _paths_with_repo(p, fake_repo)
    runner = _fake_runner("", exit_code=0)
    with pytest.raises(NotImplementedError, match="codex"):
        _coder.run_implement(pr, 1, "add-foo", backend="codex", runner=runner)


# ============================================================
# TimeoutExpired / SubprocessError → phase failed + 不裸抛
# ============================================================


def _raising_runner(exc: Exception):
    def runner(*, argv, cwd, env=None, timeout=None):
        raise exc

    return runner


def test_run_implement_timeout_lands_failed(env_setup, make_args, capsys, fake_repo: Path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    pr = _paths_with_repo(p, fake_repo)

    runner = _raising_runner(subprocess.TimeoutExpired(cmd="claude", timeout=1))
    # 绝不裸抛——返回错误 dict，phase 落 failed；dispatch=headless 强制走子进程路径
    result = _coder.run_implement(pr, 1, "add-foo", backend="claude", dispatch="headless", runner=runner)
    assert result["ok"] is False
    assert result["reason"] == "coder-timeout"
    assert result["backend"] == "claude"

    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["implement"]["status"] == "failed"
    assert entry["status"] == "failed"


def test_run_fix_timeout_lands_failed(env_setup, make_args, capsys, fake_repo: Path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    pr = _paths_with_repo(p, fake_repo)

    impl_commit = _real_commit(fake_repo, "impl.txt", "i")

    def mutate(s):
        s["progress"][0]["implement_commit"] = impl_commit

    _state.update_state(p.state_json, p.state_md, mutate)

    runner = _raising_runner(subprocess.TimeoutExpired(cmd="claude", timeout=1))
    # dispatch=headless 强制走子进程路径，确保 timeout 真正触发
    result = _coder.run_fix(pr, 1, "add-foo", 1, backend="claude", dispatch="headless", runner=runner)
    assert result["ok"] is False
    assert result["reason"] == "coder-timeout"

    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["fix-r1"]["status"] == "failed"


def test_cli_implement_run_timeout_emits_json_exit_1(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))
    monkeypatch.setattr(
        _coder,
        "run_implement",
        lambda *a, **k: {"ok": False, "seq": 1, "error": "coder-timeout", "reason": "coder-timeout", "backend": "claude"},
    )

    args = make_args(seq=1, change_id="add-foo", backend="claude", timeout=None, config=None)
    with pytest.raises(SystemExit) as ei:
        _coder.cli_implement_run(args)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "coder-timeout"


# ============================================================
# run_fix：无 RESULT 行 → 失败装订
# ============================================================


def test_run_fix_no_result_line_fails(env_setup, make_args, capsys, fake_repo: Path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    pr = _paths_with_repo(p, fake_repo)

    impl_commit = _real_commit(fake_repo, "impl.txt", "i")

    def mutate(s):
        s["progress"][0]["implement_commit"] = impl_commit

    _state.update_state(p.state_json, p.state_md, mutate)

    runner = _fake_runner("fixer chatter, no result line\n", exit_code=0)
    result = _coder.run_fix(pr, 1, "add-foo", 1, backend="claude", dispatch="headless", runner=runner)

    assert result["ok"] is False
    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["phases"]["fix-r1"]["status"] == "failed"


# ============================================================
# cli_fix_run
# ============================================================


def test_cli_fix_run_success(env_setup, make_args, capsys, fake_repo: Path, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))
    monkeypatch.setattr(
        _coder,
        "run_fix",
        lambda *a, **k: {"ok": True, "seq": 1, "round": 1, "backend": "claude", "model": None, "coder_exit": 0},
    )

    args = make_args(seq=1, change_id="add-foo", round_n=1, backend="claude", timeout=None, config=None)
    _coder.cli_fix_run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["backend"] == "claude"


def test_cli_fix_run_missing_claude_bin(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    impl_commit = _real_commit(fake_repo, "impl.txt", "i")

    def mutate(s):
        s["progress"][0]["implement_commit"] = impl_commit

    _state.update_state(p.state_json, p.state_md, mutate)

    monkeypatch.setattr(_coder.shutil, "which", lambda name: None)
    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))

    # dispatch=headless 强制走子进程路径，触发 claude 二进制查找
    args = make_args(seq=1, change_id="add-foo", round_n=1, backend="claude", dispatch="headless", timeout=None, config=None)
    with pytest.raises(SystemExit) as ei:
        _coder.cli_fix_run(args)
    assert ei.value.code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "dependency_missing"


def test_cli_fix_run_change_id_mismatch(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))

    # state 里 seq=1 是 add-foo，传入 change_id=wrong → 不一致 → exit 3
    args = make_args(seq=1, change_id="wrong", round_n=1, backend="claude", timeout=None, config=None)
    with pytest.raises(SystemExit) as ei:
        _coder.cli_fix_run(args)
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "env_missing"


def test_cli_fix_run_codex_not_implemented_exit_2(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))

    args = make_args(seq=1, change_id="add-foo", round_n=1, backend="codex", timeout=None, config=None)
    with pytest.raises(SystemExit) as ei:
        _coder.cli_fix_run(args)
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "not_implemented"


# ============================================================
# ConfigError（坏 TOML）→ exit 2
# ============================================================


def test_cli_implement_run_bad_config_exit_2(
    env_setup, make_args, capsys, fake_repo: Path, tmp_path: Path, monkeypatch
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)
    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))

    bad_cfg = tmp_path / "bad.toml"
    bad_cfg.write_text("this is = = not valid toml [[[\n")

    args = make_args(
        seq=1, change_id="add-foo", backend="claude", timeout=None, config=str(bad_cfg)
    )
    with pytest.raises(SystemExit) as ei:
        _coder.cli_implement_run(args)
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "invalid_config"


# ============================================================
# _mimo_env PermissionError → 干净错误（MimoEnvError）/ CLI exit 3
# ============================================================


def test_mimo_env_permission_error_clean(tmp_path: Path, monkeypatch):
    env_file = tmp_path / "mimo.env"
    env_file.write_text("export ANTHROPIC_AUTH_TOKEN=tok\n")
    cfg = Config(coder=CoderConfig(backend="mimo", mimo_env_file=str(env_file)))

    # env_file 存在但 read 抛 PermissionError（chmod 600 密钥权限错）
    orig_read = Path.read_text

    def boom(self, *a, **k):
        if self == env_file:
            raise PermissionError(13, "Permission denied")
        return orig_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)

    with pytest.raises(_coder.MimoEnvError, match="权限"):
        _coder._mimo_env(cfg)


# ============================================================
# scrub-coder-subprocess-api-key: 子进程 env 剔除 Anthropic 计费凭据
# ============================================================


def test_scrubbed_base_env_removes_billing_keys(monkeypatch):
    """_scrubbed_base_env() 剔除 Anthropic 计费凭据，其余键保留。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-should-be-scrubbed")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/user")

    result = _coder._scrubbed_base_env()

    assert "ANTHROPIC_API_KEY" not in result, "billing key must be scrubbed"
    assert "ANTHROPIC_AUTH_TOKEN" not in result, "auth token must be scrubbed"
    assert result.get("PATH") == "/usr/bin:/bin", "PATH must be preserved"
    assert result.get("HOME") == "/home/user", "HOME must be preserved"


def test_claude_backend_scrubs_api_key_from_env(monkeypatch, tmp_path: Path):
    """Scenario: claude 后端在环境含 API key 时仍不付费。

    WHEN claude 后端启动 coder 子进程，且 npc 进程环境里设置了 ANTHROPIC_API_KEY
    THEN 传给子进程的环境不含 ANTHROPIC_API_KEY 与 ANTHROPIC_AUTH_TOKEN
    AND 其余环境变量（PATH、HOME 等）原样保留
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-should-be-scrubbed")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    from npc.config import Config

    cfg = Config()
    runner = _fake_runner("RESULT: ok\n", exit_code=0)
    monkeypatch.setattr(_coder.shutil, "which", lambda name: "/usr/bin/claude")

    _coder._run_backend(
        cfg, "claude",
        spawn_text="test prompt",
        repo_root=tmp_path,
        backend_override_bin=None,
        runner=runner,
        timeout=None,
    )

    injected = runner.calls[0]["env"]
    assert "ANTHROPIC_API_KEY" not in injected, "billing key must be scrubbed"
    assert "ANTHROPIC_AUTH_TOKEN" not in injected, "auth token must be scrubbed"
    assert "PATH" in injected, "PATH must be preserved"


def test_claude_backend_no_key_env_equivalent_to_current(monkeypatch, tmp_path: Path):
    """Scenario: 无 Anthropic key 时行为与现状一致。

    WHEN 环境里未设置任何 Anthropic 计费凭据
    THEN scrubbed baseline 等价于继承当前环境（除两键外无差异）
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("MY_CUSTOM_VAR", "preserved")

    from npc.config import Config

    cfg = Config()
    runner = _fake_runner("RESULT: ok\n", exit_code=0)
    monkeypatch.setattr(_coder.shutil, "which", lambda name: "/usr/bin/claude")

    _coder._run_backend(
        cfg, "claude",
        spawn_text="test prompt",
        repo_root=tmp_path,
        backend_override_bin=None,
        runner=runner,
        timeout=None,
    )

    injected = runner.calls[0]["env"]
    # 无 key 时，scrubbed baseline = 当前环境（两键均不存在，无差异）
    assert "ANTHROPIC_API_KEY" not in injected
    assert "ANTHROPIC_AUTH_TOKEN" not in injected
    assert injected.get("PATH") == "/usr/local/bin:/usr/bin"
    assert injected.get("MY_CUSTOM_VAR") == "preserved"


def test_mimo_backend_scrubs_inherited_key_but_keeps_mimo_credentials(
    monkeypatch, tmp_path: Path
):
    """Scenario: mimo 后端在 scrubbed baseline 上叠加自身凭据。

    WHEN mimo 后端启动 coder 子进程
    THEN 子进程 env = scrubbed baseline + mimo.env 解析键值
    AND mimo.env 内声明的 ANTHROPIC_API_KEY（指向 MiMo 第三方端点）正常生效，不被误删
    """
    # 进程环境里有"真实"Anthropic 计费 key（应被 scrub 掉）
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-billing-key-must-be-scrubbed")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "real-auth-token-must-be-scrubbed")

    # mimo.env 里声明的是 MiMo 第三方端点凭据（应保留并覆盖 scrubbed baseline）
    env_file = tmp_path / "mimo.env"
    env_file.write_text(
        "export ANTHROPIC_BASE_URL=https://mimo.example/api\n"
        "export ANTHROPIC_API_KEY=mimo-api-key-should-survive\n"
    )

    from npc.config import Config, CoderConfig

    cfg = Config(coder=CoderConfig(backend="mimo", mimo_env_file=str(env_file)))
    runner = _fake_runner("RESULT: ok\n", exit_code=0)
    monkeypatch.setattr(_coder.shutil, "which", lambda name: "/usr/bin/claude")

    _coder._run_backend(
        cfg, "mimo",
        spawn_text="test prompt",
        repo_root=tmp_path,
        backend_override_bin=None,
        runner=runner,
        timeout=None,
    )

    injected = runner.calls[0]["env"]
    # mimo.env 里的 ANTHROPIC_API_KEY 覆盖后应是 mimo 的值，不是原始 billing key
    assert injected.get("ANTHROPIC_API_KEY") == "mimo-api-key-should-survive", (
        "mimo.env 里声明的凭据应覆盖 scrubbed baseline"
    )
    assert injected.get("ANTHROPIC_BASE_URL") == "https://mimo.example/api"
    # 继承的 ANTHROPIC_AUTH_TOKEN 已被 scrub，mimo.env 里没有声明，所以不应存在
    assert "ANTHROPIC_AUTH_TOKEN" not in injected


def test_cli_implement_run_mimo_permission_error_exit_3(
    env_setup, make_args, capsys, fake_repo: Path, tmp_path: Path, monkeypatch
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    env_file = tmp_path / "mimo.env"
    env_file.write_text("export ANTHROPIC_AUTH_TOKEN=tok\n")
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(
        '[coder]\nbackend = "mimo"\n[coder.mimo]\nenv_file = "%s"\n' % env_file
    )

    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))
    monkeypatch.setattr(_coder.shutil, "which", lambda name: "/usr/bin/claude")

    orig_read = Path.read_text

    def boom(self, *a, **k):
        if self == env_file:
            raise PermissionError(13, "Permission denied")
        return orig_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)

    args = make_args(
        seq=1, change_id="add-foo", backend=None, timeout=None, config=str(cfg_path)
    )
    with pytest.raises(SystemExit) as ei:
        _coder.cli_implement_run(args)
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "env_error"


# ============================================================
# CLI --dispatch override（F1 验证：parser 注册 + 转发）
# ============================================================


def test_cli_implement_run_dispatch_headless_forwarded(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """npc implement run --dispatch headless 把 dispatch='headless' 转发给 run_implement。"""
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    forwarded: dict = {}

    def capture_run_implement(*a, **k):
        forwarded.update(k)
        return {"ok": True, "backend": "claude", "model": None, "coder_exit": 0, "seq": 1}

    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))
    monkeypatch.setattr(_coder, "run_implement", capture_run_implement)

    args = make_args(
        seq=1, change_id="add-foo", backend="claude",
        dispatch="headless", timeout=None, config=None
    )
    _coder.cli_implement_run(args)
    assert forwarded.get("dispatch") == "headless", (
        "cli_implement_run 必须把 args.dispatch 转发给 run_implement"
    )


def test_cli_implement_run_dispatch_in_session_forwarded(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """npc implement run --dispatch in-session 把 dispatch='in-session' 转发给 run_implement。"""
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    forwarded: dict = {}

    def capture_run_implement(*a, **k):
        forwarded.update(k)
        return {
            "ok": True, "deferred": True, "dispatch": "in-session",
            "seq": 1, "change_id": "add-foo", "phase": "implement",
            "backend": "claude", "spawn_prompt": "...", "prompt_file": "/tmp/p.md",
        }

    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))
    monkeypatch.setattr(_coder, "run_implement", capture_run_implement)

    args = make_args(
        seq=1, change_id="add-foo", backend="claude",
        dispatch="in-session", timeout=None, config=None
    )
    _coder.cli_implement_run(args)
    assert forwarded.get("dispatch") == "in-session", (
        "cli_implement_run 必须把 args.dispatch 转发给 run_implement"
    )


def test_cli_fix_run_dispatch_headless_forwarded(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """npc fix run --dispatch headless 把 dispatch='headless' 转发给 run_fix。"""
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    forwarded: dict = {}

    def capture_run_fix(*a, **k):
        forwarded.update(k)
        return {"ok": True, "seq": 1, "round": 1, "backend": "claude", "model": None, "coder_exit": 0}

    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))
    monkeypatch.setattr(_coder, "run_fix", capture_run_fix)

    args = make_args(
        seq=1, change_id="add-foo", round_n=1, backend="claude",
        dispatch="headless", timeout=None, config=None
    )
    _coder.cli_fix_run(args)
    assert forwarded.get("dispatch") == "headless", (
        "cli_fix_run 必须把 args.dispatch 转发给 run_fix"
    )


def test_cli_fix_run_dispatch_in_session_forwarded(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """npc fix run --dispatch in-session 把 dispatch='in-session' 转发给 run_fix。"""
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", p=p)

    forwarded: dict = {}

    def capture_run_fix(*a, **k):
        forwarded.update(k)
        return {
            "ok": True, "deferred": True, "dispatch": "in-session",
            "seq": 1, "round": 1, "change_id": "add-foo", "phase": "fix-r1",
            "backend": "claude", "spawn_prompt": "...", "prompt_file": "/tmp/p.md",
        }

    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))
    monkeypatch.setattr(_coder, "run_fix", capture_run_fix)

    args = make_args(
        seq=1, change_id="add-foo", round_n=1, backend="claude",
        dispatch="in-session", timeout=None, config=None
    )
    _coder.cli_fix_run(args)
    assert forwarded.get("dispatch") == "in-session", (
        "cli_fix_run 必须把 args.dispatch 转发给 run_fix"
    )
