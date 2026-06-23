"""verify.py 测试：tests 子命令（真实复跑测试门）与 routing 子命令（路由不变量）。

routing：直接构造 Config/CoderConfig/ReviewEngineConfig 喂纯函数 check_routing。
tests：用 tmp_path 造假 repo 验 resolve_test_cmd；用假 runner 验 emit/退出码。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from npc import config as _config
from npc import verify as _verify


# ============================================================
# routing：check_routing 纯函数
# ============================================================


def _cfg(
    *,
    coder_backend: str = "claude",
    coder_bin: str | None = None,
    coder_model: str | None = None,
    review_engine: str = "codex",
    review_claude_bin: str | None = None,
    review_claude_model: str | None = None,
) -> _config.Config:
    return _config.Config(
        review=_config.ReviewEngineConfig(
            engine=review_engine,
            claude_bin=review_claude_bin,
            claude_model=review_claude_model,
        ),
        coder=_config.CoderConfig(
            backend=coder_backend,
            bin=coder_bin,
            model=coder_model,
        ),
    )


def test_routing_all_pass_default():
    cfg = _cfg(coder_backend="claude", review_engine="codex")
    assert _verify.check_routing(cfg) == []


def test_routing_gen_not_orthogonal_to_verify():
    # coder=claude 与 review=claude 同一 bin+model → 自己评自己
    cfg = _cfg(
        coder_backend="claude",
        coder_bin="claude",
        coder_model="claude-opus-4-8",
        review_engine="claude",
        review_claude_bin="claude",
        review_claude_model="claude-opus-4-8",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "gen_not_orthogonal" in rules


def test_routing_gen_not_orthogonal_none_bin_none_model():
    # coder=claude(bin=None,model=None) × review=claude(bin=None,model=None)
    # → 解析到同一 claude 身份（默认 bin/model）→ 自己评自己
    cfg = _cfg(
        coder_backend="claude",
        coder_bin=None,
        coder_model=None,
        review_engine="claude",
        review_claude_bin=None,
        review_claude_model=None,
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "gen_not_orthogonal" in rules


def test_routing_claude_same_engine_but_diff_model_ok():
    # 同为 claude 但 model 不同 → 不算自己评自己
    cfg = _cfg(
        coder_backend="claude",
        coder_bin="claude",
        coder_model="claude-sonnet",
        review_engine="claude",
        review_claude_bin="claude",
        review_claude_model="claude-opus-4-8",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "gen_not_orthogonal" not in rules


def test_routing_mimo_in_review_model_violation():
    cfg = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_model="mimo-v2.5-pro",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "mimo_exec_only" in rules


def test_routing_mimo_in_review_bin_violation():
    cfg = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_bin="/opt/mimo/claude",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "mimo_exec_only" in rules


def test_routing_mimo_case_insensitive():
    cfg = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_model="MiMo-Pro",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "mimo_exec_only" in rules


def test_routing_mimo_in_review_engine_violation():
    # review.engine 含 mimo → 无条件挡住（顶层检查，不限 claude 分支）。
    # ReviewEngineConfig 的 __post_init__ 会拒未知 engine，这里用 object.__setattr__
    # 绕过校验模拟"未来出现的 mimo engine 配置"，验证 check_routing 仍兜底拦截。
    cfg = _cfg(coder_backend="claude", review_engine="codex")
    object.__setattr__(cfg.review, "engine", "mimo")
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "mimo_exec_only" in rules


def test_routing_mimo_in_both_model_and_bin_single_violation():
    # claude_model 与 claude_bin 都含 mimo → 只 1 条 mimo_exec_only（不重复 append）
    cfg = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_bin="/opt/mimo/claude",
        review_claude_model="mimo-v2.5-pro",
    )
    mimo_violations = [
        v for v in _verify.check_routing(cfg) if v["rule"] == "mimo_exec_only"
    ]
    assert len(mimo_violations) == 1


def test_routing_mimo_coder_codex_review_benign():
    # coder=mimo × review=codex → 良性，无 violation
    cfg = _cfg(coder_backend="mimo", review_engine="codex")
    assert _verify.check_routing(cfg) == []


def test_routing_mimo_coder_claude_non_mimo_review_benign():
    # coder=mimo × review=claude(非 mimo) → 良性
    cfg = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_model="claude-opus-4-8",
    )
    assert _verify.check_routing(cfg) == []


# ============================================================
# routing：run_routing handler（emit + 退出码）
# ============================================================


def test_run_routing_clean_emits_ok(tmp_path, fake_home, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(
        _verify, "_load_cfg", lambda repo_root: _cfg(coder_backend="mimo", review_engine="codex")
    )
    _verify.run_routing(make_args())
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["coder_backend"] == "mimo"
    assert out["review_engine"] == "codex"
    assert out["violations"] == []


def test_run_routing_violation_exits_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    bad = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_model="mimo-v2.5-pro",
    )
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_verify, "_load_cfg", lambda repo_root: bad)
    with pytest.raises(SystemExit) as ei:
        _verify.run_routing(make_args())
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert len(out["violations"]) >= 1


def test_run_routing_config_error_exits_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)

    def _boom(repo_root):
        raise _config.ConfigError("bad toml")

    monkeypatch.setattr(_verify, "_load_cfg", _boom)
    with pytest.raises(SystemExit) as ei:
        _verify.run_routing(make_args())
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "config_error"


# ============================================================
# tests：resolve_test_cmd 纯函数（按 repo 清单探测）
# ============================================================


def test_resolve_test_cmd_config_override_wins(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    cfg = _config.Config(verify=_config.VerifyConfig(test="my-custom-test"))
    assert _verify.resolve_test_cmd(repo, cfg) == "my-custom-test"


def test_resolve_test_cmd_pyproject(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert _verify.resolve_test_cmd(repo, _config.Config()) == "python3 -m pytest -q"


def test_resolve_test_cmd_pytest_ini(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n")
    assert _verify.resolve_test_cmd(repo, _config.Config()) == "python3 -m pytest -q"


def test_resolve_test_cmd_tests_dir(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    assert _verify.resolve_test_cmd(repo, _config.Config()) == "python3 -m pytest -q"


def test_resolve_test_cmd_package_json_with_test_script(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert _verify.resolve_test_cmd(repo, _config.Config()) == "npm test"


def test_resolve_test_cmd_package_json_without_test_script(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"build": "x"}}))
    assert _verify.resolve_test_cmd(repo, _config.Config()) is None


def test_resolve_test_cmd_makefile(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text(".PHONY: test\ntest:\n\tpytest\n")
    assert _verify.resolve_test_cmd(repo, _config.Config()) == "make test"


def test_resolve_test_cmd_makefile_without_test_target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("build:\n\tgcc\n")
    assert _verify.resolve_test_cmd(repo, _config.Config()) is None


def test_resolve_test_cmd_makefile_indented_test_not_detected(tmp_path):
    # 缩进的 ``test:``（作为别的目标的配方行）不应被当成 test 目标
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("ci:\n\t@echo test:\nbuild:\n\tgcc\n")
    assert _verify.resolve_test_cmd(repo, _config.Config()) is None


def test_resolve_test_cmd_nothing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _verify.resolve_test_cmd(repo, _config.Config()) is None


def test_resolve_test_cmd_pyproject_priority_over_package_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert _verify.resolve_test_cmd(repo, _config.Config()) == "python3 -m pytest -q"


# ============================================================
# tests：run_tests handler（假 runner → emit/退出码）
# ============================================================


def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):
    def _runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return _runner


def test_run_tests_passed_exit_0(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_verify, "_load_cfg", lambda repo_root: _config.Config())
    runner = _fake_run(0, stdout="all passed\n")
    # passed → 不抛 SystemExit（退出码 0 = 正常返回）
    _verify.run_tests(make_args(), runner=runner)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["passed"] is True
    assert out["exit_code"] == 0
    assert out["cmd"] == "python3 -m pytest -q"
    assert "all passed" in out["tail"]


def test_run_tests_failed_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_verify, "_load_cfg", lambda repo_root: _config.Config())
    runner = _fake_run(1, stdout="", stderr="2 failed\n")
    with pytest.raises(SystemExit) as ei:
        _verify.run_tests(make_args(), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["passed"] is False
    assert out["exit_code"] == 1
    assert "2 failed" in out["tail"]


def test_run_tests_tail_truncated_to_30_lines(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_verify, "_load_cfg", lambda repo_root: _config.Config())
    big = "\n".join(f"line{i}" for i in range(100))
    runner = _fake_run(0, stdout=big)
    _verify.run_tests(make_args(), runner=runner)
    out = json.loads(capsys.readouterr().out)
    tail_lines = out["tail"].splitlines()
    assert len(tail_lines) <= 30
    assert "line99" in out["tail"]
    assert "line0" not in tail_lines  # 头部被截掉


def test_run_tests_no_command_exit_3(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()  # 空 repo，啥清单都没有
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_verify, "_load_cfg", lambda repo_root: _config.Config())
    with pytest.raises(SystemExit) as ei:
        _verify.run_tests(make_args(), runner=_fake_run(0))
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "no_test_command"


def test_run_tests_shell_metachars_not_shell_executed(tmp_path, make_args, capsys, monkeypatch):
    # cfg.verify.test 含 shell 元字符时：必须以 argv 列表 + shell=False 执行，
    # 注入片段作为字面 token，不被 shell 解释（不会触发 ``rm -rf`` 等）。
    repo = tmp_path / "repo"
    repo.mkdir()
    malicious = "pytest; rm -rf /tmp/pwned"
    cfg = _config.Config(verify=_config.VerifyConfig(test=malicious))
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_verify, "_load_cfg", lambda repo_root: cfg)

    captured = {}

    def _runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    _verify.run_tests(make_args(), runner=_runner)
    # argv 形态：列表，shell 关闭
    assert captured["shell"] is False
    assert isinstance(captured["cmd"], list)
    # 元字符作为字面 token 保留，未被拆成 shell 命令分隔
    assert captured["cmd"] == ["pytest;", "rm", "-rf", "/tmp/pwned"]


def test_run_tests_config_error_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_verify, "_resolve_repo_root", lambda args: repo)

    def _boom(repo_root):
        raise _config.ConfigError("bad toml")

    monkeypatch.setattr(_verify, "_load_cfg", _boom)
    with pytest.raises(SystemExit) as ei:
        _verify.run_tests(make_args(), runner=_fake_run(0))
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "config_error"


def test_run_tests_repo_locate_failure_exit_3(make_args, capsys, monkeypatch):
    from npc import paths as _paths

    # verify 只需 git 仓库：既非 git 仓库（detect_repo_root 抛）又无 active run
    # （load_paths 抛）时才判 env_missing exit 3。
    def _boom_detect(start=None):
        raise _paths.PathsError("not a git repo")

    def _boom_load(args):
        raise _paths.PathsError("no run")

    monkeypatch.setattr(_paths, "detect_repo_root", _boom_detect)
    monkeypatch.setattr(_paths, "load_paths", _boom_load)
    with pytest.raises(SystemExit) as ei:
        _verify.run_tests(make_args(), runner=_fake_run(0))
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "env_missing"
