"""Playbook 分发：宿主中立的 harness 工作流文档，随 npc 包发行。

v1.7 起本仓库不再以 Claude Code plugin 形态发布。原 plugin 的 commands / skills /
agents 内容以宿主中立措辞收编到包资源 ``src/npc/playbooks/``，npc 成为唯一分发
物：任何 agent CLI 宿主通过 ``npc playbook`` 取用：

- ``npc playbook list``：枚举全部 playbook（JSON）
- ``npc playbook show <name>``：**raw markdown 到 stdout**（唯一的非 JSON stdout
  例外，方便任意宿主把 playbook 直接拉进 context）
- ``npc playbook install --host claude|codex | --dest DIR``：物化到宿主的
  命令/技能目录（写盘副作用；结果 JSON 列出写入路径）

安装目标（--host 预置）：

- ``claude``：command → ``~/.claude/commands/``，skill →
  ``~/.claude/skills/<name>/SKILL.md``，agent → ``~/.claude/agents/``
- ``codex``：command / skill → ``~/.codex/prompts/``（Codex CLI 自定义 prompt
  目录）；agent 无对应机制，跳过并记入 skipped
- ``--dest DIR``：全部平铺为 ``DIR/<name>.md``（任意其它宿主按各自机制挂载）
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import _io


SUPPORTED_INSTALL_HOSTS = ("claude", "codex")

# kind：command（显式触发的工作流）| skill（可自动触发）| agent（coder 执行体定义）
_KINDS = ("command", "skill", "agent")


@dataclass(frozen=True)
class Playbook:
    name: str
    kind: str
    file: str  # 包资源内相对路径
    summary: str


PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        name="spine-run",
        kind="command",
        file="spine-run.md",
        summary="长时自主 harness：目标/changes → implement→review→fix→archive 全循环",
    ),
    Playbook(
        name="spine-analyze",
        kind="command",
        file="spine-analyze.md",
        summary="读跨 run telemetry 指标，产出 harness 自迭代优化建议（只读）",
    ),
    Playbook(
        name="new-plan-changes-v2",
        kind="command",
        file="new-plan-changes-v2.md",
        summary="串行批量推进 OpenSpec active changes（DAG 排序 + npc 底座）",
    ),
    Playbook(
        name="new-plan-changes-v3",
        kind="skill",
        file="new-plan-changes-v3.md",
        summary="波次并行推进 OpenSpec changes（worktree 隔离 + cherry-pick 整合）",
    ),
    Playbook(
        name="new-plan-changes-v4",
        kind="skill",
        file="new-plan-changes-v4.md",
        summary="v3 的上下文预算版：每 change 三条 npc 命令，主 session 只出现在决策分叉点",
    ),
    Playbook(
        name="spine-coder",
        kind="agent",
        file="agents/spine-coder.md",
        summary="coder 执行体定义（Claude Code subagent 格式；其它宿主作 persona prompt）",
    ),
)


class PlaybookError(Exception):
    """未知 playbook 名 / host 等用法错误。CLI 层转 emit_error exit 2。"""


def _root():
    return resources.files("npc").joinpath("playbooks")


def get(name: str) -> Playbook:
    for pb in PLAYBOOKS:
        if pb.name == name:
            return pb
    raise PlaybookError(
        f"未知 playbook：{name!r}（可选：{', '.join(p.name for p in PLAYBOOKS)}）"
    )


def read_text(pb: Playbook) -> str:
    return _root().joinpath(pb.file).read_text(encoding="utf-8")


def list_playbooks() -> list[dict]:
    out = []
    for pb in PLAYBOOKS:
        text = read_text(pb)
        out.append(
            {
                "name": pb.name,
                "kind": pb.kind,
                "summary": pb.summary,
                "bytes": len(text.encode("utf-8")),
            }
        )
    return out


def _install_target(pb: Playbook, host: str | None, dest: Path | None, home: Path) -> Path | None:
    """单个 playbook 的安装目标路径；该宿主无对应机制返回 None（记 skipped）。"""
    if dest is not None:
        return dest / f"{pb.name}.md"
    if host == "claude":
        if pb.kind == "command":
            return home / ".claude" / "commands" / f"{pb.name}.md"
        if pb.kind == "skill":
            return home / ".claude" / "skills" / pb.name / "SKILL.md"
        return home / ".claude" / "agents" / f"{pb.name}.md"
    if host == "codex":
        if pb.kind == "agent":
            return None  # Codex CLI 无自定义 subagent 文件机制
        return home / ".codex" / "prompts" / f"{pb.name}.md"
    raise PlaybookError(
        f"未知 install host：{host!r}（可选 {'/'.join(SUPPORTED_INSTALL_HOSTS)}，或改用 --dest DIR）"
    )


def install(
    names: list[str] | None,
    *,
    host: str | None,
    dest: Path | None,
    home: Path | None = None,
) -> dict:
    """物化 playbooks 到宿主目录。返回 ``{ok, host|dest, installed[], skipped[]}``。

    幂等：目标已存在则覆盖（playbook 内容以 npc 包内版本为准，升级 npc 后重跑
    install 即同步）。
    """
    if (host is None) == (dest is None):
        raise PlaybookError("--host 与 --dest 必须二选一")
    h = home or Path.home()
    selected = [get(n) for n in names] if names else list(PLAYBOOKS)

    installed: list[dict] = []
    skipped: list[dict] = []
    for pb in selected:
        target = _install_target(pb, host, dest, h)
        if target is None:
            skipped.append({"name": pb.name, "reason": f"host {host} 无 {pb.kind} 机制"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.is_file()
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(read_text(pb), encoding="utf-8")
        os.replace(tmp, target)
        installed.append(
            {"name": pb.name, "kind": pb.kind, "path": str(target), "replaced": existed}
        )

    result: dict = {"ok": True, "installed": installed, "skipped": skipped}
    if host:
        result["host"] = host
    if dest:
        result["dest"] = str(dest)
    return result


# ============================================================
# CLI handlers
# ============================================================


def cli_list(args: argparse.Namespace) -> None:
    _io.emit({"ok": True, "playbooks": list_playbooks()})


def cli_show(args: argparse.Namespace) -> None:
    try:
        pb = get(args.name)
    except PlaybookError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    # 唯一的 raw stdout 例外：playbook 正文直接进宿主 context，不包 JSON
    import sys

    sys.stdout.write(read_text(pb))


def cli_install(args: argparse.Namespace) -> None:
    try:
        result = install(
            args.names or None,
            host=args.host,
            dest=Path(args.dest).expanduser() if args.dest else None,
        )
    except PlaybookError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    except OSError as e:
        _io.emit_error("env_error", f"playbook 安装写盘失败：{e}", exit_code=3)
        return
    _io.emit(result)
