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
    coder_phase_backends: tuple[tuple[str, str], ...] = (),
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
            phase_backends=coder_phase_backends,
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
    assert "cheap_exec_only" in rules


def test_routing_mimo_in_review_bin_violation():
    cfg = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_bin="/opt/mimo/claude",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "cheap_exec_only" in rules


def test_routing_mimo_case_insensitive():
    cfg = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_model="MiMo-Pro",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "cheap_exec_only" in rules


def test_routing_mimo_in_review_engine_violation():
    # review.engine 含 mimo → 无条件挡住（顶层检查，不限 claude 分支）。
    # ReviewEngineConfig 的 __post_init__ 会拒未知 engine，这里用 object.__setattr__
    # 绕过校验模拟"未来出现的 mimo engine 配置"，验证 check_routing 仍兜底拦截。
    cfg = _cfg(coder_backend="claude", review_engine="codex")
    object.__setattr__(cfg.review, "engine", "mimo")
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "cheap_exec_only" in rules


def test_routing_mimo_in_both_model_and_bin_single_violation():
    # claude_model 与 claude_bin 都含 mimo → 只 1 条 cheap_exec_only（不重复 append）
    cfg = _cfg(
        coder_backend="mimo",
        review_engine="claude",
        review_claude_bin="/opt/mimo/claude",
        review_claude_model="mimo-v2.5-pro",
    )
    mimo_violations = [
        v for v in _verify.check_routing(cfg) if v["rule"] == "cheap_exec_only"
    ]
    assert len(mimo_violations) == 1


def test_routing_per_phase_mimo_vs_mimo_review_caught():
    """[coder.phase].fix=mimo + review.engine=mimo：全局 backend 仍 claude，
    旧实现只看 effective_backend 会漏判 gen⊥verify；现在须按 per-phase 在用后端拦下。
    review.engine=mimo 是非法配置，用 object.__setattr__ 绕过校验模拟（与既有用例同口径）。"""
    cfg = _cfg(
        coder_backend="claude",
        coder_phase_backends=(("fix", "mimo"),),
        review_engine="codex",
    )
    object.__setattr__(cfg.review, "engine", "mimo")
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "gen_not_orthogonal" in rules


