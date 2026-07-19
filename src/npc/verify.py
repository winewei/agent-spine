"""npc verify —— 把"不裸信自报"做成确定性笼子。

三个子命令：

- ``npc verify tests``：真实复跑测试（质量门）。绝不读 LLM 的 RESULT 自报，
  而是在 repo_root 实际执行测试命令、捕获退出码与输出末尾，emit 结构化判定。
  这是"不裸信 RESULT"硬轨的家。

- ``npc verify routing``：把路由不变量编进代码（生成⊥验证 + 廉价层只许执行）。
  纯函数 :func:`check_routing` 校验 coder/review 后端配置，发现"自己评自己"
  或"MiMo 越权到 review"等违规则报 violation。

- ``npc verify manifest``：并行 implementer（worktree 内 sub-agent）的产出核验。
  解析 RESULT 行（npc key=value 或 legacy JSON 两种格式）判定 plan-only，
  再对 manifest JSON 里声明的 files_written 做存在性 + sha256 核对。
  这是 /new-plan-changes-v3 波次并行的"写没写真代码"硬轨。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

from . import _io
from . import config as _config
from . import paths as _paths


# 输出末尾保留的行数（stdout/stderr 合并后取尾部）
TAIL_LINES = 30

# ============================================================
# 共享：repo 定位 + config 加载（便于测试 monkeypatch）
# ============================================================


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """定位 repo_root。verify 只需 git 仓库（无需 active run / npc init）：

    优先 git toplevel；仅当 cwd 不在 git 仓库时回退 load_paths（兼容显式 --run-ts 调试）。
    """
    try:
        return _paths.detect_repo_root()
    except _paths.PathsError:
        return _paths.load_paths(args).repo_root


def _load_cfg(repo_root: Path) -> _config.Config:
    """加载 npc 配置；失败抛 ConfigError。"""
    return _config.load_config(repo_root)


# ============================================================
# 子命令 1：npc verify tests
# ============================================================


def _has_make_test_target(makefile: Path) -> bool:
    """判断 Makefile 是否含 ``test:`` 目标（行首形如 ``test:`` 或 ``test :``）。"""
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        # 只认行首（列 0）的 ``test:`` / ``test :``，避免缩进的配方行误判。
        if line.startswith("test:") or line.startswith("test :"):
            return True
    return False


def _package_json_has_test_script(package_json: Path) -> bool:
    """判断 package.json 的 scripts.test 是否存在且非空。"""
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return False
    test = scripts.get("test")
    return isinstance(test, str) and bool(test.strip())


def resolve_test_cmd(repo_root: Path, cfg: _config.Config) -> str | None:
    """解析测试命令。纯函数，便于单测。

    优先级：
    1. ``cfg.verify.test`` 显式覆盖。
    2. Go：有 ``go.mod`` → ``go test ./...``。
    3. Python：有 ``pyproject.toml`` 或 ``pytest.ini`` 或 ``tests/`` 目录
       → ``python3 -m pytest -q``。
    4. Node：有 ``package.json`` 且 ``scripts.test`` 非空 → ``npm test``。
    5. Make：有 ``Makefile`` 且含 ``test:`` 目标 → ``make test``。
    6. 都没有 → ``None``。
    """
    if cfg.verify.test:
        return cfg.verify.test

    if (repo_root / "go.mod").is_file():
        return "go test ./..."

    if (
        (repo_root / "pyproject.toml").is_file()
        or (repo_root / "pytest.ini").is_file()
        or (repo_root / "tests").is_dir()
    ):
        return "python3 -m pytest -q"

    pkg = repo_root / "package.json"
    if pkg.is_file() and _package_json_has_test_script(pkg):
        return "npm test"

    makefile = repo_root / "Makefile"
    if makefile.is_file() and _has_make_test_target(makefile):
        return "make test"

    return None


def _tail(stdout: str, stderr: str, lines: int = TAIL_LINES) -> str:
    """合并 stdout/stderr 并取末尾 ``lines`` 行。"""
    combined = (stdout or "") + (stderr or "")
    rows = combined.splitlines()
    return "\n".join(rows[-lines:])


def run_tests(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc verify tests``：在 repo_root 真实复跑测试命令。

    ``runner`` 可注入（默认 :func:`subprocess.run`），测试用假 runner。
    退出码：passed → 0（正常返回）；失败 → 1；无命令/定位失败 → 3。
    """
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    try:
        cfg = _load_cfg(repo_root)
    except _config.ConfigError as e:
        _io.emit_error("config_error", f"配置加载失败：{e}", exit_code=1)
        return

    cmd = resolve_test_cmd(repo_root, cfg)
    if cmd is None:
        _io.emit_error(
            "no_test_command",
            f"未能为 repo 探测到测试命令（无 pyproject/pytest.ini/tests/package.json/Makefile）：{repo_root}",
            exit_code=3,
        )
        return

    # 不裸信可写的 cfg.verify.test：用 shlex.split → argv 列表 + shell=False 执行，
    # 杜绝命令注入（``; rm -rf`` 等元字符不会被 shell 解释）。
    argv = shlex.split(cmd)
    proc = runner(
        argv,
        shell=False,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    passed = proc.returncode == 0
    _io.emit(
        {
            "ok": passed,
            "cmd": cmd,
            "exit_code": proc.returncode,
            "passed": passed,
            "tail": _tail(proc.stdout or "", proc.stderr or ""),
        }
    )
    if not passed:
        raise SystemExit(1)


# ============================================================
# 子命令 2：npc verify routing
# ============================================================


def _contains_token(value: str | None, token: str) -> bool:
    return value is not None and token.lower() in value.lower()


def check_routing(cfg: _config.Config) -> list[dict]:
    """校验路由不变量，返回 violations 列表（纯函数）。

    每项 ``{"rule", "detail"}``。规则：

    1. ``backend_unsupported`` / ``engine_unsupported``：coder backend 必须是已
       注册 provider（内置 + [providers.*]）；review.engine 必须在 SUPPORTED_ENGINES
       （review 恒留 premium，结构上不接受 provider 名）。
    2. ``gen_not_orthogonal``：coder 与 review 解析到同一执行身份 → 等于自己评
       自己，违反 生成⊥验证。覆盖 (a) 都是 claude 且同 bin+model；(b) 都是 mimo。
    3. ``cheap_exec_only``（v1.5 名 ``mimo_exec_only``）：review 路由沾上任何带
       env_file 的廉价层 provider（engine / claude_bin / claude_model 含其名或
       model）→ 违反 不变量 4「廉价层只许执行」。每个命中 provider 一条。
    """
    violations: list[dict] = []
    coder = cfg.coder
    review = cfg.review
    effective_backend = coder.effective_backend
    provider_names = sorted({p.name for p in cfg.providers})

    # coder 实际会用到的全部后端 = 全局 effective + 每个 per-phase 覆盖。
    # 校验必须覆盖 per-phase 路由，否则 [coder.phase].fix=mimo 这类「只把某阶段给
    # 廉价层」会绕过下面的 gen⊥verify 校验——而这正是本校验存在的根本目的。
    backends_in_play = {effective_backend}
    backends_in_play.update(be for _ph, be in coder.phase_backends)

    # 规则 1：后端有效性（覆盖全局 + 每个 phase 覆盖）
    for be in sorted(backends_in_play):
        if cfg.provider(be) is None:
            violations.append(
                {
                    "rule": "backend_unsupported",
                    "detail": f"coder.backend={be!r} 不在 provider 注册表 {provider_names}",
                }
            )
    if review.engine not in _config.SUPPORTED_ENGINES:
        violations.append(
            {
                "rule": "engine_unsupported",
                "detail": f"review.engine={review.engine!r} 不在支持列表 {_config.SUPPORTED_ENGINES}",
            }
        )

    # 规则 2：gen ⊥ verify（coder 任一在用后端与 review 解析到同一执行身份 = 自己评自己）
    same_claude_identity = (
        "claude" in backends_in_play
        and review.engine == "claude"
        and coder.bin == review.claude_bin
        and coder.model == review.claude_model
    )
    both_mimo = "mimo" in backends_in_play and review.engine == "mimo"
    if same_claude_identity or both_mimo:
        violations.append(
            {
                "rule": "gen_not_orthogonal",
                "detail": "coder 与 review 解析到同一执行身份，等于自己评自己",
            }
        )

    # 规则 3：廉价层只许执行。带 env_file 的 provider（mimo / kimi / deepseek ...）
    # 定位为第三方执行端点，review 路由沾上其名或 model 即 violation；每 provider 单条。
    review_fields = (review.engine, review.claude_model, review.claude_bin)
    for p in cfg.providers:
        if not p.env_file:
            continue
        tokens = {p.name} | ({p.model} if p.model else set())
        if any(_contains_token(f, t) for f in review_fields for t in tokens):
            violations.append(
                {
                    "rule": "cheap_exec_only",
                    "detail": (
                        f"review 路由含廉价层 provider {p.name!r}"
                        "（engine/claude_bin/claude_model 命中其名或 model），"
                        "违反 廉价层仅限 coder"
                    ),
                }
            )

    return violations


def run_routing(args: argparse.Namespace) -> None:
    """``npc verify routing``：emit 路由检查结果。

    退出码：无 violation → 0（正常返回）；有 → 1；config 加载失败 → 1。
    """
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    try:
        cfg = _load_cfg(repo_root)
    except _config.ConfigError as e:
        _io.emit_error("config_error", f"配置加载失败：{e}", exit_code=1)
        return

    violations = check_routing(cfg)
    _io.emit(
        {
            "ok": len(violations) == 0,
            "coder_backend": cfg.coder.effective_backend,
            "coder_phase_backends": {ph: be for ph, be in cfg.coder.phase_backends},
            "review_engine": cfg.review.engine,
            "violations": violations,
        }
    )
    if violations:
        raise SystemExit(1)


# ============================================================
# 子命令 3：npc verify manifest
# ============================================================

_RESULT_JSON_RE = re.compile(r"RESULT:\s*(\{.*\})\s*$", re.DOTALL)
_RESULT_NPC_RE = re.compile(r"RESULT:\s*commit=(\S+)")


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_result_verdict(result_line: str, manifest_arg: str | None) -> dict:
    """纯函数：RESULT 行 → ``{verdict, reason, commit, manifest}``。

    两种 RESULT 格式：

    - npc 契约（v2/v3 implementer）：``RESULT: commit=<hash> tasks=.. tests=.. ...``
      manifest 路径来自单独的 ``MANIFEST:`` 行，须经 ``--manifest`` 传入。
    - legacy JSON（architect-swarm）：``RESULT: {"status":.., "files_written":N, "manifest":..}``

    verdict ∈ code / plan_only / error。plan-only 判据：无 RESULT 行、
    npc 格式 commit=-、JSON 格式 status=plan_only 或 files_written≤0。
    """
    raw = (result_line or "").strip()

    m = _RESULT_JSON_RE.search(raw)
    if m:
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            return {"verdict": "plan_only", "reason": f"json_error:{e}", "commit": None, "manifest": manifest_arg}
        manifest = manifest_arg or payload.get("manifest")
        if payload.get("status") == "plan_only":
            return {"verdict": "plan_only", "reason": "self_declared", "commit": None, "manifest": manifest}
        if payload.get("status") == "error":
            return {"verdict": "error", "reason": payload.get("error", "unknown"), "commit": None, "manifest": manifest}
        files = payload.get("files_written", 0)
        if not isinstance(files, int) or files <= 0:
            return {"verdict": "plan_only", "reason": "zero_files_written", "commit": None, "manifest": manifest}
        return {"verdict": "code", "reason": None, "commit": payload.get("commit"), "manifest": manifest}

    m = _RESULT_NPC_RE.search(raw)
    if m:
        commit = m.group(1)
        if commit == "-":
            return {"verdict": "plan_only", "reason": "no_commit", "commit": None, "manifest": manifest_arg}
        return {"verdict": "code", "reason": None, "commit": commit, "manifest": manifest_arg}

    return {"verdict": "plan_only", "reason": "no_result_line", "commit": None, "manifest": manifest_arg}


def check_manifest_files(
    manifest_path: str | None,
    *,
    repo_root: Path | None = None,
    git_ref: str | None = None,
    runner=subprocess.run,
) -> dict:
    """核对 manifest 声明的 files_written：存在性 + 可选 sha256。

    条目可以是纯路径字符串或 ``{path, sha256}`` 对象。返回
    ``{ok, reason, present, missing, sha_mismatch, total}``。

    默认对工作树做核验；传入 ``git_ref``（配合 ``repo_root``）时改为对该
    commit 的 tree 核验——worktree 并行流程里 implementer 的产出在
    cherry-pick 之前只存在于 worktree 分支 commit 上，主工作树核验必然误报。
    """

    def fail(reason: str) -> dict:
        return {"ok": False, "reason": reason, "present": 0, "missing": [], "sha_mismatch": [], "total": 0}

    if not manifest_path or not Path(manifest_path).is_file():
        return fail("manifest_missing")
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return fail(f"manifest_unreadable:{e}")
    files = data.get("files_written") or []
    if not files:
        return fail("manifest_empty_files")

    present, missing, sha_mismatch = 0, [], []
    for entry in files:
        if isinstance(entry, str):
            entry = {"path": entry}
        # manifest 是不可信的 implementer 输出：条目不是 str/dict 或缺合法
        # path 时返回结构化失败，而不是让 .get() 抛裸 traceback。
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
            return fail("manifest_malformed_entry")
        rel = entry["path"]
        declared = entry.get("sha256")
        # 绝对路径（v3 契约：指向仍存活的 worktree）走磁盘核验；
        # 相对路径在给定 git_ref 时对 worktree commit 的 tree 核验。
        if git_ref and not Path(rel).is_absolute():
            root = repo_root or Path.cwd()
            probe = runner(
                ["git", "-C", str(root), "cat-file", "-t", f"{git_ref}:{rel}"],
                capture_output=True,
                text=True,
            )
            # manifest 是不可信输出：cat-file -e 只查对象存在，目录（tree）也会
            # 通过；要求类型必须是 blob，防止 "src" 这类目录条目被计为 present。
            if probe.returncode != 0 or probe.stdout.strip() != "blob":
                missing.append(rel)
                continue
            present += 1
            if declared:
                blob = runner(
                    ["git", "-C", str(root), "show", f"{git_ref}:{rel}"],
                    capture_output=True,
                )
                if hashlib.sha256(blob.stdout).hexdigest() != declared:
                    sha_mismatch.append(rel)
            continue
        path = Path(rel)
        if not path.is_file():
            missing.append(str(path))
            continue
        present += 1
        if declared and _sha256_of(path) != declared:
            sha_mismatch.append(str(path))

    ok = not missing and not sha_mismatch
    reason = None if ok else ("files_missing" if missing else "sha_mismatch")
    return {
        "ok": ok,
        "reason": reason,
        "present": present,
        "missing": missing,
        "sha_mismatch": sha_mismatch,
        "total": len(files),
    }


def run_manifest(args: argparse.Namespace) -> None:
    """``npc verify manifest --result '<RESULT 行>' [--manifest PATH]``。

    退出码：verdict=code 且 manifest 全部核对通过 → 0；否则 1。
    stdout：``{ok, verdict, reason, commit, files}``。
    """
    parsed = parse_result_verdict(args.result, args.manifest)
    files: dict | None = None
    ok = parsed["verdict"] == "code"
    reason = parsed["reason"]

    if ok:
        files = check_manifest_files(parsed["manifest"])
        if not files["ok"]:
            ok = False
            # manifest 缺失/为空/条目非法视为 plan-only（没有可信的真实产出）；
            # 文件丢失/sha 不符是核验失败（代码可能写了但与声明不符）。
            if files["reason"] in (
                "manifest_missing",
                "manifest_empty_files",
                "manifest_malformed_entry",
            ) or str(files["reason"] or "").startswith("manifest_unreadable"):
                parsed["verdict"] = "plan_only"
            reason = files["reason"]

    _io.emit(
        {
            "ok": ok,
            "verdict": parsed["verdict"],
            "reason": reason,
            "commit": parsed["commit"],
            "files": files,
        }
    )
    if not ok:
        raise SystemExit(1)


# ============================================================
# 子命令 4：npc verify tasks（v1.5，P5）
# ============================================================


def run_tasks_check(args: argparse.Namespace) -> None:
    """``npc verify tasks --change ID [--seq N]``：tasks.md 完成度派生计数。

    change 是调度量子，task 绝不进主 context——主 session 与人只看
    ``tasks_done/tasks_total`` 两个数，不看清单。--seq 给定时与 state 里
    implement RESULT 自报的 tasks= 计数交叉验证（claim != tasks_done → 不一致）。

    退出码：0 一致或无 claim；1 claim 存在且不一致；2 缺 --change；
    3 change 目录 / tasks.md 缺失。
    """
    from .spec_analyze import parse_tasks

    change = getattr(args, "change", None)
    if not change:
        _io.emit_error("invalid_args", "必须提供 --change", exit_code=2)
        return

    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    tasks_md = repo_root / "openspec" / "changes" / change / "tasks.md"
    if not tasks_md.is_file():
        _io.emit_error(
            "tasks_not_found", f"tasks.md 不存在：{tasks_md}", exit_code=3
        )
        return

    try:
        tasks = parse_tasks(tasks_md.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        _io.emit_error("tasks_unreadable", f"tasks.md 读取失败：{e}", exit_code=3)
        return
    done = sum(1 for t in tasks if t["done"])
    total = len(tasks)

    claim: int | None = None
    seq = getattr(args, "seq", None)
    if seq is not None:
        from . import state as _state

        try:
            p = _paths.load_paths(args)
            state = _state.read_state(p.state_json)
            entry = (state.get("progress") or [])[seq - 1]
            c = ((entry.get("phases") or {}).get("implement") or {}).get("tasks")
            if isinstance(c, int):
                claim = c
        except (
            _paths.PathsError,
            FileNotFoundError,
            IndexError,
        ):
            pass  # claim 拿不到不阻塞：退化为纯计数

    consistent = None if claim is None else (claim == done)
    _io.emit(
        {
            "ok": consistent is not False,
            "change": change,
            "tasks_done": done,
            "tasks_total": total,
            "claim": claim,
            "consistent": consistent,
        }
    )
    if consistent is False:
        raise SystemExit(1)
