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


def _bootstrap_run(make_args, capsys, *change_ids: str) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
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

    result = _coder.run_implement(pr, 1, "add-foo", backend="claude", runner=runner)

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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    pr = _paths_with_repo(p, fake_repo)

    # coder 输出里完全没有 RESULT 行 → 合成失败 RESULT → record failed
    runner = _fake_runner("blah blah no result here\n", exit_code=0)

    result = _coder.run_implement(pr, 1, "add-foo", backend="claude", runner=runner)

    assert result["ok"] is False
    assert result["backend"] == "claude"
    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "failed"
    assert s["progress"][0]["phases"]["implement"]["status"] == "failed"


def test_run_implement_mimo_injects_env(env_setup, make_args, capsys, fake_repo: Path, tmp_path: Path):
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
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

    result = _coder.run_fix(pr, 1, "add-foo", 1, backend="claude", runner=runner)

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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup

    # which('claude') → None；并把 load_paths 锚到 fake_repo
    monkeypatch.setattr(_coder.shutil, "which", lambda name: None)
    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))

    args = make_args(seq=1, change_id="add-foo", backend="claude", timeout=None, config=None)
    with pytest.raises(SystemExit) as ei:
        _coder.cli_implement_run(args)
    assert ei.value.code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "dependency_missing"


def test_cli_implement_run_success_emits_backend(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup

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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    pr = _paths_with_repo(p, fake_repo)

    runner = _raising_runner(subprocess.TimeoutExpired(cmd="claude", timeout=1))
    # 绝不裸抛——返回错误 dict，phase 落 failed
    result = _coder.run_implement(pr, 1, "add-foo", backend="claude", runner=runner)
    assert result["ok"] is False
    assert result["reason"] == "coder-timeout"
    assert result["backend"] == "claude"

    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["implement"]["status"] == "failed"
    assert entry["status"] == "failed"


def test_run_fix_timeout_lands_failed(env_setup, make_args, capsys, fake_repo: Path):
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    pr = _paths_with_repo(p, fake_repo)

    impl_commit = _real_commit(fake_repo, "impl.txt", "i")

    def mutate(s):
        s["progress"][0]["implement_commit"] = impl_commit

    _state.update_state(p.state_json, p.state_md, mutate)

    runner = _raising_runner(subprocess.TimeoutExpired(cmd="claude", timeout=1))
    result = _coder.run_fix(pr, 1, "add-foo", 1, backend="claude", runner=runner)
    assert result["ok"] is False
    assert result["reason"] == "coder-timeout"

    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["fix-r1"]["status"] == "failed"


def test_cli_implement_run_timeout_emits_json_exit_1(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup

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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    pr = _paths_with_repo(p, fake_repo)

    impl_commit = _real_commit(fake_repo, "impl.txt", "i")

    def mutate(s):
        s["progress"][0]["implement_commit"] = impl_commit

    _state.update_state(p.state_json, p.state_md, mutate)

    runner = _fake_runner("fixer chatter, no result line\n", exit_code=0)
    result = _coder.run_fix(pr, 1, "add-foo", 1, backend="claude", runner=runner)

    assert result["ok"] is False
    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["phases"]["fix-r1"]["status"] == "failed"


# ============================================================
# cli_fix_run
# ============================================================


def test_cli_fix_run_success(env_setup, make_args, capsys, fake_repo: Path, monkeypatch):
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup

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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup

    impl_commit = _real_commit(fake_repo, "impl.txt", "i")

    def mutate(s):
        s["progress"][0]["implement_commit"] = impl_commit

    _state.update_state(p.state_json, p.state_md, mutate)

    monkeypatch.setattr(_coder.shutil, "which", lambda name: None)
    monkeypatch.setattr(_coder._paths, "load_paths", lambda args: _paths_with_repo(p, fake_repo))

    args = make_args(seq=1, change_id="add-foo", round_n=1, backend="claude", timeout=None, config=None)
    with pytest.raises(SystemExit) as ei:
        _coder.cli_fix_run(args)
    assert ei.value.code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "dependency_missing"


def test_cli_fix_run_change_id_mismatch(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
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
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
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


def test_cli_implement_run_mimo_permission_error_exit_3(
    env_setup, make_args, capsys, fake_repo: Path, tmp_path: Path, monkeypatch
):
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup

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
