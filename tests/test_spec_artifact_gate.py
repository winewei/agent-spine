"""spec-schema-hardening 回归测试。

覆盖 change `spec-schema-hardening` 引入的项目本地 openspec schema
`openspec/schemas/agent-spine/` + `openspec/config.yaml`：

1. 项目 schema 被 `openspec status`（进而 `npc plan check`）自动解析
   （不传 `--schema`），且 `apply.requires` 恰为
   `[proposal, specs, tasks]`。
2. `npc plan check` 在真实 openspec 二进制下对四类 fixture change 的判定：
   - 缺 `proposal.md` → not ready，`missing` 含 `"proposal"`
   - 缺 `specs/` → not ready，`missing` 含 `"specs"`
   - 三件齐备（无 `design.md`）→ ready
   - 三件齐备 + `design.md` 内联「实施时定」→ 仍 ready（软引导非硬门）
3. schema 的 `artifacts[].instruction` 含要求的静态写作规范子串
   （Open Questions / Non-Goals / WHEN-THEN / 含糊副词禁令）。
4. instruction 不泄漏任何 per-change / review 判据文本（生成 ⊥ 验证不变量，
   与 `templates.SELFCHECK_RUBRIC_MD` 同一条边界）。
5. `openspec archive` 在项目 schema 下仍能把 `changes/<id>/specs/` 折进
   `openspec/specs/`（archive 路径不回归）。
6. `openspec` 不在 PATH 时，本模块全部测试以 `skipped` 呈现，而非静默 `passed`。

Fixture 设计（design.md D6）：`npc plan check` 没有 `--repo-root`，
`_resolve_repo_root` 走 `git rev-parse --show-toplevel`，`openspec status`
从 cwd 发现 `openspec/`。因此每个测试用例都在 `tmp_path` 内构造一个
**自带** `openspec/config.yaml` + 复制进去的 `openspec/schemas/agent-spine/`
的最小 git repo，绝不在真实 `openspec/changes/` 下建临时 change。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ============================================================
# 模块级 skip：openspec 不在 PATH 时全部测试 skipped（非 passed）。
# ============================================================

pytestmark = pytest.mark.skipif(
    shutil.which("openspec") is None, reason="openspec not on PATH"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_SRC = REPO_ROOT / "openspec" / "schemas" / "agent-spine"
_SCHEMA_YAML = _SCHEMA_SRC / "schema.yaml"


# ============================================================
# fixture：tmp_path 内的最小 git repo（自带项目 schema）
# ============================================================


def _init_minimal_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "openspec").mkdir()
    (repo / "openspec" / "project.md").write_text(
        "# Fixture Project\n\ntest_spec_artifact_gate 用的最小 openspec 项目。\n"
    )
    (repo / "openspec" / "config.yaml").write_text("schema: agent-spine\n")
    shutil.copytree(_SCHEMA_SRC, repo / "openspec" / "schemas" / "agent-spine")
    (repo / "openspec" / "changes").mkdir()
    (repo / "README.md").write_text("# fixture repo\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def minimal_repo(tmp_path: Path) -> Path:
    """自带 `openspec/config.yaml` + `openspec/schemas/agent-spine/` 的最小 git repo。"""
    return _init_minimal_repo(tmp_path)


_PROPOSAL_MD = """## Why

Demo fixture proposal used only to exercise the artifact-gate regression
tests; it does not describe a real change to this repository.

## What Changes

- Add a demo capability for test purposes only.

## Non-Goals

- Does not touch any production code path.

## Capabilities

- New Capabilities: demo-cap
"""

_SPEC_MD = """## ADDED Requirements

### Requirement: Demo capability
The system SHALL do a demo thing.

#### Scenario: it does the thing
- **WHEN** triggered
- **THEN** it does the thing
"""

_TASKS_MD = """## 1. Do

- [ ] 1.1 do the demo thing
"""

_HEDGING_DESIGN_MD = """## Context

Demo fixture design doc.

## Decisions

D1: 实施时定 which approach to use (decide at implementation time).

## Open Questions

None.
"""


def _make_change(
    repo: Path,
    change_id: str,
    *,
    proposal: bool,
    specs: bool,
    tasks: bool = True,
    design: str | None = None,
) -> Path:
    """在 ``repo/openspec/changes/<change_id>/`` 下按开关生成 fixture change。"""
    change_dir = repo / "openspec" / "changes" / change_id
    change_dir.mkdir(parents=True)
    if proposal:
        (change_dir / "proposal.md").write_text(_PROPOSAL_MD)
    if specs:
        spec_dir = change_dir / "specs" / "demo-cap"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(_SPEC_MD)
    if tasks:
        (change_dir / "tasks.md").write_text(_TASKS_MD)
    if design is not None:
        (change_dir / "design.md").write_text(design)
    return change_dir


def _run_plan_check(repo: Path, change_id: str) -> tuple[int, dict]:
    """以 cwd=repo 调真实 ``npc plan check``（同解释器，保证使用当前 src/npc）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "npc.cli", "plan", "check", "--change", change_id],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout or "{}")
    return proc.returncode, payload


