"""命令行入口；先校验参数，再打开运行目录。"""
from __future__ import annotations

import argparse
from datetime import date, time
import math
from pathlib import Path
import re
import sqlite3
import sys

from .config import is_test_run, load_config


def _date(value: str) -> date:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日期格式必须是 YYYY-MM-DD") from None


def _time(value: str) -> time:
    try:
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            raise ValueError
        return time.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("时间格式必须是 HH:MM") from None


def _seconds(value: str) -> float:
    try:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("总超时必须是大于 0 的秒数") from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析并校验参数，取消的参数始终以退出码 2 拒绝。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="python -m vcradar", allow_abbrev=False)
    for token in argv:
        option = token.split("=", 1)[0]
        if option.startswith("--deep-budget-") or option == "--first-week-as-new":
            parser.error(f"该参数已取消：{option}")
    parser.add_argument("--date", type=_date)
    parser.add_argument("--now", type=_time)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test", action="store_true", help="使用测试库和 outbox-test，等价于 --to test")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--to", choices=["test"])
    parser.add_argument("--resend", action="store_true")
    parser.add_argument("--sync-sent", action="store_true")
    parser.add_argument("--reset-test-db", action="store_true")
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--deep-only", action="store_true")
    parser.add_argument("--deep-date", type=_date, help="深度补跑的首次纳入日期，默认今天")
    parser.add_argument("--company")
    parser.add_argument("--no-deep", action="store_true")
    parser.add_argument("--no-links", action="store_true", help="本次运行不带回看链接")
    parser.add_argument("--deep-deadline", type=_time, default=time(9, 30))
    parser.add_argument("--deep-final", type=_time, default=time(10, 30))
    parser.add_argument("--total-timeout", type=_seconds)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--first-issue", action="store_true")
    parser.add_argument("--prod-rules", action="store_true")
    parser.add_argument("--build-site", action="store_true", help="只读库生成回看页")
    parser.add_argument("--backfill-first-issue-gap", action="store_true")
    parser.add_argument("--fix-place-names", action="store_true", help="预览历史地名替换，带 --apply 才改库")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--preview-push", action="store_true")
    parser.add_argument("--search-fixtures", type=Path)
    parser.add_argument("--no-verify", action="store_true", help="测试运行跳过入库查证")
    parser.add_argument("--rates-file", type=Path)
    parser.add_argument("--rates-fail", action="store_true")
    parser.add_argument("--links-file", type=Path)
    parser.add_argument("--assume-review-site-passed", action="store_true")
    args = parser.parse_args(argv)
    if args.assume_review_site_passed and not is_test_run(args, exclude="assume_review_site_passed"):
        parser.error("--assume-review-site-passed 只能和其他测试标志一起用")
    args.test_flags = is_test_run(args)
    if args.apply and not (args.backfill_first_issue_gap or args.fix_place_names):
        parser.error("--apply 需要 --backfill-first-issue-gap 或 --fix-place-names")
    if args.report_dir is not None and not (args.backfill_first_issue_gap or args.fix_place_names):
        parser.error("--report-dir 需要 --backfill-first-issue-gap 或 --fix-place-names")
    if args.fix_place_names and (args.backfill_first_issue_gap or args.preview_push or args.first_issue
            or args.resend or args.sync_sent or args.deep_only or args.build_site):
        parser.error("--fix-place-names 不能与其他运行命令同用")
    if args.backfill_first_issue_gap or args.preview_push:
        if (args.first_issue or args.resend or args.sync_sent or args.deep_only or args.replay
                or args.build_site or args.reset_test_db or (args.backfill_first_issue_gap and args.preview_push)):
            parser.error("补充命令和预览不能与其他运行命令同用")
    if args.build_site and (args.deep_only or args.resend or args.sync_sent or args.reset_test_db
                            or args.replay or args.first_issue or args.fixture or args.live_model):
        parser.error("--build-site 需要单独运行")
    if args.prod_rules:
        if args.first_issue:
            parser.error("--prod-rules 不能和 --first-issue 同用")
    if args.deep_only and args.resend:
        parser.error("--deep-only 不能和 --resend 同用")
    if args.deep_date is not None and not args.deep_only:
        parser.error("--deep-date 需要 --deep-only")
    if args.deep_only and (args.no_deep or args.first_issue or args.sync_sent or args.replay):
        parser.error("--deep-only 不能和主流程或回放选项同用")
    if args.company is not None:
        if not args.deep_only or any(not part.strip() for part in args.company.split(",")):
            parser.error("--company 需要 --deep-only 和非空的公司 id 列表")
        args.company = list(dict.fromkeys(part.strip() for part in args.company.split(",")))
    if args.deep_final <= args.deep_deadline:
        parser.error("--deep-final 必须晚于 --deep-deadline")
    if args.first_issue and (args.resend or args.sync_sent or args.replay):
        parser.error("--first-issue 不能和 --resend、--sync-sent 或 --replay 同用")
    if args.replay and (args.fixture or args.resend or args.sync_sent):
        parser.error("--replay 不能和 --fixture、--resend 或 --sync-sent 同用")
    return args


def main(argv: list[str] | None = None) -> int:
    """运行主流程、回执同步或隔离回放，返回约定的退出码。"""
    args = parse_args(argv)
    try:
        cfg = load_config(args.config, total_timeout_s=args.total_timeout)
        cfg.test_mode = is_test_run(args, cfg)
        if args.fix_place_names:
            # 修复入口不启动抓取、模型、发布、配置写入或测试库重置。
            from .pipeline import Clock
            from .placenames import run_fix_place_names
            return run_fix_place_names(cfg, args, Clock(args.date, args.now))
        if args.preview_push and not cfg.test_mode:
            print("--preview-push 只在测试模式可用", file=sys.stderr)
            return 2
        if args.links_file is not None:
            cfg.links_file = args.links_file
        from .config import effective_links_file
        cfg.links_file = effective_links_file(cfg)
        if args.assume_review_site_passed:
            cfg.review_site_passed = True
        if args.no_links:
            cfg.review_site_passed = False
        if cfg.review_site_passed and not cfg.base_url:
            if cfg.test_mode:
                print("警告：review_site_passed=true 但 base_url 为空，本次省略网页链接", file=sys.stderr)
                cfg.review_site_passed = False
            else:
                print("运行失败：review_site_passed=true 时 base_url 不能为空", file=sys.stderr)
                return 1
        if cfg.review_site_passed:
            from urllib.parse import urlsplit
            parts = urlsplit(cfg.base_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("review_site_passed=true 时需要有效的 base_url")
        if args.build_site:
            from datetime import datetime
            from .config import BJ
            from .site import build_site
            current = datetime.now(BJ)
            moment = datetime.combine(args.date or current.date(), args.now or current.time(), tzinfo=BJ)
            conn = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                build_site(conn, cfg, now=moment)
            finally:
                conn.close()
            print("回看页已生成")
            return 0
        from .pipeline import Clock, run
        clock = Clock(args.date, args.now)
        if args.backfill_first_issue_gap:
            from .backfill import run_backfill
            return run_backfill(cfg, args, clock)
        if args.preview_push:
            from .pipeline import preview_push
            return preview_push(cfg, args, clock)
        if args.replay:
            from .replay import run_replay
            return run_replay(cfg, args, clock)
        return run(cfg, args, clock)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, sqlite3.Error) as exc:
        # 不打印任意异常正文，避免外部异常里携带请求头或密钥。
        print(f"运行失败（{type(exc).__name__}）", file=sys.stderr)
        return 1

