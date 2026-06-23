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
    p_init = sub.add_parser("init", help="初始化运行环境，输出路径与 session 信息")
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
    p_resume_detect = sub_resume.add_parser("detect", help="检测续跑断点")
    p_resume_detect.set_defaults(
        handler=_make_handler("resume", "detect"), _cmd_path="resume detect"
    )

    # ===== state =====
    p_state = sub.add_parser("state", help="STATE_JSON 读写")
    sub_state = p_state.add_subparsers(dest="state_cmd", required=True)

    p_state_init = sub_state.add_parser("init-run", help="首次创建 STATE_JSON")
    p_state_init.add_argument(
        "--plan-order", required=True, help='JSON 数组字符串，如 \'["a","b"]\''
    )
    p_state_init.set_defaults(
        handler=_make_handler("state", "init_run"), _cmd_path="state init-run"
    )

    p_state_get = sub_state.add_parser("get", help="按 jq 路径取值")
    p_state_get.add_argument("jq_path", help="jq 路径表达式")
    p_state_get.set_defaults(handler=_make_handler("state", "get"), _cmd_path="state get")

    p_state_add = sub_state.add_parser("add-change", help="向 progress 追加 change 条目")
    p_state_add.add_argument("seq", type=int)
    p_state_add.add_argument("change_id")
    p_state_add.add_argument("--base", default=None, help="覆盖 base 路径")
    p_state_add.set_defaults(
        handler=_make_handler("state", "add_change"), _cmd_path="state add-change"
    )

    p_state_set = sub_state.add_parser("set-progress", help="更新 progress 条目字段")
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

    p_state_fin = sub_state.add_parser("finalize", help="收尾：判定顶层 status")
    p_state_fin.set_defaults(
        handler=_make_handler("state", "finalize"), _cmd_path="state finalize"
    )

    p_state_rep = sub_state.add_parser(
        "repair",
        help="自愈漂移：把 commit 已不在 git 的 progress 项重置为 pending（旧 base 进 .repaired/ 留存）",
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

    p_phase_enter = sub_phase.add_parser("enter", help="进入 phase")
    p_phase_enter.add_argument("seq", type=int)
    p_phase_enter.add_argument("phase")
    p_phase_enter.set_defaults(
        handler=_make_handler("events", "phase_enter"), _cmd_path="phase enter"
    )

    p_phase_exit = sub_phase.add_parser("exit", help="退出 phase")
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

    p_review_parse = sub_review.add_parser("parse", help="解析 review.json 派生指标")
    p_review_parse.add_argument("review_json", help="review.json 文件路径")
    p_review_parse.set_defaults(
        handler=_make_handler("review", "parse"), _cmd_path="review parse"
    )

    p_review_trend = sub_review.add_parser("update-trend", help="更新 blocking_trend 等")
    p_review_trend.add_argument("seq", type=int)
    p_review_trend.add_argument("--metrics", required=True, help="review parse 的 JSON 输出")
    p_review_trend.set_defaults(
        handler=_make_handler("trend", "update_trend"), _cmd_path="review update-trend"
    )

    p_review_stale = sub_review.add_parser("check-stale", help="检查 stale 判定")
    p_review_stale.add_argument("seq", type=int)
    p_review_stale.set_defaults(
        handler=_make_handler("trend", "check_stale"), _cmd_path="review check-stale"
    )

    p_review_run = sub_review.add_parser(
        "run",
        help="跑完整一轮 review（focus + codex exec + parse + update-trend + check-stale）",
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
    p_focus_render = sub_focus.add_parser("render", help="渲染 focus 文本到文件")
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
    p_fixer_find = sub_fixer.add_parser("findings", help="抽 in_scope blocking findings")
    p_fixer_find.add_argument("--review", required=True, help="review.json 路径")
    p_fixer_find.add_argument("--output-fragment", required=True, help="输出 markdown 路径")
    p_fixer_find.set_defaults(
        handler=_make_handler("fixer", "findings"), _cmd_path="fixer findings"
    )

    # ===== archive =====
    p_archive = sub.add_parser("archive", help="Archive 前校验与全流程")
    sub_archive = p_archive.add_subparsers(dest="archive_cmd", required=True)
    p_archive_pre = sub_archive.add_parser("precheck", help="commit chain 一致性")
    p_archive_pre.add_argument("seq", type=int)
    p_archive_pre.set_defaults(
        handler=_make_handler("git_chain", "precheck"), _cmd_path="archive precheck"
    )

    p_archive_run = sub_archive.add_parser(
        "run",
        help="archive 全流程：precheck → openspec validate → openspec archive → git commit → 状态装订",
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
    )
    p_impl_run.add_argument("--seq", type=int, required=True)
    p_impl_run.add_argument("--change-id", default=None, help="可选；与 state 中的 seq 一致性校验")
    p_impl_run.add_argument(
        "--backend",
        choices=["claude", "mimo", "codex"],
        default=None,
        help="覆盖 coder 后端（默认从 [coder].backend 读，或 mimo.env 存在时自动 mimo）",
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
    )
    p_fix_run.add_argument("--seq", type=int, required=True)
    p_fix_run.add_argument("--round", dest="round_n", type=int, required=True)
    p_fix_run.add_argument("--change-id", default=None)
    p_fix_run.add_argument(
        "--backend", choices=["claude", "mimo", "codex"], default=None,
        help="覆盖 coder 后端",
    )
    p_fix_run.add_argument("--timeout", type=int, default=None)
    p_fix_run.add_argument("--config", default=None)
    p_fix_run.set_defaults(
        handler=_make_handler("coder", "cli_fix_run"), _cmd_path="fix run"
    )

    # ===== verify =====
    p_verify = sub.add_parser("verify", help="质量门 + 路由不变量校验")
    sub_verify = p_verify.add_subparsers(dest="verify_cmd", required=True)
    p_verify_tests = sub_verify.add_parser(
        "tests", help="按 repo 清单探测并真实复跑测试（不裸信自报）"
    )
    p_verify_tests.add_argument("--config", default=None, help="显式 TOML 配置路径")
    p_verify_tests.set_defaults(
        handler=_make_handler("verify", "run_tests"), _cmd_path="verify tests"
    )
    p_verify_routing = sub_verify.add_parser(
        "routing", help="校验 coder/review 路由不变量（生成⊥验证 + MiMo 只许执行）"
    )
    p_verify_routing.add_argument("--config", default=None, help="显式 TOML 配置路径")
    p_verify_routing.set_defaults(
        handler=_make_handler("verify", "run_routing"), _cmd_path="verify routing"
    )

    # ===== doctor =====
    p_doctor = sub.add_parser("doctor", help="环境前置体检（git/openspec/codex/claude/mimo.env/...）")
    p_doctor.set_defaults(handler=_make_handler("doctor", "run"), _cmd_path="doctor")

    # ===== spec =====
    p_spec = sub.add_parser("spec", help="spec 一致性分析")
    sub_spec = p_spec.add_subparsers(dest="spec_cmd", required=True)
    p_spec_an = sub_spec.add_parser("analyze", help="spec↔tasks 漂移/覆盖检查（实现前闸门）")
    p_spec_an.add_argument("--change", required=True, help="change-id")
    p_spec_an.set_defaults(handler=_make_handler("spec_analyze", "run"), _cmd_path="spec analyze")

    # ===== plan =====
    p_plan = sub.add_parser("plan", help="阶段前置门 + change 脚手架")
    sub_plan = p_plan.add_subparsers(dest="plan_cmd", required=True)
    p_plan_chk = sub_plan.add_parser("check", help="阶段前置门：apply 所需 artifact 是否齐全")
    p_plan_chk.add_argument("--change", required=True)
    p_plan_chk.add_argument("--phase", default="implement")
    p_plan_chk.add_argument("--openspec-bin", dest="openspec_bin", default=None)
    p_plan_chk.set_defaults(handler=_make_handler("plan", "cli_check"), _cmd_path="plan check")
    p_plan_new = sub_plan.add_parser("new-change", help="脚手架一个 openspec change")
    p_plan_new.add_argument("--change", required=True, help="kebab-case change-id")
    p_plan_new.add_argument("--description", default=None)
    p_plan_new.add_argument("--schema", default=None)
    p_plan_new.add_argument("--openspec-bin", dest="openspec_bin", default=None)
    p_plan_new.set_defaults(handler=_make_handler("plan", "cli_new_change"), _cmd_path="plan new-change")

    # ===== git =====
    p_git = sub.add_parser("git", help="SDD git 卫生（分支/脏树/commit）")
    sub_git = p_git.add_subparsers(dest="git_cmd", required=True)
    p_git_br = sub_git.add_parser("branch-for", help="为 change 切到确定性分支 change/<id>")
    p_git_br.add_argument("--change", required=True)
    p_git_br.set_defaults(handler=_make_handler("git_ops", "cli_branch_for"), _cmd_path="git branch-for")
    p_git_ec = sub_git.add_parser("ensure-clean", help="工作树脏则拒绝（exit 1）")
    p_git_ec.set_defaults(handler=_make_handler("git_ops", "cli_ensure_clean"), _cmd_path="git ensure-clean")
    p_git_ci = sub_git.add_parser("commit", help="git add -A + commit（消息可派生）")
    p_git_ci.add_argument("--message", default=None)
    p_git_ci.add_argument("--change", default=None)
    p_git_ci.add_argument("--phase", default=None)
    p_git_ci.set_defaults(handler=_make_handler("git_ops", "cli_commit"), _cmd_path="git commit")

    # ===== deliver / pr =====（对外动作；skill 人闸）
    p_deliver = sub.add_parser("deliver", help="push 当前分支到远程（对外动作）")
    p_deliver.add_argument("--remote", default="origin")
    p_deliver.add_argument("--branch", default=None)
    p_deliver.add_argument("--no-set-upstream", dest="set_upstream", action="store_false", default=True)
    p_deliver.set_defaults(handler=_make_handler("deliver", "cli_deliver"), _cmd_path="deliver")
    p_pr = sub.add_parser("pr", help="PR 操作（对外动作）")
    sub_pr = p_pr.add_subparsers(dest="pr_cmd", required=True)
    p_pr_open = sub_pr.add_parser("open", help="gh pr create（body 可从 run-summary 派生）")
    p_pr_open.add_argument("--title", default=None)
    p_pr_open.add_argument("--body", default=None)
    p_pr_open.add_argument("--body-file", dest="body_file", default=None)
    p_pr_open.add_argument("--base", default=None)
    p_pr_open.add_argument("--draft", action="store_true")
    p_pr_open.set_defaults(handler=_make_handler("deliver", "cli_pr_open"), _cmd_path="pr open")

    # ===== status / cost / clean =====
    p_status = sub.add_parser("status", help="当前 run 进度一览（只读）")
    p_status.set_defaults(handler=_make_handler("status", "run"), _cmd_path="status")
    p_cost = sub.add_parser("cost", help="按后端拆 token 成本（Claude vs MiMo ...）")
    p_cost.add_argument("--since", default=None, help="如 7d/24h/30m/ISO")
    p_cost.set_defaults(handler=_make_handler("cost", "run"), _cmd_path="cost")
    p_clean = sub.add_parser("clean", help="清理陈旧/已中止 run 目录（默认 dry-run）")
    p_clean.add_argument("--yes", action="store_true", help="真删（默认 dry-run）")
    p_clean.add_argument("--keep-days", dest="keep_days", type=int, default=14)
    p_clean.set_defaults(handler=_make_handler("clean", "run"), _cmd_path="clean")

    # ===== agent =====
    p_agent = sub.add_parser(
        "agent", help="Sub-agent prompt 渲染与 spawn 引导语生成（v1.0+）"
    )
    sub_agent = p_agent.add_subparsers(dest="agent_cmd", required=True)

    p_agent_prompt = sub_agent.add_parser("prompt", help="Prompt 文件渲染")
    sub_agent_prompt = p_agent_prompt.add_subparsers(dest="prompt_cmd", required=True)

    p_agent_prompt_render = sub_agent_prompt.add_parser(
        "render", help="渲染 implement/fix prompt 到 disk（主 session 不接触模板内容）"
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
    p_sum_render = sub_sum.add_parser("render", help="从 STATE_JSON + RUN_EVENTS 派生")
    p_sum_render.set_defaults(
        handler=_make_handler("summary", "render"), _cmd_path="summary render"
    )

    # ===== index =====
    p_idx = sub.add_parser("index", help="跨 run 索引")
    sub_idx = p_idx.add_subparsers(dest="index_cmd", required=True)
    p_idx_app = sub_idx.add_parser("append", help="追加一行到 index.jsonl")
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

    p_tel_tail = sub_tel.add_parser("tail", help="看最近 N 条原始 telemetry 事件")
    p_tel_tail.add_argument("--kind", default=None, help="仅保留某 kind")
    p_tel_tail.add_argument("--last", type=int, default=20)
    p_tel_tail.set_defaults(
        handler=_make_handler("telemetry", "cli_tail"), _cmd_path="telemetry tail"
    )

    p_tel_agg = sub_tel.add_parser(
        "agg",
        help="按维度聚合并写 aggregates/by-<by>.json",
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
    )
    p_tel_hot.add_argument("--top", type=int, default=5)
    p_tel_hot.add_argument("--since", default=None)
    p_tel_hot.set_defaults(
        handler=_make_handler("telemetry", "cli_hotspots"), _cmd_path="telemetry hotspots"
    )

    p_tel_est = sub_tel.add_parser(
        "estimate-tokens", help="单文件 token 估算（bytes/4）",
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
