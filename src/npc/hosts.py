"""宿主（host agent CLI）抽象。

npc 总是被某个 agent CLI（宿主）在其 shell 工具里调用。v1.7 起 npc 不再假设宿主
是 Claude Code：宿主差异收敛到本模块，其余模块只消费 :class:`ResolvedHost`。

宿主差异只有三件事：

1. **session 目录**：宿主是否有 per-project transcript 目录可供 mtime 启发识别
   session（Claude Code：``~/.claude/projects/<PROJ_KEY>/*.jsonl``）。没有的宿主
   session 识别退化为 by-cwd hook 索引（宿主中立，任何 CLI 的 SessionStart hook
   均可写 ``~/task_log/.session-cache/by-cwd/``）。
2. **auto 授权写入**：``npc init --auto`` 是否会写宿主的项目级权限配置
   （Claude Code：``<repo>/.claude/settings.json``）。其他宿主跳过（各 CLI 权限
   模型不同，由用户按宿主文档自行放行）。
3. **项目上下文文件**：与宿主无关，统一为 ``CLAUDE.md`` → ``AGENTS.md`` fallback
   （见 focus.load_project_context），不进本抽象。

内置宿主：

- ``claude``：Claude Code（session 目录 + settings 授权）
- ``generic``：任意其他 CLI（kimi / codex / qwen / …）；无 session 目录扫描、
  无授权写入。可用 ``[host].session_dir`` 补一个 session 目录模板升级识别能力。

选择顺序：``[host].name`` 显式配置 > env 探测（``CLAUDECODE=1`` → claude）>
``generic``。探测放在 env 而非二进制，因为 npc 跑在宿主的子 shell 里，宿主二进制
是否在 PATH 与"当前谁在调用我"无关。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


HOST_CLAUDE = "claude"
HOST_GENERIC = "generic"

# Claude Code 在其 Bash 工具子进程注入的环境变量
_CLAUDE_ENV_MARKER = "CLAUDECODE"

CLAUDE_SESSION_DIR_TEMPLATE = ".claude/projects/{proj_key}"


@dataclass(frozen=True)
class ResolvedHost:
    """一次运行解析出的宿主能力面。

    - ``session_dir_template``：相对 home 的目录模板，``{proj_key}`` 占位；
      None = 该宿主无 per-project session 目录（mtime 启发不可用）。
    - ``settings_grant``：``npc init --auto`` 是否写宿主项目级权限配置。
    - ``source``：``config`` / ``env`` / ``default``，标注宿主是怎么定下来的。
    """

    name: str
    session_dir_template: str | None = None
    settings_grant: bool = False
    source: str = "default"

    def session_dir(self, home: Path, proj_key: str) -> Path | None:
        """该宿主的 session 目录（绝对路径）；宿主无此能力返回 None。"""
        if not self.session_dir_template:
            return None
        return home / self.session_dir_template.format(proj_key=proj_key)


def _builtin(name: str, source: str, session_dir_override: str | None) -> ResolvedHost:
    if name == HOST_CLAUDE:
        return ResolvedHost(
            name=HOST_CLAUDE,
            session_dir_template=session_dir_override or CLAUDE_SESSION_DIR_TEMPLATE,
            settings_grant=True,
            source=source,
        )
    # generic 或任意自定义名：能力面一致（session 目录仅在显式配置时可用）
    return ResolvedHost(
        name=name,
        session_dir_template=session_dir_override,
        settings_grant=False,
        source=source,
    )


def resolve_host(
    name: str | None = None,
    session_dir: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> ResolvedHost:
    """解析当前宿主。

    - ``name`` / ``session_dir``：来自 ``[host]`` 配置（None = 未配置）。
    - ``env``：注入点，默认 ``os.environ``。

    ``name`` 不做枚举校验：未知名字按 generic 能力面处理（宿主生态随时冒新 CLI，
    npc 不做守门员），但 ``session_dir`` 配置了就生效。
    """
    e = env if env is not None else dict(os.environ)
    if name:
        return _builtin(name, "config", session_dir)
    if e.get(_CLAUDE_ENV_MARKER):
        return _builtin(HOST_CLAUDE, "env", session_dir)
    return _builtin(HOST_GENERIC, "default", session_dir)


def resolve_host_from_config(cfg, *, env: dict[str, str] | None = None) -> ResolvedHost:
    """从 :class:`npc.config.Config` 解析宿主（薄封装，供 init/doctor 复用）。"""
    return resolve_host(cfg.host.name, cfg.host.session_dir, env=env)
