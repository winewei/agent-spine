"""npc CLI dispatcher。

每个子命令注册其 argparse parser，handler 路由到对应模块。
未实现的命令返回 not_implemented 错误，便于 stub 阶段验证整体结构。
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from . import __version__
from . import _io

# 57 个叶子命令的 epilog 统一用 RawDescriptionHelpFormatter，保留手写换行/缩进。
_EPILOG_FMT = argparse.RawDescriptionHelpFormatter


def _not_implemented(args: argparse.Namespace) -> None:
    _io.emit_error("not_implemented", f"命令尚未实现：{args._cmd_path}", exit_code=1)


def _make_handler(module_name: str, func_name: str) -> Callable[[argparse.Namespace], None]:
    """惰性导入子模块的 handler，避免启动时全量 import。"""

    def handler(args: argparse.Namespace) -> None:
        from importlib import import_module

        try:
            mod = import_module(f"npc.{module_name}")
        except ImportError as e:
            _io.emit_error(
                "module_missing",
                f"无法导入 npc.{module_name}：{e}",
                exit_code=1,
            )
            return
        func = getattr(mod, func_name, None)
        if func is None:
            _io.emit_error(
                "handler_missing",
                f"模块 {module_name} 缺少函数 {func_name}",
                exit_code=1,
            )
            return
        func(args)

    return handler


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="npc",
        description="agent-spine：Agent skill 的确定性执行层 CLI（契约详见 docs/cli.md）。",
    )
    parser.add_argument("--version", action="version", version=f"npc {__version__}")
    parser.add_argument(
        "--state-json",
        default=None,
        help="覆盖当前 run 的 state_json 路径（调试/特殊场景用）",
    )
    parser.add_argument(
        "--run-ts",
        default=None,
        help="显式指定 run timestamp（跳过 active.json 探测）",
    )
    parser.add_argument(
        "--task-log-dir",
        default=None,
        help="显式指定 task_log_dir（跳过 cwd→repo_root 推导）",
    )

    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # ===== init =====
    p_init = sub.add_parser(
        "init",
        help="初始化运行环境，输出路径与 session 信息",
        formatter_class=_EPILOG_FMT,
        epilog="""\
做什么：探测 repo_root，落盘 run.json / active.json，自举 review schema 与
portable-timeout，识别当前 cc session_id；不写 STATE_JSON（由 state init-run 负责）。

stdout（默认 JSON 模式）:
  {"repo_root", "proj_key", "task_log_dir", "run_ts", "run_dir", "state_json",
   "state_md", "index_file", "schema_path", "run_events", "run_json",
   "active_json", "session_id", "transcript_path", "session_source",
   "needs_resume": bool, "resume_state_json": str|null, "mode", "fresh"}

stdout（--shell-exports，已 Deprecated 0.2）:
  一系列 export NPC_XXX='...' 行（stderr 打 deprecation warning）

exit code:
  0  正常（非 git 仓库 / 缺 ~/.claude/projects/<PROJ_KEY> 仅告警，仍 exit 0）
  3  致命环境错误
""",
    )
    p_init.add_argument("--auto", action="store_true", help="标记 auto 模式")
    p_init.add_argument("--fresh", action="store_true", help="忽略 in-progress 旧 run")
    p_init.add_argument(
        "--shell-exports",
        action="store_true",
        help="输出 export KEY=VALUE 行供 eval 注入环境",
    )
    p_init.set_defaults(handler=_make_handler("init_cmd", "run"), _cmd_path="init")

    # ===== resume detect =====
    p_resume = sub.add_parser("resume", help="续跑相关")
    sub_resume = p_resume.add_subparsers(dest="resume_cmd", required=True)
    p_resume_detect = sub_resume.add_parser(
        "detect",
        help="检测续跑断点",
        formatter_class=_EPILOG_FMT,
        epilog="""\
仅在 npc init 报告 needs_resume=true 后调用。

stdout:
  {"needs_resume": true, "state_json": "<path>", "last_updated_at": "<iso>",
   "completed_changes": <int>, "total_changes": <int>, "next_seq": <int>,
   "next_change_id": "<str>", "next_phase": "<implement|review-r0|fix-rN|review-rN|archive>",
   "current_round": <int>, "blocking_trend": [<int>, ...]}

exit code:
  0  成功
  3  环境错（未找到 in-progress STATE_JSON）
""",
    )
    p_resume_detect.set_defaults(
        handler=_make_handler("resume", "detect"), _cmd_path="resume detect"
    )

    # ===== state =====
    p_state = sub.add_parser("state", help="STATE_JSON 读写")
    sub_state = p_state.add_subparsers(dest="state_cmd", required=True)

    p_state_init = sub_state.add_parser(
        "init-run",
        help="首次创建 STATE_JSON",
        formatter_class=_EPILOG_FMT,
        epilog="""\
写完整 STATE_JSON header（schema_version=2 / plan_order / progress=[各项 pending]）+
渲染 STATE_MD + touch RUN_EVENTS。

stdout:
  {"ok": true, "state_json": "<path>", "total_changes": <int>}

exit code:
  0  成功
  2  --plan-order 不是合法 JSON 数组
  3  环境错（未定位到 run）
""",
    )
    p_state_init.add_argument(
        "--plan-order", required=True, help='JSON 数组字符串，如 \'["a","b"]\''
    )
    p_state_init.add_argument(
        "--goal",
        default=None,
        help="本 run 的原始目标（人设的一句话；summary render 用于目标覆盖对照）",
    )
    p_state_init.set_defaults(
        handler=_make_handler("state", "init_run"), _cmd_path="state init-run"
    )

    p_state_note = sub_state.add_parser(
        "note",
        help="编排日志：--text 追加一条（意图备忘/steering）/ --consume 打消费水位",
        formatter_class=_EPILOG_FMT,
        epilog="""\
notes.jsonl（<run_dir>/notes.jsonl，追加式）承载编排器意图备忘与人的中途转向
指令（steering）。消费进度用 state.notes_consumed_at 水位标记；
`npc status --brief` 只带出水位之后的未消费 note，主循环在 change 边界消费后
`--consume` 打水位。

stdout（--text）:
  {"ok": true, "path": "<notes.jsonl>", "ts": "<iso>"}
stdout（--consume）:
  {"ok": true, "consumed_up_to": "<iso>"}

exit code:
  0  成功
  2  --text 与 --consume 都缺或同给
  3  环境错（未定位到 run / STATE_JSON 缺失）
""",
    )
    p_state_note.add_argument("--text", default=None, help="note 正文")
    p_state_note.add_argument(
        "--consume", action="store_true", help="把当前全部未消费 note 标记为已消费"
    )
    p_state_note.add_argument(
        "--source", default=None, help="来源标记（orchestrator/user/...；默认 orchestrator）"
    )
    p_state_note.set_defaults(
        handler=_make_handler("state", "note"), _cmd_path="state note"
    )

    p_state_get = sub_state.add_parser(
        "get",
        help="按 jq 路径取值",
        formatter_class=_EPILOG_FMT,
        epilog="""\
无固定 schema：stdout 是 <jq_path> 表达式在 STATE_JSON 上的求值结果（任意 JSON 值，
非对象包裹）。例：npc state get '.progress[0].status' -> "pending"