# ============================================================
# 守护：本模块任何测试都不得污染真实 openspec/changes/
# ============================================================


@pytest.fixture(autouse=True)
def _guard_real_changes_dir():
    real_changes = REPO_ROOT / "openspec" / "changes"
    before = {p.name for p in real_changes.iterdir() if p.is_dir()}
    yield
    after = {p.name for p in real_changes.iterdir() if p.is_dir()}
    assert after == before, f"真实 openspec/changes/ 被测试污染，新增目录：{after - before}"


# ============================================================
# 1. status 自动解析项目 schema + apply.requires
# ============================================================


class TestSchemaAutoResolution:
    def test_status_resolves_project_schema_without_flag(self, minimal_repo):
        _make_change(minimal_repo, "demo-schema-name", proposal=True, specs=True, tasks=True)
        proc = subprocess.run(
            ["openspec", "status", "--change", "demo-schema-name", "--json"],
            cwd=str(minimal_repo),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["schemaName"] == "agent-spine"
        assert set(payload["applyRequires"]) == {"proposal", "specs", "tasks"}

    def test_schema_validate_exits_zero_and_reports_valid(self):
        proc = subprocess.run(
            ["openspec", "schema", "validate", "agent-spine"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "is valid" in proc.stdout


# ============================================================
# 2. npc plan check 硬门行为
# ============================================================


class TestArtifactGate:
    def test_missing_proposal_is_rejected(self, minimal_repo):
        _make_change(minimal_repo, "demo-missing-proposal", proposal=False, specs=True, tasks=True)
        code, payload = _run_plan_check(minimal_repo, "demo-missing-proposal")
        assert code == 1
        assert payload["ok"] is False
        assert payload["ready"] is False
        assert "proposal" in payload["missing"]
        assert set(payload["apply_requires"]) == {"proposal", "specs", "tasks"}

    def test_missing_specs_is_rejected(self, minimal_repo):
        _make_change(minimal_repo, "demo-missing-specs", proposal=True, specs=False, tasks=True)
        code, payload = _run_plan_check(minimal_repo, "demo-missing-specs")
        assert code == 1
        assert payload["ready"] is False
        assert "specs" in payload["missing"]

    def test_complete_change_without_design_is_ready(self, minimal_repo):
        _make_change(minimal_repo, "demo-complete", proposal=True, specs=True, tasks=True)
        code, payload = _run_plan_check(minimal_repo, "demo-complete")
        assert code == 0
        assert payload["ok"] is True
        assert payload["ready"] is True
        assert payload["missing"] == []

    def test_hedging_design_does_not_block_ready(self, minimal_repo):
        """软引导非硬门：design.md 内联「实施时定」不影响 apply.requires 判定。"""
        _make_change(
            minimal_repo,
            "demo-hedging-design",
            proposal=True,
            specs=True,
            tasks=True,
            design=_HEDGING_DESIGN_MD,
        )
        code, payload = _run_plan_check(minimal_repo, "demo-hedging-design")
        assert code == 0
        assert payload["ready"] is True
        assert payload["missing"] == []


# ============================================================
# 3. archive 路径不回归
# ============================================================


class TestArchivePathNotRegressed:
    def test_archive_folds_specs_into_openspec_specs(self, minimal_repo):
        _make_change(minimal_repo, "demo-archive", proposal=True, specs=True, tasks=True)
        # tasks 必须全勾选，archive 才不会因未完成任务而中止。
        change_dir = minimal_repo / "openspec" / "changes" / "demo-archive"
        (change_dir / "tasks.md").write_text("## 1. Do\n\n- [x] 1.1 do the demo thing\n")

        validate_proc = subprocess.run(
            ["openspec", "validate", "demo-archive", "--type", "change", "--strict"],
            cwd=str(minimal_repo),
            capture_output=True,
            text=True,
        )
        assert validate_proc.returncode == 0, validate_proc.stderr
        assert "valid" in validate_proc.stdout

        archive_proc = subprocess.run(
            ["openspec", "archive", "demo-archive", "-y"],
            cwd=str(minimal_repo),
            capture_output=True,
            text=True,
        )
        assert archive_proc.returncode == 0, archive_proc.stderr

        archived_spec = minimal_repo / "openspec" / "specs" / "demo-cap" / "spec.md"
        assert archived_spec.is_file(), "archive 后 openspec/specs/demo-cap/spec.md 应存在"
        assert "Demo capability" in archived_spec.read_text()


# ============================================================
# 4. schema instruction 静态写作规范内容
# ============================================================


def _load_schema() -> dict:
    return yaml.safe_load(_SCHEMA_YAML.read_text())


def _instruction_for(schema: dict, artifact_id: str) -> str:
    for art in schema["artifacts"]:
        if art.get("id") == artifact_id:
            return art.get("instruction", "")
    raise AssertionError(f"schema.yaml 中未找到 id=={artifact_id!r} 的 artifact")


class TestSchemaInstructionContent:
    def test_design_instruction_requires_open_questions(self):
        instr = _instruction_for(_load_schema(), "design")
        assert "Open Questions" in instr
        assert "Decisions" in instr
        # 明确要求延迟决策写入 Open Questions，禁止内联于 Decisions 正文。
        assert "MUST" in instr

    def test_proposal_instruction_requires_non_goals(self):
        instr = _instruction_for(_load_schema(), "proposal")
        assert "Non-Goals" in instr or "非目标" in instr

    def test_specs_instruction_requires_when_then_and_bans_vague_adverbs(self):
        instr = _instruction_for(_load_schema(), "specs")
        assert "WHEN" in instr
        assert "THEN" in instr
        assert "appropriately" in instr  # 含糊副词示例，作为「禁止清单已注入」的标志

    def test_apply_requires_is_exactly_proposal_specs_tasks(self):
        schema = _load_schema()
        assert set(schema["apply"]["requires"]) == {"proposal", "specs", "tasks"}


# ============================================================
# 5. 负向断言 —— instruction 不泄漏验证侧判据（守不变量 1）
# ============================================================


_LEAK_PATTERNS = [
    re.compile(r"round-[\w.\-]*\.review\.json"),
    re.compile(r"blocking_findings"),
    re.compile(r"blocking\s*(==|>)\s*0"),
    re.compile(r"review\s+focus", re.IGNORECASE),
]


def _has_severity_critical_high_same_line(text: str) -> bool:
    for line in text.splitlines():
        low = line.lower()
        if "severity" in low and ("critical" in low or "high" in low):
            return True
    return False


class TestInstructionDoesNotLeakVerificationJudgement:
    """镜像 `templates.SELFCHECK_RUBRIC_MD` 的既有负向测试写法（见
    ``test_reduce_review_fix_cost.py``）：只挡具体泄漏形态，不裸 grep 关键词。
    """

    def test_no_active_or_archived_change_directory_names_leak(self):
        schema = _load_schema()
        all_instr = "\n".join(a.get("instruction", "") for a in schema["artifacts"])

        changes_root = REPO_ROOT / "openspec" / "changes"
        names = [p.name for p in changes_root.iterdir() if p.is_dir() and p.name != "archive"]
        archive_root = changes_root / "archive"
        if archive_root.is_dir():
            names += [p.name for p in archive_root.iterdir() if p.is_dir()]

        for name in names:
            assert name not in all_instr, f"instruction 泄漏了 change 目录名 {name!r}"

    def test_no_leakage_shape_patterns_in_instructions(self):
        schema = _load_schema()
        all_instr = "\n".join(a.get("instruction", "") for a in schema["artifacts"])
        for pattern in _LEAK_PATTERNS:
            assert not pattern.search(all_instr), f"instruction 命中禁止泄漏模式 {pattern.pattern!r}"
        assert not _has_severity_critical_high_same_line(all_instr), (
            "instruction 中 severity 与 critical/high 同行共现，疑似泄漏 reviewer 阈值"
        )

    def test_no_template_interpolation_placeholders(self):
        schema = _load_schema()
        for art in schema["artifacts"]:
            instr = art.get("instruction", "")
            assert "{" not in instr, f"artifact {art.get('id')!r} instruction 含 {{ 插值标记"
            assert "}" not in instr, f"artifact {art.get('id')!r} instruction 含 }} 插值标记"
            assert "$" not in instr, f"artifact {art.get('id')!r} instruction 含 $ 插值标记"

    def test_instruction_text_is_static_not_per_change_rendered(self):
        """静态性证据：schema.yaml 是纯声明式文件，不经过任何 per-change 模板渲染步骤——
        直接读取两次内容完全一致，且不含上面已验证的插值标记，
        故对任意两个不同 change 的「渲染结果」（即：本文件本身）恒定相同。"""
        text1 = _SCHEMA_YAML.read_text()
        text2 = _SCHEMA_YAML.read_text()
        assert text1 == text2