def test_routing_per_phase_claude_identity_caught():
    """[coder.phase].implement=claude 且与 review.claude 同 bin+model：须判自评。"""
    cfg = _cfg(
        coder_backend="mimo",  # 全局是 mimo，但 implement 阶段回退 claude
        coder_phase_backends=(("implement", "claude"),),
        coder_bin="/x/claude",
        coder_model="opus",
        review_engine="claude",
        review_claude_bin="/x/claude",
        review_claude_model="opus",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "gen_not_orthogonal" in rules


def test_routing_per_phase_mimo_codex_review_benign():
    """[coder.phase].fix=mimo + review.engine=codex：合法（review 非同源），不应误判。"""
    cfg = _cfg(
        coder_backend="claude",
        coder_phase_backends=(("fix", "mimo"),),
        review_engine="codex",
    )
    assert _verify.check_routing(cfg) == []


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
# provider 注册表泛化（v1.6：自定义廉价层 provider）
# ============================================================


def _cfg_with_providers(extra_providers: tuple, **kwargs) -> _config.Config:
    cfg = _cfg(**kwargs)
    return _config.Config(
        review=cfg.review,
        coder=cfg.coder,
        providers=_config.BUILTIN_PROVIDERS + extra_providers,
    )


def test_routing_custom_provider_backend_supported():
    kimi = _config.ProviderConfig(name="kimi", env_file="~/x/kimi.env", model="kimi-k3")
    cfg = _cfg_with_providers((kimi,), coder_backend="kimi", review_engine="codex")
    assert _verify.check_routing(cfg) == []


def test_routing_unregistered_backend_violation():
    cfg = _cfg(coder_backend="claude", review_engine="codex")
    object.__setattr__(cfg.coder, "backend", "kimi")  # 绕过加载期校验模拟脏 state
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "backend_unsupported" in rules


def test_routing_cheap_provider_name_in_review_model_violation():
    kimi = _config.ProviderConfig(name="kimi", env_file="~/x/kimi.env", model="kimi-k3")
    cfg = _cfg_with_providers(
        (kimi,),
        coder_backend="kimi",
        review_engine="claude",
        review_claude_model="kimi-k3",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "cheap_exec_only" in rules


def test_routing_cheap_provider_without_env_file_not_flagged():
    """无 env_file 的自定义 provider 不属于廉价层，review 撞名不触发 cheap_exec_only。"""
    local = _config.ProviderConfig(name="local", model="claude-opus-4-8")
    cfg = _cfg_with_providers(
        (local,),
        coder_backend="local",
        review_engine="claude",
        review_claude_model="claude-opus-4-8",
    )
    rules = {v["rule"] for v in _verify.check_routing(cfg)}
    assert "cheap_exec_only" not in rules


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


# ============================================================
# parse_failed_ids / judge_against_baseline
# ============================================================


def test_parse_failed_ids_pytest_and_go():
    from npc import verify as _verify

    out = (
        "some log FAILED not-at-line-start\n"
        "FAILED tests/unit/test_a.py::TestX::test_y[asyncio]\n"
        "ERROR tests/unit/test_b.py::test_setup\n"
        "--- FAIL: TestGoThing (0.00s)\n"
        "3 failed in 1s\n"
    )
    assert _verify.parse_failed_ids(out) == {
        "tests/unit/test_a.py::TestX::test_y[asyncio]",
        "tests/unit/test_b.py::test_setup",
        "TestGoThing",
    }
    assert _verify.parse_failed_ids("") == set()


def test_parse_failed_ids_go_qualified_by_package():
    """go test ./... 的用例名只在包内唯一：同名 TestConnect 分属两包必须得到两个 id。"""
    from npc import verify as _verify

    out = (
        "--- FAIL: TestConnect (0.01s)\n"
        "    conn_test.go:12: boom\n"
        "FAIL\n"
        "FAIL\texample.com/m/pkga\t0.020s\n"
        "ok  \texample.com/m/pkgb\t0.010s\n"
        "--- FAIL: TestConnect (0.00s)\n"
        "    --- FAIL: TestConnect/sub (0.00s)\n"
        "FAIL\texample.com/m/pkgc\t0.030s\n"
        "FAIL\n"
    )
    assert _verify.parse_failed_ids(out) == {
        "example.com/m/pkga::TestConnect",
        "example.com/m/pkgc::TestConnect",
        "example.com/m/pkgc::TestConnect/sub",
    }
    # 基线只有 pkga 失败，pkgc 新增同名失败必须被判为回归
    import subprocess

    proc = subprocess.CompletedProcess(["go"], 1, stdout=out, stderr="")
    judged = _verify.judge_against_baseline(proc, {"example.com/m/pkga::TestConnect"})
    assert judged["passed"] is False
    assert "example.com/m/pkgc::TestConnect" in judged["new_failures"]


def test_judge_against_baseline_modes():
    import subprocess

    from npc import verify as _verify

    ok = subprocess.CompletedProcess(["t"], 0, stdout="", stderr="")
    bad = subprocess.CompletedProcess(["t"], 1, stdout="FAILED a::b\nFAILED c::d\n", stderr="")

    assert _verify.judge_against_baseline(ok, None)["passed"] is True
    assert _verify.judge_against_baseline(bad, None) == {
        "passed": False, "mode": "strict", "failed": None, "new_failures": []}
    assert _verify.judge_against_baseline(bad, {"a::b", "c::d", "e::f"})["passed"] is True
    j = _verify.judge_against_baseline(bad, {"a::b"})
    assert j["passed"] is False and j["new_failures"] == ["c::d"]
    assert _verify.judge_against_baseline(ok, {"a::b"}) == {
        "passed": True, "mode": "diff", "failed": 0, "new_failures": []}


def test_config_rejects_bad_test_baseline(tmp_path):
    import pytest

    from npc import config as _config

    (tmp_path / ".npc").mkdir()
    (tmp_path / ".npc" / "config.toml").write_text('[verify]\ntest_baseline = "lenient"\n')
    with pytest.raises(_config.ConfigError):
        _config.load_config(tmp_path)
    (tmp_path / ".npc" / "config.toml").write_text('[verify]\ntest_baseline = "diff"\n')
    assert _config.load_config(tmp_path).verify.test_baseline == "diff"


# ============================================================
# deps：check_deps 纯函数（依赖不变量执法）
# ============================================================


def _fake_pkg(tmp_path: Path, deps: str = "[]", *, sources: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "pkg"
    (repo / "src" / "npc").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "npc"\ndependencies = {deps}\n', encoding="utf-8"
    )
    for name, body in (sources or {"ok.py": "import json\nfrom . import _io\n"}).items():
        target = repo / "src" / "npc" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return repo


def test_deps_clean_repo_has_no_violation(tmp_path: Path):
    assert _verify.check_deps(_fake_pkg(tmp_path)) == []


def test_deps_rejects_non_empty_dependencies(tmp_path: Path):
    repo = _fake_pkg(tmp_path, deps='["httpx>=0.27"]')
    rules = {v["rule"] for v in _verify.check_deps(repo)}
    assert rules == {"dependencies_not_empty"}


def test_deps_rejects_missing_pyproject(tmp_path: Path):
    repo = _fake_pkg(tmp_path)
    (repo / "pyproject.toml").unlink()
    assert [v["rule"] for v in _verify.check_deps(repo)] == ["pyproject_missing"]


def test_deps_rejects_malformed_pyproject(tmp_path: Path):
    repo = _fake_pkg(tmp_path)
    (repo / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    assert [v["rule"] for v in _verify.check_deps(repo)] == ["pyproject_unreadable"]


@pytest.mark.parametrize(
    "line",
    [
        "import openviking",
        "from openviking.client import X",
        "import httpx",
        "from httpx import Client",
        "import requests",
        "    import requests",
    ],
)
def test_deps_detects_forbidden_imports(tmp_path: Path, line: str):
    repo = _fake_pkg(tmp_path, sources={"bad.py": f"import json\n{line}\n"})
    violations = _verify.check_deps(repo)
    assert [v["rule"] for v in violations] == ["forbidden_import"]
    assert "src/npc/bad.py:2" in violations[0]["detail"]


@pytest.mark.parametrize(
    "line",
    [
        "# import openviking 只是注释",
        'DOC = "import requests"',
        "import requests_stub",
        "from openviking_shim import X",
    ],
)
def test_deps_ignores_non_import_mentions(tmp_path: Path, line: str):
    repo = _fake_pkg(tmp_path, sources={"ok.py": f"{line}\n"})
    assert _verify.check_deps(repo) == []


def test_deps_scans_nested_packages(tmp_path: Path):
    repo = _fake_pkg(tmp_path, sources={"sub/mod.py": "import httpx\n"})
    violations = _verify.check_deps(repo)
    assert len(violations) == 1
    assert "src/npc/sub/mod.py:1" in violations[0]["detail"]


def test_deps_self_check_on_real_repo():
    """本仓库自身必须通过——经验层只经 HTTP，不得引入任何运行时依赖。"""
    repo_root = Path(__file__).resolve().parents[1]
    assert _verify.check_deps(repo_root) == []