exit code:
  0  成功
  1  jq 表达式非法或字段不存在
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_state_get.add_argument("jq_path", help="jq 路径表达式")
    p_state_get.set_defaults(handler=_make_handler("state", "get"), _cmd_path="state get")

    p_state_add = sub_state.add_parser(
        "add-change",
        help="向 progress 追加 change 条目",
        formatter_class=_EPILOG_FMT,
        epilog="""\
追加一个 pending 初态的 progress 条目；未给 --base 则自动计算并 mkdir。

stdout:
  {"ok": true, "seq": <int>, "change_id": "<str>", "base": "<path>"}

exit code:
  0  成功
  1  seq 非法（如与已有条目冲突）
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_state_add.add_argument("seq", type=int)
    p_state_add.add_argument("change_id")
    p_state_add.add_argument("--base", default=None, help="覆盖 base 路径")
    p_state_add.set_defaults(
        handler=_make_handler("state", "add_change"), _cmd_path="state add-change"
    )

    p_state_set = sub_state.add_parser(
        "set-progress",
        help="更新 progress 条目字段",
        formatter_class=_EPILOG_FMT,
        epilog="""\
只更新传入的字段（全部可选，但至少传一个）；同时刷新 last_updated_at 并重渲染 STATE_MD。

stdout:
  {"ok": true, "seq": <int>, ...（回显本次更新的字段）}

exit code:
  0  成功
  1  seq 超出 progress 数组长度
  2  一个字段都未传
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_state_set.add_argument("seq", type=int)
    p_state_set.add_argument("--status", default=None)
    p_state_set.add_argument("--reason", default=None)
    p_state_set.add_argument("--implement-commit", default=None)
    p_state_set.add_argument("--archive-commit", default=None)
    p_state_set.add_argument("--total-rounds", type=int, default=None)
    p_state_set.add_argument("--stale-verdict", default=None)
    p_state_set.set_defaults(
        handler=_make_handler("state", "set_progress"), _cmd_path="state set-progress"
    )

    p_state_fin = sub_state.add_parser(
        "finalize",
        help="收尾：判定顶层 status",
        formatter_class=_EPILOG_FMT,
        epilog="""\
根据全部 progress[].status 推算顶层 status：全 archived -> completed；
部分 archived 但有 failed/skipped-auto -> completed-with-issues；
任何 needs-user-decision -> 不动 status，报错。

stdout（成功）:
  {"ok": true, "final_status": "<str>", "archived": <int>, "failed": <int>,
   "skipped": <int>, "total": <int>}

exit code:
  0  成功
  1  存在 needs-user-decision，不能 finalize
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_state_fin.set_defaults(
        handler=_make_handler("state", "finalize"), _cmd_path="state finalize"
    )

    p_state_rep = sub_state.add_parser(
        "repair",
        help="自愈漂移：把 commit 已不在 git 的 progress 项重置为 pending（旧 base 进 .repaired/ 留存）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
目标 seq：显式 --seqs 给定，否则跑 scan_state_drift 自动探测。逐项把 progress
条目重置为 pending（清空 phases/trend），旧 base 目录整体 mv 到
<run_dir>/.repaired/，若该 change 已被 openspec archive 会 mv 回 active。
repair_log 单向追加（不删除历史）。

stdout（无漂移）:
  {"ok": true, "repaired": [], "message": "no drift detected; nothing to repair"}

stdout（有修复）:
  {"ok": true, "repaired": [{"ts","seq","change_id","previous_status",
   "audit_base","openspec_moved_back"}, ...], "audit_root": "<path>"}

exit code:
  0  成功（含"无需修复"）
  2  --seqs 含非整数 token
  3  环境错（STATE_JSON 不存在 / git 缺失）
""",
    )
    p_state_rep.add_argument(
        "--seqs", default=None, help="显式 CSV（如 1,3,5）；省略则自动取 scan_state_drift 结果"
    )
    p_state_rep.add_argument(
        "--auto",
        action="store_true",
        help="标记主 session 自动调用（行为本就无交互，此参数仅做日志标记）",
    )
    p_state_rep.set_defaults(
        handler=_make_handler("repair", "state_repair"), _cmd_path="state repair"
    )

    # ===== phase =====
    p_phase = sub.add_parser("phase", help="Phase 计时与事件")
    sub_phase = p_phase.add_subparsers(dest="phase_cmd", required=True)

    p_phase_enter = sub_phase.add_parser(
        "enter",
        help="进入 phase",
        formatter_class=_EPILOG_FMT,
        epilog="""\
<phase> 取值：implement / review-r0 / fix-rN / review-rN (N>=1) / archive。
设置 progress[seq-1].phases.<phase>={status:in-progress, started_at, started_ms}，
追加 phase.start 事件到 per-change events.jsonl 与 RUN_EVENTS。base 缺失时自动
计算 $RUN_DIR/<NNN>-<change_id> 并 mkdir。

stdout:
  {"ok": true, "seq": <int>, "phase": "<str>", "base": "<path>", "started_at": "<iso>"}

exit code:
  0  成功
  1  seq 超出 progress 数组长度
  2  phase 名不合法
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_phase_enter.add_argument("seq", type=int)
    p_phase_enter.add_argument("phase")
    p_phase_enter.set_defaults(
        handler=_make_handler("events", "phase_enter"), _cmd_path="phase enter"
    )

    p_phase_exit = sub_phase.add_parser(
        "exit",
        help="退出 phase",
        formatter_class=_EPILOG_FMT,
        epilog="""\
自动算 duration_ms = now - phases.<phase>.started_ms；--extra 的 JSON 对象合并进
phases 字段与事件（如 '{"commit":"abc1234","tasks":8,"tests":"pass"}'）。事件名
按 phase-base 推导（implement/review/fix/archive）+ .done 或 .failed 后缀。

stdout:
  {"ok": true, "seq": <int>, "phase": "<str>", "duration_ms": <int>, "status": "done|failed"}

exit code:
  0  成功
  1  seq 超出 progress 数组长度
  2  phase 名不合法 / --extra 非法 JSON
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_phase_exit.add_argument("seq", type=int)
    p_phase_exit.add_argument("phase")
    p_phase_exit.add_argument("--status", required=True, choices=["done", "failed"])
    p_phase_exit.add_argument("--extra", default="{}", help="合并到 phases 字段与事件的额外 JSON")
    p_phase_exit.set_defaults(
        handler=_make_handler("events", "phase_exit"), _cmd_path="phase exit"
    )

    p_phase_rot = sub_phase.add_parser(
        "rotate",
        help="原子完成：(上一 in-progress phase 退出) + (进入新 phase)；fix loop 推荐用法",
        formatter_class=_EPILOG_FMT,
        epilog="""\
把当前所有 status=in-progress 的 phase 以 --prev-status 退出（正常 <=1 个），
再进入 --to 指定的新 phase；避免主 session 漏调 phase enter 造成 started_at=null。

stdout:
  {"ok": true, "seq": <int>, "to_phase": "<str>", "started_at": "<iso>",
   "base": "<path>", "prev_phases_closed": [{"phase","duration_ms"}, ...]}

exit code:
  0  成功
  1  seq 超出 progress 数组长度
  2  --to 或 --prev-status 非法 / --prev-extra 非法 JSON
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_phase_rot.add_argument("--seq", type=int, required=True)
    p_phase_rot.add_argument("--to", dest="to_phase", required=True, help="新 phase 名")
    p_phase_rot.add_argument(
        "--prev-status", default="done", choices=["done", "failed"], help="退出上一 phase 的状态"
    )
    p_phase_rot.add_argument(
        "--prev-extra", default="{}", help="合并到上一 phase 字段与事件的额外 JSON"
    )
    p_phase_rot.set_defaults(
        handler=_make_handler("events", "phase_rotate"), _cmd_path="phase rotate"
    )

    # ===== review =====
    p_review = sub.add_parser("review", help="Review JSON 解析与 trend")
    sub_review = p_review.add_subparsers(dest="review_cmd", required=True)

    p_review_parse = sub_review.add_parser(
        "parse",
        help="解析 review.json 派生指标",
        formatter_class=_EPILOG_FMT,
        epilog="""\
读 schema-validated review.json，计算 blocking（severity in critical/high 且
in_scope=true 的计数）/ advisory / verdict / categories。

stdout:
  {"verdict": "<str>", "blocking": <int>, "advisory": <int>,
   "categories": ["<str>", ...],
   "blocking_findings": [{"id","severity","category","title","file","line_range"}, ...]}

exit code:
  0  成功
  1  review.json 不存在 / 非法 JSON / schema 不合法
""",
    )
    p_review_parse.add_argument("review_json", help="review.json 文件路径")
    p_review_parse.set_defaults(
        handler=_make_handler("review", "parse"), _cmd_path="review parse"
    )

    p_review_trend = sub_review.add_parser(
        "update-trend",
        help="更新 blocking_trend 等",
        formatter_class=_EPILOG_FMT,
        epilog="""\
把 --metrics（review parse 的 JSON 输出）的 blocking 追加到 progress[seq-1]
.blocking_trend；严格下降则 rounds_since_strict_decrease 清零，否则 +1；
categories 合并进 categories_seen（去重保序）。

stdout:
  {"ok": true, "blocking_trend": [<int>, ...], "rounds_since_strict_decrease": <int>,
   "categories_seen": ["<str>", ...]}

exit code:
  0  成功
  1  seq 超出 progress 数组长度
  2  --metrics 非法 JSON 或缺 blocking/categories 字段
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_review_trend.add_argument("seq", type=int)
    p_review_trend.add_argument("--metrics", required=True, help="review parse 的 JSON 输出")
    p_review_trend.set_defaults(
        handler=_make_handler("trend", "update_trend"), _cmd_path="review update-trend"
    )

    p_review_stale = sub_review.add_parser(
        "check-stale",
        help="检查 stale 判定",
        formatter_class=_EPILOG_FMT,
        epilog="""\
读 progress[seq-1].rounds_since_strict_decrease，>= 3 视为 stale（纯查询，不写 state）。

stdout:
  {"stale": <bool>, "rounds_since_strict_decrease": <int>,
   "blocking_trend": [<int>, ...], "threshold": 3}

exit code:
  0  成功（无论 stale 是否为 true）
  1  seq 超出 progress 数组长度
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_review_stale.add_argument("seq", type=int)
    p_review_stale.set_defaults(
        handler=_make_handler("trend", "check_stale"), _cmd_path="review check-stale"
    )

    p_review_run = sub_review.add_parser(
        "run",
        help="跑完整一轮 review（focus + codex exec + parse + update-trend + check-stale）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
做什么：渲染 round-N.focus.md -> phase enter review-rN -> 起 codex/claude 子进程
（stdin=focus.md, 超时/失败重试 --retries 次）-> parse review.json -> 一次性
phase exit + update_trend + categories_seen 合并；blocking>0 时自动渲染下一轮
的 round-(N+1).fix.findings.md。

stdout（成功）:
  {"ok": true, "seq": <int>, "round": <int>, "change_id": "<str>",
   "verdict": "<str>", "blocking": <int>, "advisory": <int>,
   "categories": [...], "stale": <bool>, "rounds_since_strict_decrease": <int>,
   "blocking_trend": [...], "review_json": "<path>", "events_path": "<path>",
   "focus_path": "<path>", "findings_path": "<path>|null",
   "project_context_source": "openspec/project.md|CLAUDE.md|both|default"}

stdout（失败）:
  {"ok": false, "seq": <int>, "round": <int>, "error": "codex-exec-failed",
   "detail": "<str>", "attempts": <int>, "events_path": "<path>"}

exit code:
  0  成功
  1  业务失败（codex/claude 子进程失败或 review.json schema 不合法，已重试耗尽）
  4  依赖缺失（codex/claude/portable-timeout 未在 PATH 中）
""",
    )
    p_review_run.add_argument("--seq", type=int, required=True)
    p_review_run.add_argument("--round", dest="round_n", type=int, required=True)
    p_review_run.add_argument("--retries", type=int, default=1, help="codex 失败重试次数")
    p_review_run.add_argument("--timeout", type=int, default=900, help="单次 codex 超时秒数")
    p_review_run.add_argument("--codex-bin", default=None, help="覆盖 codex 路径")
    p_review_run.add_argument("--portable-timeout", default=None, help="覆盖 portable-timeout 路径")
    p_review_run.add_argument(
        "--engine",
        choices=["codex", "claude"],
        default=None,
        help="覆盖 review 引擎（默认从配置文件 [review].engine 读，缺省 codex）",
    )
    p_review_run.add_argument(
        "--config",
        default=None,
        help=(
            "显式指定 TOML 配置文件路径；"
            "省略则按 <repo>/.npc/config.toml → ~/.config/npc/config.toml 查找"
        ),
    )
    p_review_run.set_defaults(
        handler=_make_handler("pipeline", "cli_review_run"), _cmd_path="review run"
    )

    # ===== focus =====
    p_focus = sub.add_parser("focus", help="Codex review focus 文本渲染")
    sub_focus = p_focus.add_subparsers(dest="focus_cmd", required=True)
    p_focus_render = sub_focus.add_parser(
        "render",
        help="渲染 focus 文本到文件",
        formatter_class=_EPILOG_FMT,
        epilog="""\
--project-context 未传时自动从 <repo>/openspec/project.md 与 <repo>/CLAUDE.md
抽"评审重点"/"威胁模型"/"Review Context"/"Threat Model" 章节；都无则用默认中性约束。

stdout:
  {"ok": true, "output": "<path>", "bytes": <int>,
   "project_context_source": "openspec/project.md|CLAUDE.md|both|default"}

exit code:
  0  成功
  2  round>=1 但缺 --implement-commit
  3  环境错（--output 所在目录不可写等）
""",
    )
    p_focus_render.add_argument("--round", dest="round_n", type=int, required=True)
    p_focus_render.add_argument("--change-id", required=True)
    p_focus_render.add_argument("--implement-commit", default=None)
    p_focus_render.add_argument("--output", required=True)
    p_focus_render.add_argument(
        "--project-context",
        default=None,
        help="自定义 PROJECT_REVIEW_CONTEXT 文件；未传则从 openspec/project.md + CLAUDE.md 抽",
    )
    p_focus_render.set_defaults(
        handler=_make_handler("focus", "render"), _cmd_path="focus render"
    )

    # ===== fixer =====
    p_fixer = sub.add_parser("fixer", help="Fixer prompt 片段抽取")
    sub_fixer = p_fixer.add_subparsers(dest="fixer_cmd", required=True)
    p_fixer_find = sub_fixer.add_parser(
        "findings",
        help="抽 in_scope blocking findings",
        formatter_class=_EPILOG_FMT,
        epilog="""\
从 review.json 抽 in_scope=true 且 severity in {critical,high} 的 findings，
渲染为 markdown 片段（每条一个 H2 段落）写到 --output-fragment，供 Fixer prompt 拼接。

stdout:
  {"ok": true, "output": "<path>", "count": <int>, "categories": ["<str>", ...]}

exit code:
  0  成功
  1  review.json 非法 JSON / schema 不合法
  3  --review 文件不存在
""",
    )
    p_fixer_find.add_argument("--review", required=True, help="review.json 路径")
    p_fixer_find.add_argument("--output-fragment", required=True, help="输出 markdown 路径")
    p_fixer_find.set_defaults(
        handler=_make_handler("fixer", "findings"), _cmd_path="fixer findings"
    )

    # ===== archive =====
    p_archive = sub.add_parser("archive", help="Archive 前校验与全流程")
    sub_archive = p_archive.add_subparsers(dest="archive_cmd", required=True)
    p_archive_pre = sub_archive.add_parser(
        "precheck",
        help="commit chain 一致性",
        formatter_class=_EPILOG_FMT,
        epilog="""\
取 progress[seq-1].implement_commit + 所有 phases.fix-rN.commit，逐个跑
git merge-base --is-ancestor <c> HEAD，收集不在当前分支历史里的 commit。

stdout（成功）:
  {"ok": true, "expected": ["<hash>", ...], "missing": []}

stdout（失败，仍打印以便 jq 取 missing）:
  {"ok": false, "expected": [...], "missing": ["<hash>", ...]}

exit code:
  0  chain 完整
  1  有缺失
""",
    )
    p_archive_pre.add_argument("seq", type=int)
    p_archive_pre.set_defaults(
        handler=_make_handler("git_chain", "precheck"), _cmd_path="archive precheck"
    )

    p_archive_run = sub_archive.add_parser(
        "run",
        help="archive 全流程：precheck → openspec validate → openspec archive → git commit → 状态装订",
        formatter_class=_EPILOG_FMT,
        epilog="""\
做什么：phase enter archive -> archive precheck -> openspec validate --strict ->
openspec archive --yes -> git add openspec/ + commit -> phase exit + state
set-progress status=archived。任一步失败即 phase exit failed + reason 记录。

stdout（成功）:
  {"ok": true, "seq": <int>, "change_id": "<str>", "archive_commit": "<hash>",
   "total_rounds": <int>, "final_status": "passed (round N)"}

stdout（失败）:
  {"ok": false, "seq": <int>, "change_id": "<str>",
   "error": "commit-chain-broken|openspec-validate-failed|openspec-archive-failed|git-commit-failed",
   "stderr_tail": "<str>"}

exit code:
  0  成功
  1  业务失败
  4  依赖缺失（openspec 未安装）
""",
    )
    p_archive_run.add_argument("--seq", type=int, required=True)
    p_archive_run.add_argument("--openspec-bin", default=None, help="覆盖 openspec 路径")
    p_archive_run.set_defaults(
        handler=_make_handler("pipeline", "cli_archive_run"), _cmd_path="archive run"
    )

    # ===== implement =====
    p_implement = sub.add_parser("implement", help="Implement 阶段记录")
    sub_implement = p_implement.add_subparsers(dest="implement_cmd", required=True)
    p_impl_rec = sub_implement.add_parser(
        "record",
        help="喂入 sub-agent 的 RESULT 行，完成 phase exit + state set-progress",
        formatter_class=_EPILOG_FMT,
        epilog="""\
RESULT 行格式（失败时 commit=- tests=fail）：
  RESULT: commit=<hash> tasks=<n> tests=<pass|fail> summary=<path> notes=<str|->

做什么：解析 RESULT 行 -> 校验 commit!=- 且 tests=pass（否则 reason=implementer）
-> 校验 summary 文件存在（除非 --no-summary-check，否则 reason=summary-missing）
-> 校验 commit 在 repo 中（git cat-file -e，否则 reason=commit-not-found）
-> phase exit implement done + state set-progress status=reviewing implement_commit=<hash>。

stdout（成功）:
  {"ok": true, "seq": <int>, "change_id": "<str>", "commit": "<hash>",
   "tasks": <int>, "tests": "pass", "summary": "<path>"}

stdout（失败）:
  {"ok": false, "seq": <int>, "error": "result-line-missing|implementer-failed|summary-missing|commit-not-found", ...}

exit code:
  0  成功
  1  业务失败（RESULT 校验不过 / summary 缺失 / commit 不在 git 历史里）
  2  用法错误（缺 --seq，或 --result 与 --result-file 都未给）
""",
    )
    p_impl_rec.add_argument("--seq", type=int, required=True)
    p_impl_rec.add_argument(
        "--result", default=None, help="sub-agent 的 RESULT 行原文（与 --result-file 二选一）"
    )
    p_impl_rec.add_argument("--result-file", default=None, help="从文件读 RESULT 行（最后一行）")
    p_impl_rec.add_argument(
        "--no-summary-check", action="store_true", help="跳过 summary.md 存在性校验"
    )
    p_impl_rec.set_defaults(
        handler=_make_handler("pipeline", "cli_implement_record"), _cmd_path="implement record"
    )

    p_impl_run = sub_implement.add_parser(
        "run",
        help="跑 coder 子进程完成 implement（render prompt → coder 后端 → 抽 RESULT → record）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
做什么：phase enter implement -> 渲染 §A 模板 -> 起 coder 后端子进程（claude-cli
runner 走 headless claude -p、可经 provider env_file 路由到 Anthropic 兼容端点；
codex-cli runner 走 codex exec）-> 从 stdout 尾部抽 RESULT 行（缺失则合成失败
RESULT）-> 等价于 implement record。后端未产出 RESULT 不会让 phase 悬挂。

stdout（成功，implement record 的字段 + 以下）:
  {..., "backend": "<provider 名>", "model": "<str>|null", "coder_exit": <int>}

stdout（失败）:
  {"ok": false, "seq": <int>, "error": "<implement record 的 error 码，或
   coder-timeout|coder-subprocess-error>", ...}

exit code:
  0  成功
  1  业务失败（RESULT 校验不过等）
  2  配置错误（含未注册 provider）
  3  环境错（缺 state / provider env 文件问题）
  4  依赖缺失（coder 可执行文件 / env_file 未找到）
""",
    )
    p_impl_run.add_argument("--seq", type=int, required=True)
    p_impl_run.add_argument("--change-id", default=None, help="可选；与 state 中的 seq 一致性校验")
    p_impl_run.add_argument(
        "--backend",
        default=None,
        help="覆盖 coder 后端（内置 claude/mimo/codex 或 [providers.*] 自定义名；默认从 [coder].backend 读）",
    )
    p_impl_run.add_argument("--timeout", type=int, default=None, help="coder 子进程超时秒数")
    p_impl_run.add_argument("--config", default=None, help="显式 TOML 配置路径")
    p_impl_run.set_defaults(
        handler=_make_handler("coder", "cli_implement_run"), _cmd_path="implement run"
    )

    # ===== fix =====
    p_fix = sub.add_parser("fix", help="Fix 阶段记录")
    sub_fix = p_fix.add_subparsers(dest="fix_cmd", required=True)
    p_fix_rec = sub_fix.add_parser(
        "record",
        help="喂入 fixer sub-agent 的 RESULT 行，完成 phase exit + state set-progress",
        formatter_class=_EPILOG_FMT,
        epilog="""\
RESULT 行格式：
  RESULT: commit=<hash> fixed=<n> tests=<pass|fail> summary=<path>
          categories_scanned=<csv> regressions_added=<csv|-> notes=<str>

校验规则同 implement record（commit=- 或 tests!=pass -> reason=implementer；
summary 缺失 -> reason=summary-missing；commit 不在 repo -> reason=commit-not-found）。
成功后 phase exit fix-rN done + state set-progress status=in-fix-loop（等下一轮 review）。

stdout（成功）:
  {"ok": true, "seq": <int>, "round": <int>, "change_id": "<str>",
   "commit": "<hash>", "fixed": <int>, "tests": "pass", "summary": "<path>"}

exit code:
  0  成功
  1  业务失败
  2  用法错误（缺 --seq/--round，或 --result 与 --result-file 都未给）
""",
    )
    p_fix_rec.add_argument("--seq", type=int, required=True)
    p_fix_rec.add_argument("--round", dest="round_n", type=int, required=True)
    p_fix_rec.add_argument("--result", default=None)
    p_fix_rec.add_argument("--result-file", default=None)
    p_fix_rec.add_argument("--no-summary-check", action="store_true")
    p_fix_rec.set_defaults(
        handler=_make_handler("pipeline", "cli_fix_record"), _cmd_path="fix record"
    )

    p_fix_run = sub_fix.add_parser(
        "run",
        help="跑 coder 子进程完成 fix-rN（render fix prompt → coder 后端 → 抽 RESULT → record）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
做什么：phase enter fix-rN -> 渲染 §B 模板 -> coder 后端子进程 -> 抽 RESULT ->
等价于 fix record。超时/子进程异常时 progress.status 落 needs-user-decision
（比 implement run 更保守，因为 fix loop 失败通常需要人工介入）。

stdout（成功，fix record 的字段 + 以下）:
  {..., "backend": "<provider 名>", "model": "<str>|null", "coder_exit": <int>}

exit code:
  0  成功
  1  业务失败
  2  配置错误（含未注册 provider）/ 缺 --round
  3  环境错
  4  依赖缺失
""",
    )
    p_fix_run.add_argument("--seq", type=int, required=True)
    p_fix_run.add_argument("--round", dest="round_n", type=int, required=True)
    p_fix_run.add_argument("--change-id", default=None)
    p_fix_run.add_argument(
        "--backend", default=None,
        help="覆盖 coder 后端（内置或 [providers.*] 自定义名）",
    )
    p_fix_run.add_argument("--timeout", type=int, default=None)
    p_fix_run.add_argument("--config", default=None)
    p_fix_run.set_defaults(
        handler=_make_handler("coder", "cli_fix_run"), _cmd_path="fix run"
    )

    # ===== change =====（v1.5：单 change 内环下沉）
    p_change = sub.add_parser("change", help="单 change 内环编排（v1.5）")
    sub_change = p_change.add_subparsers(dest="change_cmd", required=True)
    p_change_run = sub_change.add_parser(
        "run",
        help="一条命令跑完单 change 内环：implement → review → fix 循环 → archive",
        formatter_class=_EPILOG_FMT,
        epilog="""\
把 v2/v3 skill 里逐轮搬运 JSON 的 review-fix 循环整体下沉。复用既有 pipeline
（implement/fix run、review run、archive run）与 auto-decide，不重写。

决策点分档：
- --auto（或 state.mode=auto）：内部调 auto-decide 一路跑完，返回一行终态 JSON。
- 交互档：跑到决策点（stale / max-rounds / implementer/fixer/codex-failed /
  archive-failed）带 status=needs-decision 退出（exit 5），pending_decision 装订进
  state；主 session 问人后用 --decision <action> 续跑。

stdout（终态）:
  {"ok": true, "seq": <int>, "change_id": "<str>", "status": "archived",
   "rounds": <int>, "archive_commit": "<hash>", "blocking_trend": [...], "pointer": {...}}
stdout（决策点，exit 5）:
  {"ok": false, "seq": <int>, "change_id": "<str>", "status": "needs-decision",
   "trigger": "<str>", "phase": "<str>", "round": <int>, "suggested": "<action>",
   "blocking_trend": [...], "categories_seen": [...], "pointer": {...}}

exit code:
  0  archived
  1  终态失败（skipped / failed / aborted）
  5  needs-decision（仅交互档；--auto 下不会出现）
  2  用法错（--decision 无 pending_decision / 终态重跑未带 --from）
  3  环境错
  4  依赖缺失（coder/review 引擎可执行文件）
""",
    )
    p_change_run.add_argument("--seq", type=int, required=True)
    p_change_run.add_argument(
        "--from", dest="from_phase", default=None,
        choices=["implement", "review", "fix", "archive"],
        help="断点重入起点（默认按 state 推导）",
    )
    p_change_run.add_argument(
        "--decision", default=None,
        choices=["continue-retry", "skip", "force-archive", "abort"],
        help="消费上次 needs-decision 的人工裁定",
    )
    p_change_run.add_argument(
        "--max-rounds", dest="max_rounds", type=int, default=20, help="fix 轮数上限"
    )
    p_change_run.add_argument("--auto", action="store_true", help="决策点走 auto-decide")
    p_change_run.add_argument(
        "--backend", default=None,
        help="覆盖 coder 后端（内置或 [providers.*] 自定义名）",
    )
    p_change_run.add_argument(
        "--coder-timeout", dest="coder_timeout", type=int, default=None,
        help="coder 子进程超时秒数",
    )
    p_change_run.add_argument(
        "--review-retries", dest="review_retries", type=int, default=1
    )
    p_change_run.add_argument(
        "--review-timeout", dest="review_timeout", type=int, default=900
    )
    p_change_run.add_argument(
        "--engine", choices=["codex", "claude"], default=None, help="覆盖 review 引擎"
    )
    p_change_run.add_argument("--config", default=None, help="显式 TOML 配置路径")
    p_change_run.set_defaults(
        handler=_make_handler("change", "cli_run"), _cmd_path="change run"
    )

    # ===== integrate =====（v1.5：worktree 整合编排下沉）
    p_integrate = sub.add_parser(
        "integrate",
        help="worktree 产物整合进 main：verify manifest → cherry-pick → hash 翻译 → record → verify tests（fail 则 revert）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
替代 v3 skill Step 9 的整合编排段（cherry-pick + sed 换 hash + record +
verify tests + revert），单命令、失败自动收拾现场（main 保持绿）。
hash 翻译是必要项：archive precheck 用 merge-base --is-ancestor 校验 chain，
worktree 原始 hash 不在 main 链上会被判 chain-broken。

stdout（成功）:
  {"ok": true, "seq": <int>, "change_id": "<str>", "worktree_commit": "<hash>",
   "integrated_commit": "<hash>", "verify_tests": "pass|skipped",
   "files": {"present": <int>, "total": <int>}}
stdout（失败）:
  {"ok": false, "seq": <int>, "step": "verify-manifest|cherry-pick|record|verify-tests",
   "reason": "<str>", "reverted": "<hash>|null", ...}

exit code:
  0  成功
  1  任一步失败（stdout 带 step 定位；现场已收拾干净）
  2  用法错
  3  环境错
""",
    )
    p_integrate.add_argument("--seq", type=int, required=True)
    p_integrate.add_argument(
        "--result", default=None, help="implementer 的 RESULT 行原文（与 --result-file 二选一）"
    )
    p_integrate.add_argument("--result-file", default=None, help="从文件读 RESULT 行")
    p_integrate.add_argument(
        "--manifest", default=None, help="MANIFEST 行给出的 manifest JSON 路径"
    )
    p_integrate.add_argument(
        "--no-verify-tests", dest="no_verify_tests", action="store_true",
        help="跳过整合后测试复跑（不推荐）",
    )
    p_integrate.set_defaults(
        handler=_make_handler("integrate", "cli_integrate"), _cmd_path="integrate"
    )

    # ===== verify =====
    p_verify = sub.add_parser("verify", help="质量门 + 路由不变量校验")
    sub_verify = p_verify.add_subparsers(dest="verify_cmd", required=True)
    p_verify_tests = sub_verify.add_parser(
        "tests",
        help="按 repo 清单探测并真实复跑测试（不裸信自报）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
测试命令解析优先级：[verify].test 配置覆盖 > 有 pyproject.toml/pytest.ini/tests/
-> "python3 -m pytest -q" > package.json scripts.test -> "npm test" > Makefile
含 test: 目标 -> "make test" > 都没有 -> 报错。命令经 shlex.split + shell=False
执行，杜绝注入。

stdout:
  {"ok": <bool>, "cmd": "<str>", "exit_code": <int>, "passed": <bool>,
   "tail": "<stdout/stderr 合并后末尾 30 行>"}

exit code:
  0  测试通过
  1  测试失败
  1  配置加载失败（config_error）
  3  未探测到测试命令 / repo_root 定位失败
""",
    )
    p_verify_tests.add_argument("--config", default=None, help="显式 TOML 配置路径")
    p_verify_tests.set_defaults(
        handler=_make_handler("verify", "run_tests"), _cmd_path="verify tests"
    )
    p_verify_routing = sub_verify.add_parser(
        "routing",
        help="校验 coder/review 路由不变量（生成⊥验证 + 廉价层只许执行）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
规则：1) coder backend 必须是已注册 provider（内置 + [providers.*]），review.engine
必须在受支持列表；2) coder 与 review 不得解析到同一执行身份（自己评自己，覆盖
全局 + per-phase 覆盖）；3) review 路由含任何带 env_file 的廉价层 provider
（engine/claude_bin/claude_model 命中其名或 model）一律 violation（cheap_exec_only）。

stdout:
  {"ok": <bool>, "coder_backend": "<str>", "coder_phase_backends": {"<phase>":"<backend>", ...},
   "review_engine": "<str>", "violations": [{"rule","detail"}, ...]}

exit code:
  0  无 violation
  1  有 violation，或配置加载失败
  3  repo_root 定位失败
""",
    )
    p_verify_routing.add_argument("--config", default=None, help="显式 TOML 配置路径")
    p_verify_routing.set_defaults(
        handler=_make_handler("verify", "run_routing"), _cmd_path="verify routing"
    )
    p_verify_manifest = sub_verify.add_parser(
        "manifest",
        help="核验并行 implementer 产出：RESULT 行 plan-only 判定 + manifest 文件存在性/sha 核对",
        formatter_class=_EPILOG_FMT,
        epilog="""\
RESULT 行接受两种格式：npc 契约 "RESULT: commit=<hash> tasks=.. tests=.. summary=.."
（manifest 路径经 --manifest 传入）；legacy JSON（architect-swarm）
'RESULT: {"status":..,"files_written":N,"manifest":".."}'。verdict 语义：
plan_only（无 RESULT/commit=-/自报 plan-only/manifest 缺失）；error（自报 error）；
code（有产出；文件缺失或 sha 不符时 verdict 仍是 code 但 ok=false）。

stdout:
  {"ok": <bool>, "verdict": "plan_only|error|code", "reason": "<str>|null",
   "commit": "<str>|null",
   "files": {"ok": <bool>, "reason": "<str>|null", "present": <int>,
             "missing": [...], "sha_mismatch": [...], "total": <int>}}

exit code:
  0  verdict=code 且 manifest 全部核对通过
  1  其余（plan_only / error / 文件缺失 / sha 不符）
  2  用法错
""",
    )
    p_verify_manifest.add_argument(
        "--result", required=True, help="implementer 的 RESULT 行原文（npc key=value 或 legacy JSON）"
    )
    p_verify_manifest.add_argument(
        "--manifest", default=None, help="MANIFEST 行给出的 manifest JSON 路径"
    )
    p_verify_manifest.set_defaults(
        handler=_make_handler("verify", "run_manifest"), _cmd_path="verify manifest"
    )
    p_verify_tasks = sub_verify.add_parser(
        "tasks",
        help="tasks.md checkbox 完成度派生计数（可与 implement 自报交叉验证）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
change 是调度量子，task 绝不进主 context——主 session 与人只看两个数。
解析 openspec/changes/<id>/tasks.md 的 - [ ] / - [x] 项；--seq 给定时与 state
里 implement RESULT 自报的 tasks= 计数交叉验证。

stdout:
  {"ok": <bool>, "change": "<str>", "tasks_done": <int>, "tasks_total": <int>,
   "claim": <int>|null, "consistent": <bool>|null}

exit code:
  0  一致或无 claim 可比
  1  claim 存在且与 tasks.md 计数不一致
  2  缺 --change
  3  change 目录 / tasks.md 缺失
""",
    )
    p_verify_tasks.add_argument("--change", required=True, help="change-id")
    p_verify_tasks.add_argument(
        "--seq", type=int, default=None, help="可选；与 state 自报计数交叉验证"
    )
    p_verify_tasks.set_defaults(
        handler=_make_handler("verify", "run_tasks_check"), _cmd_path="verify tasks"
    )

    # ===== doctor =====
    p_doctor = sub.add_parser(
        "doctor",
        help="环境前置体检（git/openspec/codex/claude/mimo.env/...）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
体检项：git（必备）/ openspec / codex / claude / jq / portable-timeout（PATH 或
~/.local/bin 自举）/ review schema 自举情况 / mimo.env 成本路由 / npc config
可加载性 / docs/principles.md。除 git 外均为 warn 级、不阻塞。

stdout（单行，required 缺失时同一行内嵌 error/message）:
  {"ok": <bool>, "checks": [{"name","status":"ok|missing|warn","detail","required"}, ...],
   "summary": {"ok","warn","missing","missing_required":[...]},
   "error": "dependency_missing"（仅缺失时）, "message": "<str>"（仅缺失时）}

exit code:
  0  无 required 项缺失
  4  required 项缺失（当前仅 git）
""",
    )
    p_doctor.set_defaults(handler=_make_handler("doctor", "run"), _cmd_path="doctor")

    # ===== spec =====
    p_spec = sub.add_parser("spec", help="spec 一致性分析")
    sub_spec = p_spec.add_subparsers(dest="spec_cmd", required=True)
    p_spec_an = sub_spec.add_parser(
        "analyze",
        help="spec↔tasks 漂移/覆盖检查（实现前闸门）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
纯读 openspec/changes/<id>/ 下 proposal.md / specs/*/spec.md / tasks.md 做确定性
一致性检查（不依赖 openspec CLI）。findings.kind：no-tasks(high) /
capability-no-spec(high) / orphan-spec(high) / requirement-maybe-uncovered(medium，
关键词覆盖启发式，可能误报) / tasks-all-done(low，信息性)。

stdout:
  {"ok": <bool>, "change": "<str>", "requirements_count": <int>, "tasks_count": <int>,
   "capabilities": ["<str>", ...], "findings": [{"kind","severity","detail"}, ...]}

exit code:
  0  无 high/medium finding
  1  有 high/medium drift
  2  缺 --change
  3  change 目录不存在 / repo_root 定位失败
""",
    )
    p_spec_an.add_argument("--change", required=True, help="change-id")
    p_spec_an.set_defaults(handler=_make_handler("spec_analyze", "run"), _cmd_path="spec analyze")

    # ===== plan =====
    p_plan = sub.add_parser("plan", help="阶段前置门 + change 脚手架")
    sub_plan = p_plan.add_subparsers(dest="plan_cmd", required=True)
    p_plan_chk = sub_plan.add_parser(
        "check",
        help="阶段前置门：apply 所需 artifact 是否齐全",
        formatter_class=_EPILOG_FMT,
        epilog="""\
调 `openspec status --change <id> --json`，解析 payload.applyRequires 中每个
产物 id 在 artifacts[] 里的 status 是否为 done；绝不裸信 LLM 自报"已就绪"。

stdout:
  {"ok": <bool>, "change": "<str>", "phase": "<str>", "ready": <bool>,
   "apply_requires": ["<str>", ...], "missing": ["<str>", ...]}

exit code:
  0  ready
  1  not ready / openspec 调用失败或输出非法 JSON
  2  缺 --change / --change 疑似参数注入
  3  非 git 仓库
  4  openspec 未安装
""",
    )
    p_plan_chk.add_argument("--change", required=True)
    p_plan_chk.add_argument("--phase", default="implement")
    p_plan_chk.add_argument("--openspec-bin", dest="openspec_bin", default=None)
    p_plan_chk.set_defaults(handler=_make_handler("plan", "cli_check"), _cmd_path="plan check")
    p_plan_new = sub_plan.add_parser(
        "new-change",
        help="脚手架一个 openspec change",
        formatter_class=_EPILOG_FMT,
        epilog="""\
调 `openspec new change <id> [--description ..] [--schema ..]`，成功后扫描
生成目录列出全部文件。change-id 强校验 ^[A-Za-z0-9][A-Za-z0-9._-]*$（挡参数注入
与路径遍历），description/schema 不得以 '-' 开头。

stdout:
  {"ok": true, "change": "<str>", "path": "<change 目录>", "files": ["<相对路径>", ...]}

exit code:
  0  成功
  1  openspec 调用失败（error 带 stderr 尾段）
  2  缺 --change / change-id 或参数非法
  3  非 git 仓库
  4  openspec 未安装
""",
    )
    p_plan_new.add_argument("--change", required=True, help="kebab-case change-id")
    p_plan_new.add_argument("--description", default=None)
    p_plan_new.add_argument("--schema", default=None)
    p_plan_new.add_argument("--openspec-bin", dest="openspec_bin", default=None)
    p_plan_new.set_defaults(handler=_make_handler("plan", "cli_new_change"), _cmd_path="plan new-change")
    p_plan_waves = sub_plan.add_parser(
        "waves",
        help="并行波次候选划分：Kahn 拓扑分层 + 层内文件交集着色（stdin/--input JSON）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
输入 JSON（stdin 或 --input）：{"nodes":[...], "edges":[["A","B"],...]（A 先于 B）,
"files":{"<node>":["<path>",...]}, "tie_break":{"<node>":[tier,scope]}}。
输出是候选划分，语义耦合仍须架构师 sub-agent 裁定。

stdout:
  {"waves": [["<node>", ...], ...], "layers": [...],
   "split_reasons": [{"layer","members","sub_waves","serialized_pairs","shared_files"}, ...],
   "cycle": ["<node>", ...]}

`cycle` 非空表示 DAG 有环，已强制释放所列节点破环。

exit code:
  0  成功
  2  输入不合法（非 JSON / 缺 nodes / 文件不可读）
""",
    )
    p_plan_waves.add_argument("--input", default=None, help="JSON 输入文件（省略则读 stdin）")
    p_plan_waves.set_defaults(handler=_make_handler("waves", "run"), _cmd_path="plan waves")

    # ===== git =====
    p_git = sub.add_parser("git", help="SDD git 卫生（分支/脏树/commit）")
    sub_git = p_git.add_subparsers(dest="git_cmd", required=True)
    p_git_br = sub_git.add_parser(
        "branch-for",
        help="为 change 切到确定性分支 change/<id>",
        formatter_class=_EPILOG_FMT,
        epilog="""\
分支名固定为 change/<change-id>；已存在则 checkout（created=false），否则
create+checkout（created=true）。change-id 只允许 [A-Za-z0-9._-]，不含 ".."、
不以 '-' 开头。

stdout（成功）:
  {"ok": true, "branch": "change/<id>", "created": <bool>}

stdout（失败）:
  {"ok": false, "branch": "...", "created": <bool>, "error": "git_checkout_failed", "stderr": "<str>"}

exit code:
  0  成功
  1  git checkout 失败
  2  缺 --change / change-id 非法
  3  非 git 仓库
""",
    )
    p_git_br.add_argument("--change", required=True)
    p_git_br.set_defaults(handler=_make_handler("git_ops", "cli_branch_for"), _cmd_path="git branch-for")
    p_git_ec = sub_git.add_parser(
        "ensure-clean",
        help="工作树脏则拒绝（exit 1）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
`git status --porcelain -z` 解析（支持含空格/特殊字符路径），空则视为 clean。

stdout:
  {"ok": <bool>, "clean": <bool>, "dirty_files": ["<path>", ...]}

exit code:
  0  clean
  1  脏（仍打印 dirty_files） / git status 本身失败
  3  非 git 仓库
""",
    )
    p_git_ec.set_defaults(handler=_make_handler("git_ops", "cli_ensure_clean"), _cmd_path="git ensure-clean")
    p_git_ci = sub_git.add_parser(
        "commit",
        help="git add -A + commit（消息可派生）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
消息优先级：--message 显式给定 > 由 --change/--phase 派生
"chore(spine): <phase> <change>"（无 phase 则省略）。message 不得含换行、不得以
'-'/'#' 开头。无改动可提交时 committed=false 且视为成功（exit 0）。

stdout（有提交）:
  {"ok": true, "committed": true, "commit": "<hash>", "message": "<str>", "branch": "<str>"}

stdout（无改动）:
  {"ok": true, "committed": false, "reason": "nothing-to-commit"}

exit code:
  0  成功（含"无改动"情况）
  1  git add / git commit 失败
  2  缺消息且无 --change/--phase 可派生 / message 非法
  3  非 git 仓库
""",
    )
    p_git_ci.add_argument("--message", default=None)
    p_git_ci.add_argument("--change", default=None)
    p_git_ci.add_argument("--phase", default=None)
    p_git_ci.set_defaults(handler=_make_handler("git_ops", "cli_commit"), _cmd_path="git commit")

    # ===== deliver / pr =====（对外动作；skill 人闸）
    p_deliver = sub.add_parser(
        "deliver",
        help="push 当前分支到远程（对外动作）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
对外动作：不自作主张决定要不要推，仅在被显式调用时执行一次 `git push [-u] <remote>
<branch>`。--branch 省略则取当前分支（游离 HEAD 报错要求显式传）。

stdout（成功）:
  {"ok": true, "remote": "<str>", "branch": "<str>", "pushed": true}

stdout（失败）:
  {"ok": false, "remote": "...", "branch": "...", "pushed": false,
   "error": "push_failed", "stderr_tail": "<脱敏后末尾 40 行>"}

exit code:
  0  成功
  1  push 失败
  2  未给 --branch 且无法确定当前分支
  3  非 git 仓库
  4  未装 git
""",
    )
    p_deliver.add_argument("--remote", default="origin")
    p_deliver.add_argument("--branch", default=None)
    p_deliver.add_argument("--no-set-upstream", dest="set_upstream", action="store_false", default=True)
    p_deliver.set_defaults(handler=_make_handler("deliver", "cli_deliver"), _cmd_path="deliver")
    p_pr = sub.add_parser("pr", help="PR 操作（对外动作）")
    sub_pr = p_pr.add_subparsers(dest="pr_cmd", required=True)
    p_pr_open = sub_pr.add_parser(
        "open",
        help="gh pr create（body 可从 run-summary 派生）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
对外动作：调 `gh pr create [--title][--body][--base][--draft]` 并从 stdout 解析
PR url。--body-file 优先于 --body（如传 run-summary.md 路径）。

stdout（成功，能解析到 url）:
  {"ok": true, "pr_url": "https://.../pull/<n>", "title": "<str>|null"}

stdout（成功但未解析到 url，附 raw_stdout_tail 供人工捞取）:
  {"ok": true, "pr_url": null, "title": "...", "raw_stdout_tail": "<str>"}

stdout（失败）:
  {"ok": false, "title": "...", "error": "gh_pr_create_failed", "stderr_tail": "<脱敏后末尾 40 行>"}

exit code:
  0  成功
  1  gh pr create 失败
  2  --body-file 读取失败
  3  非 git 仓库
  4  未装 gh
""",
    )
    p_pr_open.add_argument("--title", default=None)
    p_pr_open.add_argument("--body", default=None)
    p_pr_open.add_argument("--body-file", dest="body_file", default=None)
    p_pr_open.add_argument("--base", default=None)
    p_pr_open.add_argument("--draft", action="store_true")
    p_pr_open.set_defaults(handler=_make_handler("deliver", "cli_pr_open"), _cmd_path="pr open")

    # ===== status / cost / clean =====
    p_status = sub.add_parser(
        "status",
        help="当前 run 进度一览（只读）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
纯读 STATE_JSON 派生快照，绝不写 state。

stdout:
  {"ok": true, "run_ts": "<str>", "top_status": "<str>|null", "total": <int>,
   "by_status": {"<status>": <int>, ...},
   "current": {"seq","change_id","status"}|null（第一个非终态 change）,
   "changes": [{"seq","change_id","status","rounds"}, ...]}

exit code:
  0  成功
  3  未定位到 active run / STATE_JSON 缺失
""",
    )
    p_status.add_argument(
        "--brief",
        action="store_true",
        help="重定向契约（v1.5）：收掉 changes 全列表，带出 pending_decisions / 未消费 notes / next_action",
    )
    p_status.set_defaults(handler=_make_handler("status", "run"), _cmd_path="status")
    p_cost = sub.add_parser(
        "cost",
        help="按后端拆 token 成本（Claude vs MiMo ...）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
只读 telemetry 派生指标（events.ndjson），按后端身份（review.round 用 engine
字段；其它含 tokens 的 record 统一归 "coder"，telemetry 暂无法区分 MiMo/Claude
故标注 method=heuristic）分桶累加。无数据时 total 全 0，仍视为成功。

stdout:
  {"ok": true, "since": "<str>|null",
   "by_bucket": {"<bucket>": {"events","est_input_tokens","est_output_tokens","duration_ms"}, ...},
   "total": {同上字段}, "method": "heuristic"}

exit code:
  0  成功
  2  --since 格式不合法
""",
    )
    p_cost.add_argument("--since", default=None, help="如 7d/24h/30m/ISO")
    p_cost.set_defaults(handler=_make_handler("cost", "run"), _cmd_path="cost")
    p_clean = sub.add_parser(
        "clean",
        help="清理陈旧/已中止 run 目录（默认 dry-run）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
可清理判定（三条件全满足才删）：非 active run；顶层 status in
{completed,completed-with-issues,aborted} 或 state 缺失/不可读（孤儿）；最后修改
时间早于 --keep-days 之前。in-progress / active run 绝不删。

stdout（dry-run，默认）:
  {"ok": true, "dry_run": true, "removable": ["<run_ts>", ...],
   "kept_count": <int>, "freed_estimate": <int>}

stdout（--yes 真删）:
  {"ok": true, "dry_run": false, "removed": ["<path>", ...], "kept_count": <int>}

exit code:
  0  成功
  2  --keep-days < 1
  3  非 git 仓库 / task_log_dir 定位失败
""",
    )
    p_clean.add_argument("--yes", action="store_true", help="真删（默认 dry-run）")
    p_clean.add_argument("--keep-days", dest="keep_days", type=int, default=14)
    p_clean.set_defaults(handler=_make_handler("clean", "run"), _cmd_path="clean")

    # ===== notify =====
    p_notify = sub.add_parser(
        "notify",
        help="best-effort webhook 推送（永不打断 run，总是 exit 0）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
URL 解析顺序：--url > $NPC_WEBHOOK > $NPC_V3_WEBHOOK；都为空则静默 no-op（未启用，
不算失败）。--format 决定 payload 形状：raw={"event","text",<kv..>} /
slack={"text"} / feishu={"msg_type":"text","content":{"text"}}。webhook 超时/
拒连/4xx 只写 stderr 警告，不影响 exit code。

stdout:
  {"ok": true, "event": "<str>", "url_set": <bool>, "delivered": <bool>}

exit code:
  0  总是 0
""",
    )
    p_notify.add_argument("--event", required=True, help="事件类型，如 implement-done / wave-done / run-finalized")
    p_notify.add_argument("--url", default="", help="webhook URL；缺省依次读 $NPC_WEBHOOK / $NPC_V3_WEBHOOK")
    p_notify.add_argument("--format", choices=["raw", "slack", "feishu"], default="raw")
    p_notify.add_argument("--kv", action="append", default=[], help="key=value 结构化字段，可重复")
    p_notify.add_argument("--text", default="", help="人读摘要；省略则由 event+kv 派生")
    p_notify.add_argument("--timeout", type=float, default=5.0)
    p_notify.set_defaults(handler=_make_handler("notify", "run"), _cmd_path="notify")

    # ===== task / watch =====
    p_task = sub.add_parser("task", help="后台任务观测契约（start/update/heartbeat/finish）")
    sub_task = p_task.add_subparsers(dest="task_cmd", required=True)

    def add_task_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--phase", default=None, help="当前 phase，如 implement/test/review")
        p.add_argument("--message", default=None, help="短状态摘要")
        p.add_argument("--progress-current", dest="progress_current", type=int, default=None)
        p.add_argument("--progress-total", dest="progress_total", type=int, default=None)
        p.add_argument("--progress-unit", dest="progress_unit", default=None)
        p.add_argument("--log", default=None, help="任务日志指针")
        p.add_argument("--summary", default=None, help="任务 summary 指针")
        p.add_argument("--transcript", default=None, help="Claude transcript/subagent 指针")

    p_task_start = sub_task.add_parser(
        "start",
        help="登记一个可被 watch 观测的任务",
        formatter_class=_EPILOG_FMT,
        epilog="""\
写 <run_dir>/tasks/<id>.json（权威快照）+ 追加 task.started 事件到
<id>.events.jsonl。task_id 须匹配 [A-Za-z0-9][A-Za-z0-9_.-]{0,127}（防路径穿越）。

stdout:
  {"ok": true, "task_id": "<str>", "status": "running",
   "task_json": "<path>", "events": "<path>"}

exit code:
  0  成功
  1  同 id 任务已存在（未传 --replace）
  2  task id 非法
  3  未定位到当前 run
""",
    )
    p_task_start.add_argument("--id", required=True, help="任务 id（文件名安全）")
    p_task_start.add_argument("--description", required=True)
    p_task_start.add_argument("--source", default="manual", help="npc/claude-subagent/bash/manual 等")
    p_task_start.add_argument("--session-id", dest="session_id", default=None)
    p_task_start.add_argument("--stale-seconds", dest="stale_seconds", type=int, default=900)
    p_task_start.add_argument("--replace", action="store_true", help="覆盖同 id 旧任务")
    add_task_common(p_task_start)
    p_task_start.set_defaults(handler=_make_handler("task", "start"), _cmd_path="task start")

    p_task_update = sub_task.add_parser(
        "update",
        help="更新任务 phase/message/progress",
        formatter_class=_EPILOG_FMT,
        epilog="""\
更新既有任务快照的传入字段，追加 task.updated 事件（不刷新心跳）。

stdout:
  {"ok": true, "kind": "task.updated", "task_id": "<str>", "status": "<str>",
   "task_json": "<path>", "events": "<path>"}

exit code:
  0  成功
  1  任务不存在 / 更新失败
  3  未定位到当前 run
""",
    )
    p_task_update.add_argument("--id", required=True)
    p_task_update.add_argument("--status", choices=["running", "waiting"], default=None)
    add_task_common(p_task_update)
    p_task_update.set_defaults(handler=_make_handler("task", "update"), _cmd_path="task update")

    p_task_hb = sub_task.add_parser(
        "heartbeat",
        help="刷新任务心跳",
        formatter_class=_EPILOG_FMT,
        epilog="""\
刷新 last_heartbeat_at 并追加 task.heartbeat 事件；watch 用
last_heartbeat_at + stale_seconds 派生 observed_status=stale。

stdout:
  {"ok": true, "kind": "task.heartbeat", "task_id": "<str>", "status": "<str>",
   "last_heartbeat_at": "<iso>", "task_json": "<path>", "events": "<path>"}

exit code:
  0  成功
  1  任务不存在 / 更新失败
  3  未定位到当前 run
""",
    )
    p_task_hb.add_argument("--id", required=True)
    p_task_hb.add_argument("--status", choices=["running", "waiting"], default=None)
    add_task_common(p_task_hb)
    p_task_hb.set_defaults(handler=_make_handler("task", "heartbeat"), _cmd_path="task heartbeat")

    p_task_finish = sub_task.add_parser(
        "finish",
        help="标记任务为终态",
        formatter_class=_EPILOG_FMT,
        epilog="""\
写终态 status/phase/message/summary/result，追加 task.finished 事件。

stdout:
  {"ok": true, "task_id": "<str>", "status": "done|failed|cancelled",
   "task_json": "<path>", "events": "<path>"}

exit code:
  0  成功
  1  任务不存在 / 更新失败
  3  未定位到当前 run
""",
    )
    p_task_finish.add_argument("--id", required=True)
    p_task_finish.add_argument("--status", choices=["done", "failed", "cancelled"], default="done")
    p_task_finish.add_argument("--phase", default=None)
    p_task_finish.add_argument("--message", default=None)
    p_task_finish.add_argument("--summary", default=None)
    p_task_finish.add_argument("--result", default=None)
    p_task_finish.set_defaults(handler=_make_handler("task", "finish"), _cmd_path="task finish")

    p_watch = sub.add_parser(
        "watch",
        help="只读观测 active run 与 watchable tasks",
        formatter_class=_EPILOG_FMT,
        epilog="""\
默认观测当前 cwd 所属项目的 active run；--all 扫描 ~/task_log/*/active.json 指向
的全部 active run；--project PATH 观测指定 worktree。无 --once 时循环刷新终端
TUI 视图（无结构化 stdout）；--once 输出一次快照后退出，适合脚本/测试。

stdout（--once）:
  {"ok": true, "schema_version": 1, "generated_at": "<iso>", "scope": "project|all|...",
   "runs": [{"proj_key","run_ts","state":{...},
             "tasks": [{"task_id","observed_status","heartbeat_age_seconds", ...}, ...]}, ...]}

exit code:
  0  成功
  3  观测失败（watch_failed，如定位不到任何 run）
""",
    )
    p_watch.add_argument("--all", action="store_true", help="扫描所有 task_log 项目的 active run")
    p_watch.add_argument("--project", default=None, help="指定项目/worktree 路径")
    p_watch.add_argument("--once", action="store_true", help="输出一次 JSON 快照后退出")
    p_watch.add_argument("--interval", type=float, default=2.0, help="TUI 刷新间隔秒数")
    p_watch.add_argument("--stale-seconds", dest="stale_seconds", type=int, default=None)
    p_watch.set_defaults(handler=_make_handler("watch", "run"), _cmd_path="watch")

    # ===== agent =====
    p_agent = sub.add_parser(
        "agent", help="Sub-agent prompt 渲染与 spawn 引导语生成（v1.0+）"
    )
    sub_agent = p_agent.add_subparsers(dest="agent_cmd", required=True)

    p_agent_prompt = sub_agent.add_parser("prompt", help="Prompt 文件渲染")
    sub_agent_prompt = p_agent_prompt.add_subparsers(dest="prompt_cmd", required=True)

    p_agent_prompt_render = sub_agent_prompt.add_parser(
        "render",
        help="渲染 implement/fix prompt 到 disk（主 session 不接触模板内容）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
从 STATE_JSON 自包含 resolve seq/base/categories_seen/blocking_trend/
implement_commit。implement：渲染 §A 模板（Runtime Variables + 必读输入 + 双产物
契约 + RESULT schema）。fix：从 --review-json（默认 round-(N-1).review.json）抽
blocking findings，渲染 §B 模板（root-cause 扫描 + 真实回归 + Self-Check）。

stdout（implement）:
  {"ok": true, "phase": "implement", "seq": <int>, "change_id": "<str>",
   "output": "<path>", "bytes": <int>, "template_version": "<str>"}

stdout（fix）: implement 字段 + "round" / "blocking_count" / "review_json" / "implement_commit"

exit code:
  0  成功
  1  state 不一致 / review.json schema 不合法
  2  用法错（缺 --round、implement 阶段传 --round、fix 缺 --implement-commit）
  3  环境错（缺 state / review.json）
""",
    )
    p_agent_prompt_render.add_argument(
        "--phase", required=True, choices=["implement", "fix"]
    )
    p_agent_prompt_render.add_argument("--change-id", required=True)
    p_agent_prompt_render.add_argument(
        "--seq", type=int, default=None, help="可选；默认按 change_id 在 state 中查"
    )
    p_agent_prompt_render.add_argument(
        "--round", dest="round_n", type=int, default=None, help="fix 阶段必传"
    )
    p_agent_prompt_render.add_argument(
        "--output",
        default=None,
        help="可选；默认 $BASE/implement.prompt.md 或 $BASE/round-N.fix.prompt.md",
    )
    p_agent_prompt_render.add_argument(
        "--review-json",
        default=None,
        help="fix 可选；默认 $BASE/round-(N-1).review.json",
    )
    p_agent_prompt_render.add_argument(
        "--implement-commit",
        default=None,
        help="fix 可选；默认从 state 取 progress[].implement_commit",
    )
    p_agent_prompt_render.set_defaults(
        handler=_make_handler("agent", "prompt_render"),
        _cmd_path="agent prompt render",
    )

    p_agent_spawn = sub_agent.add_parser(
        "spawn-prompt",
        help="生成给 Claude Agent 工具 prompt 字段用的薄引导语",
        formatter_class=_EPILOG_FMT,
        epilog="""\
不校验 STATE_JSON 中 phase 的状态（该职责属于 phase enter 等命令）；仅负责
"取 prompt 文件路径 + 拼引导语"的纯字符串操作。--prompt-file 未传时按
phase/round 推算 $BASE/...prompt.md，要求该文件已存在（先跑 prompt render）。

stdout:
  {"ok": true, "phase": "<str>", "seq": <int>, "change_id": "<str>",
   "prompt": "<引导语全文>", "prompt_file": "<abs-path>",
   "has_extension": <bool>, "bytes": <int>}

exit code:
  0  成功
  2  用法错（缺 --round / --extension 与 --extension-inline 同时给）
  3  prompt_file 或 extension 文件不存在
""",
    )
    p_agent_spawn.add_argument("--phase", required=True, choices=["implement", "fix"])
    p_agent_spawn.add_argument("--change-id", required=True)
    p_agent_spawn.add_argument("--seq", type=int, default=None)
    p_agent_spawn.add_argument("--round", dest="round_n", type=int, default=None)
    p_agent_spawn.add_argument(
        "--prompt-file",
        default=None,
        help="可选；默认按 phase/round 算出 $BASE/...prompt.md",
    )
    p_agent_spawn.add_argument(
        "--extension",
        default=None,
        help="可选；额外约束文件，内容追加到引导语",
    )
    p_agent_spawn.add_argument(
        "--extension-inline",
        default=None,
        help="可选；直接传 extension 文本（与 --extension 互斥）",
    )
    p_agent_spawn.set_defaults(
        handler=_make_handler("agent", "spawn_prompt"),
        _cmd_path="agent spawn-prompt",
    )

    # agent timeout-budget / record-timeout（v1.1 起，渐进退避）
    p_agent_tb = sub_agent.add_parser(
        "timeout-budget",
        help="查询当前 phase 应用的 Agent 调用 wall-clock timeout（不副作用）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
纯查询，不写 state。timeout_sec = min(base * mult^retries, max_sec)，retries 取
phases.<phase>.timeout_retries（由 record-timeout 递增）。exhausted 判定
retries >= 5（对应两次撞满 3600s 上限）。

stdout:
  {"ok": true, "seq": <int>, "phase": "<str>", "timeout_sec": <int>, "retries": <int>,
   "exhausted": <bool>, "max_reached": <bool>, "base_sec": <int>,
   "multiplier": <float>, "max_sec": <int>, "exhausted_at_retries": 5}

exit code:
  0  成功
  1  seq 超出 progress 数组长度
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_agent_tb.add_argument("--seq", type=int, required=True)
    p_agent_tb.add_argument(
        "--phase",
        required=True,
        help="phase 名（implement / fix-rN）；查询的是该 phase 的 timeout_retries",
    )
    p_agent_tb.add_argument("--base", type=int, default=None, help="覆盖 base 秒数（默认 1800）")
    p_agent_tb.add_argument("--mult", type=float, default=None, help="覆盖退避倍率（默认 1.2）")
    p_agent_tb.add_argument(
        "--max-sec", dest="max_sec", type=int, default=None, help="覆盖 timeout 上限秒数（默认 3600）"
    )
    p_agent_tb.set_defaults(
        handler=_make_handler("agent", "timeout_budget"), _cmd_path="agent timeout-budget"
    )

    p_agent_rt = sub_agent.add_parser(
        "record-timeout",
        help="记录一次 Agent 调用超时；递增 retries 并返回下次的预算",
        formatter_class=_EPILOG_FMT,
        epilog="""\
把 phases.<phase>.timeout_retries += 1 并写 timeout_last_ts，返回按新 retries
计算的下一次 timeout 预算（公式同 timeout-budget）。

stdout:
  {"ok": true, "seq": <int>, "phase": "<str>", "retries": <int>,
   "next_timeout_sec": <int>, "exhausted": <bool>, "max_reached": <bool>}

exit code:
  0  成功
  1  seq 超出 progress 数组长度
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_agent_rt.add_argument("--seq", type=int, required=True)
    p_agent_rt.add_argument("--phase", required=True)
    p_agent_rt.add_argument("--base", type=int, default=None)
    p_agent_rt.add_argument("--mult", type=float, default=None)
    p_agent_rt.add_argument("--max-sec", dest="max_sec", type=int, default=None)
    p_agent_rt.set_defaults(
        handler=_make_handler("agent", "record_timeout"), _cmd_path="agent record-timeout"
    )

    # ===== auto-decide =====
    p_ad = sub.add_parser(
        "auto-decide",
        help="--auto 模式下的主 session 决策器（输入 trigger，输出 action）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
纯 state 决策（不读 git / 不调 sub-agent），同一 state+trigger 总返回同一
action。trigger 合法集：stale / max-rounds / agent-timeout-exhausted /
codex-failed / implementer-failed / fixer-failed / summary-missing /
commit-not-found。action：continue-retry / skip / force-archive。--apply 直接把
set_status / reason / auto_retry_<trigger> 写回 state（否则只建议不落盘）。

stdout:
  {"ok": true, "trigger": "<str>", "action": "continue-retry|skip|force-archive",
   "reason": "<str>", "set_status": "<str>|null",
   "increment_retry_key": "<str>|null"（continue-retry 时才有）,
   "seq": <int>, "change_id": "<str>", "blocking_trend": [...],
   "categories_seen": [...], "applied": <bool>}

exit code:
  0  成功
  1  seq 超出 progress 数组长度
  2  --trigger 不在合法集
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_ad.add_argument("--seq", type=int, required=True)
    p_ad.add_argument(
        "--trigger",
        required=True,
        help="触发场景：stale / max-rounds / agent-timeout-exhausted / codex-failed / implementer-failed / fixer-failed / summary-missing / commit-not-found",
    )
    p_ad.add_argument(
        "--apply",
        action="store_true",
        help="直接把建议的 set_status / set_reason / auto_retry_<trigger> 写回 state",
    )
    p_ad.set_defaults(handler=_make_handler("auto_decide", "cli"), _cmd_path="auto-decide")

    # ===== summary =====
    p_sum = sub.add_parser("summary", help="run-summary.md 渲染")
    sub_sum = p_sum.add_subparsers(dest="summary_cmd", required=True)
    p_sum_render = sub_sum.add_parser(
        "render",
        help="从 STATE_JSON + RUN_EVENTS 派生",
        formatter_class=_EPILOG_FMT,
        epilog="""\
渲染 $RUN_DIR/run-summary.md（run 收尾用，供 `npc pr open --body-file` 复用）。

stdout:
  {"ok": true, "output": "<path>", "duration_ms": <int>, "archived": <int>,
   "failed": <int>, "skipped": <int>}

exit code:
  0  成功
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_sum_render.set_defaults(
        handler=_make_handler("summary", "render"), _cmd_path="summary render"
    )

    # ===== index =====
    p_idx = sub.add_parser("index", help="跨 run 索引")
    sub_idx = p_idx.add_subparsers(dest="index_cmd", required=True)
    p_idx_app = sub_idx.add_parser(
        "append",
        help="追加一行到 index.jsonl",
        formatter_class=_EPILOG_FMT,
        epilog="""\
把本 run 概要（含 top_status / 各 change 结果）追加到 $NPC_INDEX_FILE，供跨 run
学习/统计使用。

stdout:
  {"ok": true, "index_file": "<path>", "appended_line_bytes": <int>}

exit code:
  0  成功
  3  环境错（STATE_JSON 不存在）
""",
    )
    p_idx_app.set_defaults(
        handler=_make_handler("summary", "index_append"), _cmd_path="index append"
    )

    # ===== telemetry =====
    p_tel = sub.add_parser(
        "telemetry",
        help="跨 run 指标流（~/task_log/_telemetry/events.ndjson）与聚合",
    )
    sub_tel = p_tel.add_subparsers(dest="telemetry_cmd", required=True)

    p_tel_emit = sub_tel.add_parser(
        "emit",
        help="手动追加一条指标事件（排错用；正常由 pipeline/events/agent 自动 emit）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
追加一条 record 到 ~/task_log/_telemetry/events.ndjson（schema_version=1）。
--proj-key 缺省按 cwd -> repo_root 推；--extra 的 JSON 对象合并进 record（不覆盖
已有同名字段）。

stdout:
  {"ok": true, "kind": "<str>", "path": "<events.ndjson 绝对路径>"}

exit code:
  0  成功
  2  --extra 非法 JSON
""",
    )
    p_tel_emit.add_argument("--kind", required=True, help="事件类型，如 phase.exit / review.round")
    p_tel_emit.add_argument("--seq", type=int, default=None)
    p_tel_emit.add_argument("--change-id", default=None)
    p_tel_emit.add_argument("--phase", default=None)
    p_tel_emit.add_argument("--status", default=None, choices=["done", "failed", None])
    p_tel_emit.add_argument("--duration-ms", dest="duration_ms", type=int, default=None)
    p_tel_emit.add_argument("--proj-key", dest="proj_key", default=None)
    p_tel_emit.add_argument("--run-ts", dest="run_ts", default=None)
    p_tel_emit.add_argument("--extra", default=None, help="合并到 record 的 JSON 对象")
    p_tel_emit.set_defaults(
        handler=_make_handler("telemetry", "cli_emit"), _cmd_path="telemetry emit"
    )

    p_tel_tail = sub_tel.add_parser(
        "tail",
        help="看最近 N 条原始 telemetry 事件",
        formatter_class=_EPILOG_FMT,
        epilog="""\
排错用（主 session 正常不应读 events.ndjson 原文）。

stdout:
  {"ok": true, "count": <int>, "total": <int>（过滤后总数）, "events": [<record>, ...]}

exit code:
  0  成功
""",
    )
    p_tel_tail.add_argument("--kind", default=None, help="仅保留某 kind")
    p_tel_tail.add_argument("--last", type=int, default=20)
    p_tel_tail.set_defaults(
        handler=_make_handler("telemetry", "cli_tail"), _cmd_path="telemetry tail"
    )

    p_tel_agg = sub_tel.add_parser(
        "agg",
        help="按维度聚合并写 aggregates/by-<by>.json",
        formatter_class=_EPILOG_FMT,
        epilog="""\
--by 省略时 phase/change/week 三个维度全跑；--no-write 只输出 stdout，不写
aggregates/。每个维度聚合出：
  {count, done, failed, failure_rate, duration_ms{p50,p95,max,sum},
   est_input_tokens_sum, est_output_tokens_sum, retry_count_sum,
   blocking_total, review_rounds, kinds, reasons, verdicts}

stdout:
  {"ok": true, "since": "<str>|null", "by": {"<dim>": {聚合对象},
   "<dim>_path": "<path>"（未传 --no-write 时才有）, ...}, "events_considered": <int>}

exit code:
  0  成功
  2  --since 格式不合法
""",
    )
    p_tel_agg.add_argument(
        "--by", choices=["phase", "change", "week"], default=None,
        help="省略则三个维度全跑",
    )
    p_tel_agg.add_argument(
        "--since", default=None, help="只统计该窗口内的事件，如 7d / 24h / 30m / ISO 8601",
    )
    p_tel_agg.add_argument(
        "--no-write", action="store_true", help="只在 stdout 输出，不写 aggregates/",
    )
    p_tel_agg.set_defaults(
        handler=_make_handler("telemetry", "cli_agg"), _cmd_path="telemetry agg"
    )

    p_tel_hot = sub_tel.add_parser(
        "hotspots",
        help="按 (failure_rate × p50_duration × retries) 排序，给出最值得优化的前 N 个 phase",
        formatter_class=_EPILOG_FMT,
        epilog="""\
score = (failure_rate + 0.1) * (p50_duration_ms + 1) * (1 + retry_count_sum)；
+0.1 / +1 常数项防止全成功 phase 永远 score=0 压住高 retry 项。是第二阶段
meta-agent 的唯一输入之一（不读 events.ndjson 原文）。

stdout:
  {"ok": true, "since": "<str>|null", "top": <int>, "events_considered": <int>,
   "hotspots": [{"phase","score","count","failure_rate","p50_duration_ms",
                 "p95_duration_ms","retry_count_sum","top_reasons","top_verdicts"}, ...]}

exit code:
  0  成功
  2  --since 格式不合法
""",
    )
    p_tel_hot.add_argument("--top", type=int, default=5)
    p_tel_hot.add_argument("--since", default=None)
    p_tel_hot.set_defaults(
        handler=_make_handler("telemetry", "cli_hotspots"), _cmd_path="telemetry hotspots"
    )

    p_tel_est = sub_tel.add_parser(
        "estimate-tokens",
        help="单文件 token 估算（bytes/4）",
        formatter_class=_EPILOG_FMT,
        epilog="""\
估算法固定 method=bytes_div_4（与 telemetry token 字段口径一致）。

stdout:
  {"ok": true, "file": "<path>", "bytes": <int>, "est_tokens": <int>, "method": "bytes_div_4"}

exit code:
  0  成功
  3  文件不存在
""",
    )
    p_tel_est.add_argument("file")
    p_tel_est.set_defaults(
        handler=_make_handler("telemetry", "cli_estimate_tokens"),
        _cmd_path="telemetry estimate-tokens",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error("未识别的命令")
    handler(args)


if __name__ == "__main__":
    main()
