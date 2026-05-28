"""Review 引擎抽象与具体实现。

为何抽象：``npc review run`` 历史上硬编码 ``codex exec --output-schema`` 子进程；
现在希望同样的 review 管线支持用 ``claude -p`` 跑同一份 focus prompt。

两种引擎的差异：

- **Codex**：原生支持 ``--output-schema``，把符合 schema 的 JSON 直接写到 ``-o`` 指定文件，
  stdout/stderr 流到 events 文件。
- **Claude**：``claude -p`` 没有 schema 强约束，需要把 schema 内联进 prompt 末尾，
  从 stdout 提取 balanced JSON 对象后再写到 ``review_out``。stderr / stdout 原文落 events。

两种引擎的契约共享同一份 :class:`ReviewRunInputs`，返回 exit code（0 成功）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .config import ReviewEngineConfig


# Claude 引擎自定义 exit code：表示 claude 子进程返回 0、但 stdout 里
# 没法提取出合法 JSON 对象。选 65 是为了避开 codex 常见的 0/1/124/127 与
# shell 通用范围。
CLAUDE_JSON_EXTRACT_FAILED_RC = 65


class EngineError(Exception):
    """引擎构造或可执行文件查找失败。"""


@dataclass(frozen=True)
class ReviewRunInputs:
    """单次 review 调用的输入。"""

    repo_root: Path
    schema_path: Path
    focus_text: str
    review_out: Path
    events_out: Path
    timeout_sec: int
    portable_timeout: Path


class ReviewEngine(ABC):
    """review 引擎抽象。"""

    name: str

    @abstractmethod
    def run(self, inputs: ReviewRunInputs) -> int:
        """执行一次 review；副作用：写 ``review_out`` + ``events_out``，返回 exit code。"""


# ============================================================
# Codex
# ============================================================


# codex exec 的 agent-loop 语义：模型一旦发出一条**不含任何工具调用的纯文本消息**，
# 本次 exec 立即结束，并把该文本写入 --output-last-message 文件。实测中模型常在"准备下一步"
# 时发一句旁白（如"让我去读 X"），exec 因此在产出终判 JSON 之前就收尾，落盘的是散文而非 JSON。
# 下面这段约束显式告知模型该机制，要求过程一律走工具调用、唯一文本消息必须是最终 JSON。
_CODEX_LOOP_GUARD = (
    "\n\n---\n\n"
    "**【codex exec 运行机制 — 必须遵守】**\n"
    "你运行在非交互的 codex exec 模式。一旦你发出一条**不包含任何工具调用的纯文本消息**，"
    "本次运行会立即结束、不再继续。因此：\n"
    "- 在你准备好输出最终 JSON 之前，**绝不要**单独发出旁白文本（例如"
    "“让我去读 X”“接下来运行测试”“我已收集完信息，现在开始分析”）。\n"
    "- 需要执行命令时，**直接在该步发起工具调用**，不要先用文本宣告你的计划。\n"
    "- 命令输出被截断时，直接用更精确的命令（缩小范围 / 分段）重新取，**不要**用文本描述你要做什么。\n"
    "- 你**只能发出一条文本消息**，且它必须是符合上方 output-schema 的最终 JSON 对象，"
    "不含 markdown 围栏、不含任何解释性文字。\n"
)


class CodexEngine(ReviewEngine):
    """``codex exec`` 子进程封装（保留 ``--output-schema`` 强约束）。"""

    name = "codex"

    def __init__(self, codex_bin: str):
        self.codex_bin = codex_bin

    def run(self, inputs: ReviewRunInputs) -> int:
        cmd = [
            str(inputs.portable_timeout),
            str(inputs.timeout_sec),
            self.codex_bin,
            "exec",
            "--cd",
            str(inputs.repo_root),
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-c",
            "model_reasoning_effort=high",
            "--output-schema",
            str(inputs.schema_path),
            "-o",
            str(inputs.review_out),
            "--json",
            "-",
        ]
        inputs.review_out.parent.mkdir(parents=True, exist_ok=True)
        inputs.events_out.parent.mkdir(parents=True, exist_ok=True)
        stdin_text = inputs.focus_text + _CODEX_LOOP_GUARD
        with inputs.events_out.open("wb") as ev:
            proc = subprocess.run(
                cmd,
                input=stdin_text.encode("utf-8"),
                stdout=ev,
                stderr=subprocess.STDOUT,
                check=False,
            )
        return proc.returncode


# ============================================================
# Claude
# ============================================================


_CLAUDE_OUTPUT_HINT = (
    "\n\n---\n\n"
    "**输出格式硬约束（claude -p 模式）**：\n"
    "- 不要使用 markdown 围栏（不要 ```json）。\n"
    "- 不要写任何解释、致歉、前后缀文字。\n"
    "- 输出消息的第一个字符必须是 `{`，最后一个字符必须是 `}`。\n"
    "- JSON 必须严格符合上方 Schema；多余字段会被拒绝。\n"
)


class ClaudeEngine(ReviewEngine):
    """``claude -p`` 子进程封装。

    Prompt 拼装：focus_text + Schema 内联块 + 严格输出指令。
    输出处理：从 stdout 提取 balanced JSON 对象后写到 ``review_out``。
    """

    name = "claude"

    def __init__(
        self,
        claude_bin: str,
        *,
        model: str | None = None,
        extra_args: tuple[str, ...] = (),
    ):
        self.claude_bin = claude_bin
        self.model = model
        self.extra_args = tuple(extra_args)

    def run(self, inputs: ReviewRunInputs) -> int:
        prompt = self._compose_prompt(inputs.focus_text, inputs.schema_path)

        cmd: list[str] = [
            str(inputs.portable_timeout),
            str(inputs.timeout_sec),
            self.claude_bin,
            "-p",
            "--output-format",
            "text",
        ]
        if self.model:
            cmd += ["--model", self.model]
        cmd += list(self.extra_args)

        inputs.review_out.parent.mkdir(parents=True, exist_ok=True)
        inputs.events_out.parent.mkdir(parents=True, exist_ok=True)

        with inputs.events_out.open("wb") as ev:
            ev.write(b"# claude -p invocation\n")
            ev.write(("# cmd: " + " ".join(cmd) + "\n").encode("utf-8"))
            ev.write(b"# --- prompt below ---\n")
            ev.write(prompt.encode("utf-8"))
            ev.write(b"\n# --- claude stdout below ---\n")
            ev.flush()

            proc = subprocess.run(
                cmd,
                input=prompt.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=ev,
                check=False,
            )
            stdout_bytes = proc.stdout or b""
            ev.write(stdout_bytes)

        if proc.returncode != 0:
            return proc.returncode

        text = stdout_bytes.decode("utf-8", errors="replace")
        block = extract_json_object(text)
        if block is None:
            with inputs.events_out.open("ab") as ev:
                ev.write(
                    b"\n# ERROR: failed to extract JSON object from claude stdout\n"
                )
            return CLAUDE_JSON_EXTRACT_FAILED_RC
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError as e:
            with inputs.events_out.open("ab") as ev:
                ev.write(
                    f"\n# ERROR: json.loads failed on extracted block: {e}\n".encode(
                        "utf-8"
                    )
                )
            return CLAUDE_JSON_EXTRACT_FAILED_RC
        inputs.review_out.write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0

    @staticmethod
    def _compose_prompt(focus_text: str, schema_path: Path) -> str:
        schema_block = ""
        try:
            schema_text = schema_path.read_text(encoding="utf-8")
        except OSError:
            schema_text = ""
        if schema_text:
            schema_block = (
                "\n\n---\n\n## 输出必须匹配的 JSON Schema\n\n"
                "```\n" + schema_text.strip() + "\n```\n"
            )
        return focus_text + schema_block + _CLAUDE_OUTPUT_HINT


# ============================================================
# JSON 提取
# ============================================================


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> str | None:
    """从任意文本里抽出第一个 balanced JSON 对象字面量。

    顺序：

    1. 整段 strip 后已经是 ``{...}`` → 直接返回。
    2. 出现 ```` ```json ... ``` ```` 围栏 → 返回围栏内文本。
    3. 用栈匹配第一个 ``{...}``（识别字符串与转义，不会被 ``}`` in string 干扰）。

    全部失败返回 ``None``。
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        return s

    m = _FENCE_RE.search(s)
    if m:
        candidate = m.group(1).strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return candidate

    depth = 0
    start_idx = -1
    in_string = False
    escape = False
    for i, ch in enumerate(s):
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start_idx >= 0:
                return s[start_idx : i + 1]
    return None


# ============================================================
# 工厂
# ============================================================


def get_engine(
    cfg: ReviewEngineConfig,
    *,
    name_override: str | None = None,
    codex_bin_override: str | None = None,
) -> ReviewEngine:
    """根据配置 + 可选 CLI 覆盖返回引擎实例。"""
    name = (name_override or cfg.engine or "codex").lower()
    if name == "codex":
        bin_path = codex_bin_override or cfg.codex_bin or shutil.which("codex")
        if not bin_path:
            raise EngineError(
                "未找到 codex 命令；请安装或在配置 [review.codex] bin = ... 指定"
            )
        return CodexEngine(bin_path)
    if name == "claude":
        bin_path = cfg.claude_bin or shutil.which("claude")
        if not bin_path:
            raise EngineError(
                "未找到 claude 命令；请安装 Claude Code CLI 或在配置 [review.claude] bin = ... 指定"
            )
        return ClaudeEngine(
            bin_path, model=cfg.claude_model, extra_args=cfg.claude_extra_args
        )
    raise EngineError(f"未知 review engine：{name!r}（仅支持 codex / claude）")
