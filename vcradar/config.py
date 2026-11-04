"""配置：默认值 + /srv/vc-radar/state/config.toml 覆盖 + 命令行覆盖。不含任何密钥。"""
from __future__ import annotations

import os
import json
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


@dataclass
class Config:
    state_dir: Path = Path("/srv/vc-radar/state")
    env_file: Path = Path("/srv/vc-radar/.env")
    base_url: str = ""                 # 空串不带站内链接；通过发布验收后填真实基址。
    push_mode: str = "weekly"          # weekly | daily
    push_weekday: int = 4              # 周五
    holiday_shift: str = "prev_workday"
    trigger_time: str = "08:40"
    run_deadline: str = "08:50"
    file_deadline: str = "08:55"
    pickup_time: str = "08:58"
    push_time: str = "09:00"
    total_timeout_s: int = 600
    per_url_timeout_s: int = 30
    extract_concurrency: int = 4
    model: str = "deepseek-flash"
    # 测试运行时由 cli 设置
    test_mode: bool = False
    link_path_template: str = ""       # 留空指向最新一期；{date} 指向推送日期快照。
    links_file: Path = Path("/srv/vc-radar/links.json")
    review_site_passed: bool = False
    runtime_file: Path = Path(__file__).resolve().parent.parent / "config/runtime.json"
    backfill_verify_limit: int = 174
    verify_daily_limit: int = 60
    verify_daily_limit_after_break: int = 120
    verify_per_company_searches: int = 3
    verify_step_timeout: float = 120
    verify_search_timeout: float = 15
    verify_page_timeout: float = 15
    verify_concurrency: int = 4
    verify_recheck_confirmed_days: int = 30
    verify_recheck_unconfirmed_days: int = 7
    verify_search_price: float = 0.005
    verify_model_price_idle: float = 0.0035
    verify_model_price_peak: float = 0.007
    verify_query_templates: list[str] = field(default_factory=lambda: [
        '"{name}" {context} headquarters funding',
        '"{name}" {context} {amount} seed OR "Series A" {exclude}',
        '"{name}" {context} about contact location {exclude}',
    ])
    homepage_check_file: Path = Path(__file__).resolve().parent.parent / "config/homepage-check.json"
    enable_extended_sources: bool = False

    def __post_init__(self) -> None:
        validate_link_path_template(self.link_path_template)

    @property
    def db_path(self) -> Path:
        return self.state_dir / ("test/vc-radar-test.db" if self.test_mode else "vc-radar.db")

    @property
    def outbox_dir(self) -> Path:
        return self.state_dir / ("outbox-test" if self.test_mode else "outbox")

    @property
    def raw_dir(self) -> Path:
        return self.state_dir / ("test/raw" if self.test_mode else "raw")

    @property
    def log_dir(self) -> Path:
        return self.state_dir / ("test/logs" if self.test_mode else "logs")

    @property
    def usage_file(self) -> Path:
        return self.state_dir / ("test/extract-usage.json" if self.test_mode else "extract-usage.json")

    @property
    def rates_file(self) -> Path:
        """汇率缓存与测试数据使用同一套隔离目录。"""
        return self.state_dir / ("test/rates.json" if self.test_mode else "rates.json")


def validate_link_path_template(template: str) -> None:
    """拒绝日期占位以外的格式语法及不安全的路径模板。"""
    if not isinstance(template, str) or re.search(r"\.\.|//|[\s?#{}]", template.replace("{date}", "date")):
        raise ValueError("link_path_template 只允许 {date} 占位，不能含 ..、//、空白、? 或 #")


def load_config(path: Path | None = None, **overrides) -> Config:
    """读取 TOML 和运行覆盖项，并在启动时校验链接模板。"""
    cfg = Config()
    p = path or Path(os.environ.get("VC_RADAR_CONFIG", "/srv/vc-radar/state/config.toml"))
    if p.exists():
        data = tomllib.loads(p.read_text())
        names = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k in names:
                setattr(cfg, k, Path(v) if k in ("state_dir", "env_file", "links_file", "runtime_file", "homepage_check_file") else v)
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, v)
    validate_link_path_template(cfg.link_path_template)
    try:
        runtime = json.loads(cfg.runtime_file.read_text(encoding="utf-8"))
        cfg.review_site_passed = runtime.get("review_site_passed") is True
    except FileNotFoundError:
        cfg.review_site_passed = False
    return cfg


def is_test_run(args, cfg: Config | None = None, *, exclude: str | None = None) -> bool:
    """按 PRD 6.6 唯一的 A 类表判断测试运行，各入口共用。"""
    flags = ("fixture", "dry_run", "search_fixtures", "rates_file", "rates_fail", "now",
             "links_file", "assume_review_site_passed", "replay", "no_verify",
             "prod_rules", "reset_test_db", "test")
    return bool((cfg is not None and cfg.test_mode) or getattr(args, "to", None) == "test"
                or any(getattr(args, flag, None) is not None if flag == "now" else bool(getattr(args, flag, False))
                       for flag in flags if flag != exclude))


def effective_links_file(cfg: Config) -> Path:
    """测试只读写显式测试链接文件或测试库目录，生产使用配置路径。"""
    path = Path(cfg.links_file)
    if cfg.test_mode and path.resolve() == Config().links_file.resolve():
        return cfg.db_path.parent / "links.json"
    return path


def load_api_key(cfg: Config) -> str | None:
    """DEEPSEEK_API_KEY：先看环境变量，再读 .env。返回值绝不打印、不写日志。"""
    v = os.environ.get("DEEPSEEK_API_KEY")
    if v:
        return v
    try:
        for line in cfg.env_file.read_text().splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None
