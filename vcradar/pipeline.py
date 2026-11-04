"""阶段 1 串联：抓取、抽取、扫变更、周汇总和文件交接。"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time as daytime, timedelta
import fcntl
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from threading import Lock
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

from . import cal, config, db, extract, fetcher, fx, http, llm, outbox, render, scan, wording
from .config import BJ, Config, UA
from .guard import check_no_fictional, check_no_placeholder
from .models import DigestData, FetchResult, HttpResponse
from .robots import RobotsChecker
from .sources.registry import ALL_SOURCES, DISPLAY_ORDER, P0_SOURCES, display_rank
from .sources.yc_directory import batch_order, canonical_latest_batches

_FUNDING = re.compile(
    r"raise[sd]?|funding|seed|pre-seed|Series [A-Z]|\$\d[\d.]*\s?(?:M|B|million|billion)|"
    r"backs|lands|led by|investment", re.I,
)
_SOURCE_ORDER = {name: n for n, name in enumerate(DISPLAY_ORDER)}


class Clock:
    """北京时间时钟；模拟日期/钟点之后仍随真实流逝秒数前进。"""

    def __init__(self, day: date | None = None, now: daytime | None = None):
        real = datetime.now(BJ)
        self.simulated = day is not None or now is not None
        self._base = datetime.combine(day or real.date(), now or real.timetz(), tzinfo=BJ)
        self._started = time.monotonic()
        self._jump = 0.0
        self._lock = Lock()

    def now(self) -> datetime:
        """返回 aware 北京时间。"""
        with self._lock:
            return self._base + timedelta(seconds=time.monotonic() - self._started + self._jump)

    def advance(self, seconds: float) -> None:
        """fixture 中跳过 Term Sheet 的等待，保留模拟时间流逝。"""
        with self._lock:
            self._jump += max(0.0, seconds)
            self.simulated = True


class _Budget:
    def __init__(self, cfg, clock, *, scheduled=True):
        self.clock = clock
        self.started = getattr(clock, "run_started_monotonic", time.monotonic())
        self.started_at = getattr(clock, "run_started_at", clock.now())
        self.total = float(cfg.total_timeout_s)
        self.deadline = datetime.combine(clock.now().date(), daytime.fromisoformat(cfg.run_deadline), tzinfo=BJ) if scheduled else None

    def remaining(self):
        elapsed = max(time.monotonic() - self.started, (self.clock.now() - self.started_at).total_seconds())
        return max(0.0, min(self.total - elapsed,
                            (self.deadline - self.clock.now()).total_seconds() if self.deadline else float("inf")))


class _ClockView:
    def __init__(self, clock, simulated=False):
        self.clock = clock
        self.simulated = bool(getattr(clock, "simulated", False) or simulated)
        self.offset = 0.0
        self.lock = Lock()
        self.run_started_monotonic = time.monotonic()
        self.run_started_at = self.now()

    def now(self):
        with self.lock:
            now = self.clock.now()
            if now.utcoffset() is None:
                raise ValueError("时钟必须返回 aware datetime")
            return now.astimezone(BJ) + timedelta(seconds=self.offset)

    def advance(self, seconds):
        with self.lock:
            self.offset += max(0.0, seconds)
            self.simulated = True


class _RunLog:
    def __init__(self, path):
        self.path = path
        self.lock = Lock()

    def write(self, record):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class _Transport:
    def __init__(self, client, budget, cfg, log):
        self.client, self.budget, self.cfg, self.log = client, budget, cfg, log
        self.active = True
        self.lock = Lock()
        self.counts = defaultdict(int)
        self.pages = {}

    def get(self, url, headers=None, timeout=30.0, data=None):
        remaining = self.budget.remaining()
        if remaining <= 0:
            return HttpResponse(url, None, error="timeout")
        result = self.client.get(url, headers=headers, timeout=min(timeout, self.cfg.per_url_timeout_s, remaining), data=data)
        parsed = urlsplit(url)
        with self.lock:
            if self.active:
                if result.status is not None and 200 <= result.status < 300 and not result.error:
                    self.pages[url] = result.body
                    if result.final_url:
                        self.pages[result.final_url] = result.body
                self.log.write({"event": "fetch", "host": parsed.netloc, "path": parsed.path,
                                "status": result.status, "at": self.budget.clock.now().isoformat()})
                source_id = http._source_id(url)
                if source_id:
                    self.counts[source_id] += 1
                    http.save_raw_response(result, self.cfg.raw_dir, source_id, self.counts[source_id], self.budget.clock.now())
        return result

    def close(self):
        with self.lock:
            self.active = False


class _MissingModel:
    def __init__(self, model):
        self.model = model

    def complete_json(self, system, user, timeout_s):
        return llm.LLMResult(False, error="missing_key")


def model_client(cfg: Config, fixture: Path | None, response_count: int = 1):
    """fixture 只用 Mock；真实运行缺密钥时返回规则降级客户端。"""
    if fixture:
        path = Path(fixture) / "model_responses.json"
        if path.exists():
            responses = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(responses, dict):
                responses = responses["responses"]
            if not isinstance(responses, list):
                raise ValueError("model_responses.json 必须是数组")
        else:
            uncertain = {"ai": {"verdict": "拿不准", "quote": ""},
                         "social": {"verdict": "拿不准", "quote": ""}, "summary": ""}
            responses = [uncertain] * max(1, response_count * 2)
        return llm.MockLLMClient(responses, model=cfg.model)
    key = config.load_api_key(cfg)
    return llm.DeepSeekClient(key, model=cfg.model) if key else _MissingModel(cfg.model)


class ModelSession:
    """记录每次调用起止和用量；截止后不允许后台线程继续落盘。"""

    def __init__(self, client, cfg, clock, budget, log):
        self.client, self.cfg, self.clock, self.budget, self.log = client, cfg, clock, budget, log
        self.model = client.model
        self.lock = Lock()
        self.active = True
        self.inflight = {}
        self.usage = defaultdict(lambda: {"calls": 0, "failed_calls": 0, "prompt_tokens": 0,
                                          "prompt_cache_hit_tokens": 0, "completion_tokens": 0,
                                          "estimated_cost": 0.0})

    def complete_json(self, system, user, timeout_s):
        """以主流程剩余时间限制一次调用，并按调用开始时刻计费。"""
        with self.lock:
            remaining = self.budget.remaining()
            if not self.active or remaining <= 0:
                return llm.LLMResult(False, error="timeout")
            started = self.clock.now()
            call_id = uuid4().hex
            self.inflight[call_id] = started
            self.log.write({"event": "model_start", "call_id": call_id, "model": self.model,
                            "at": started.isoformat()})
        try:
            result = self.client.complete_json(system, user, min(timeout_s, remaining))
        except Exception:
            result = llm.LLMResult(False, error="model_error")
        with self.lock:
            if not self.active:
                return llm.LLMResult(False, error="timeout")
            self.inflight.pop(call_id)
            month = self.usage[started.strftime("%Y-%m")]
            month["calls"] += 1
            month["failed_calls"] += int(not result.ok)
            tokens = result.usage or {}
            counts = {key: value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
                      for key, value in ((key, tokens.get(key, 0)) for key in (
                          "prompt_tokens", "prompt_cache_hit_tokens", "completion_tokens"))}
            counts["prompt_cache_hit_tokens"] = min(counts["prompt_tokens"], counts["prompt_cache_hit_tokens"])
            for key, value in counts.items():
                month[key] += value
            month["estimated_cost"] += extract.usage_cost(counts, started)
            self.log.write({"event": "model_end", "call_id": call_id, "model": self.model,
                            "at": self.clock.now().isoformat(), "ok": result.ok, "usage": counts,
                            "missing_key": result.error == "missing_key"})
        return result

    def close(self):
        """封存本次用量，原子累加到月份账本。"""
        with self.lock:
            self.active = False
            for call_id, started in self.inflight.items():
                month = self.usage[started.strftime("%Y-%m")]
                month["calls"] += 1
                month["failed_calls"] += 1
                self.log.write({"event": "model_end", "call_id": call_id, "model": self.model,
                                "at": self.clock.now().isoformat(), "ok": False, "reason": "总超时"})
            self.inflight.clear()
            if not self.usage:
                return
            path = self.cfg.usage_file
            data = json.loads(path.read_text()) if path.exists() else {"model": self.model, "months": {}}
            for month, values in self.usage.items():
                saved = data.setdefault("months", {}).setdefault(month, {})
                for key, value in values.items():
                    saved[key] = saved.get(key, 0) + value
            outbox.write_atomic(path, data)


def _peak(stamp):
    hour = stamp.hour + stamp.minute / 60
    return stamp.weekday() < 5 and cal.is_cn_workday_or_weekday(stamp.date()) and (9 <= hour < 12 or 14 <= hour < 18)


@contextmanager
def run_lock(cfg: Config):
    """持有生产或测试各自的单实例锁；竞争时立即退出。"""
    directory = cfg.state_dir / "test" if cfg.test_mode else cfg.state_dir
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有实例运行") from None
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reset_test_db(cfg: Config) -> None:
    """清测试库及其 sidecar、raw、缓存和 outbox-test。"""
    test_cfg = replace(cfg, test_mode=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(test_cfg.db_path) + suffix).unlink(missing_ok=True)
    for path in (test_cfg.raw_dir, cfg.state_dir / "test-cache", test_cfg.outbox_dir):
        if path.exists():
            shutil.rmtree(path)
    test_cfg.rates_file.unlink(missing_ok=True)
    # 官网测试记录有独立副本，重置只读取 research，不改它。
    links = test_cfg.db_path.parent / "links.json"
    links.parent.mkdir(parents=True, exist_ok=True)
    source_links = Config().links_file
    if source_links.is_file():
        shutil.copyfile(source_links, links)
    else:
        links.write_text("[]", encoding="utf-8")


def _stale_paths(cfg):
    paths = list(cfg.db_path.parent.glob("*.first-tmp.db*"))
    paths.extend(cfg.db_path.parent.glob(".first-stage-*"))
    paths.extend(cfg.outbox_dir.glob(".*.json.*.tmp"))
    paths.extend(cfg.outbox_dir.glob("*.tmp"))
    if cfg.test_mode:
        site = cfg.state_dir / "test-site"
        paths.extend(path for path in site.rglob("*") if path.is_dir() and (
            path.name.startswith((".tmp-", ".first-stage-")) or path.name.endswith((".tmp", "-tmp"))
            or ".tmp-" in path.name or ".first-tmp" in path.name))
    return sorted(set(paths), key=lambda path: (len(path.parts), str(path)))


def cleanup_stale(cfg: Config) -> None:
    """在持有运行锁时清理强杀残留，保留正式交接文件和已完成的导出。"""
    for path in _stale_paths(cfg):
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def _stamp(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp.replace(tzinfo=BJ) if stamp.tzinfo is None else stamp.astimezone(BJ)


def _first_file(cfg, day):
    if cfg.test_mode:
        return _test_issue_file(cfg, day, first=True)
    path = outbox.first_path(cfg.outbox_dir, day)
    return path if path.is_file() else None


def _on_first_day(cfg, day):
    first = outbox.first_path(cfg.outbox_dir, day)
    return (first.exists() or first.with_suffix(".sent").exists() or
            (cfg.test_mode and (any(cfg.outbox_dir.glob(f"{day}-*-first.json"))
                                or any(cfg.outbox_dir.glob(f"{day}-*-first.sent")))))


@contextmanager
def _read_database(path):
    # 已关闭库无需创建 WAL/SHM；有 WAL 时必须读取已提交但尚未 checkpoint 的数据。
    suffix = "?mode=ro" if Path(str(path) + "-wal").exists() else "?mode=ro&immutable=1"
    conn = sqlite3.connect(Path(path).resolve().as_uri() + suffix, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def preflight(cfg: Config, args, day: date) -> str | None:
    """只读检查首期及生产守卫，拒绝时不创建目录、库或锁文件。"""
    if args.sync_sent or args.deep_only or args.resend:
        return None
    if not args.first_issue and cfg.test_mode and not args.prod_rules:
        return None
    empty = True
    if cfg.db_path.exists() and not args.reset_test_db:
        with _read_database(cfg.db_path) as conn:
            empty = db.is_empty(conn)
    if args.first_issue:
        if not empty:
            return "库不为空，不能再跑 --first-issue"
        if not args.reset_test_db and _on_first_day(cfg, day):
            return "今天是首期日，只允许 --resend"
    elif not cfg.test_mode or args.prod_rules:
        if not args.reset_test_db and _on_first_day(cfg, day):
            return "今天是首期日，只允许 --resend"
        if empty:
            return "库为空，请先 --first-issue"
    return None


class _FictionalData(ValueError):
    pass


class _PlaceholderData(ValueError):
    pass


def validate_delivery(conn, text: str, *, test_mode: bool = False) -> None:
    """写交接文件前检查禁用名及占位地址；测试模式的占位地址只警告。"""
    if check_no_fictional(text) or any(check_no_fictional(line) for line in conn.iterdump()):
        raise _FictionalData("交付数据包含禁用虚构名")
    if hits := check_no_placeholder(text):
        if test_mode:
            print("警告：交付文本包含占位地址：" + "、".join(hits), file=sys.stderr)
        else:
            raise _PlaceholderData("交付文本包含占位地址")


@dataclass
class _RunResult:
    path: Path
    data: DigestData
    listing: dict
    incomplete_sources: dict
    notes: str
    baseline_items: int
    digest: dict
    source_bodies: dict | None = None


class _Promotion:
    """逐文件原子替换并记录撤销信息，首期交接文件最后公开。"""

    def __init__(self, undo_dir):
        self.undo_dir = undo_dir
        self.changes = []
        self.created_dirs = []

    def move(self, source, target):
        missing = []
        parent = target.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for parent in reversed(missing):
            parent.mkdir()
            self.created_dirs.append(parent)
        backup = None
        if target.exists():
            backup = self.undo_dir / str(len(self.changes))
            shutil.copy2(target, backup)
        os.replace(source, target)
        self.changes.append((target, backup))

    def rollback(self):
        for target, backup in reversed(self.changes):
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(backup, target)
        for directory in reversed(self.created_dirs):
            directory.rmdir()

    def remove(self, path):
        backup = self.undo_dir / str(len(self.changes))
        shutil.copy2(path, backup)
        path.unlink()
        self.changes.append((path, backup))


def _first_issue(cfg, args, clock, today):
    directory = cfg.db_path.parent
    with tempfile.TemporaryDirectory(prefix=".first-stage-", dir=directory) as temporary:
        root = Path(temporary)
        staged_cfg = replace(cfg, state_dir=root / "state")
        staged_cfg.links_file = staged_cfg.db_path.parent / "links.json"
        temp_db = directory / f"{cfg.db_path.stem}-{uuid4().hex}.first-tmp.db"
        promotion = _Promotion(root / "undo")
        promotion.undo_dir.mkdir()
        conn = None
        try:
            if cfg.db_path.exists():
                with _read_database(cfg.db_path) as old:
                    db.backup(old, temp_db)
            if cfg.usage_file.exists():
                staged_cfg.usage_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cfg.usage_file, staged_cfg.usage_file)
            if cfg.rates_file.exists():
                staged_cfg.rates_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cfg.rates_file, staged_cfg.rates_file)
            if cfg.links_file.exists():
                staged_cfg.links_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cfg.links_file, staged_cfg.links_file)
            conn = db.connect(temp_db)
            result = _run_connected(conn, staged_cfg, args, clock, today)
            if not isinstance(result, _RunResult):
                raise RuntimeError("首期没有完成")
            validate_delivery(conn, result.digest["text"], test_mode=cfg.test_mode)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.close()
            conn = None
            final_path = cfg.outbox_dir / result.path.name
            status_path = staged_cfg.state_dir / ("test/status.json" if cfg.test_mode else "status.json")
            status = json.loads(status_path.read_text())
            status["outbox"] = str(final_path)
            outbox.write_atomic(status_path, status)
            for path in sorted(staged_cfg.state_dir.rglob("*")):
                if not path.is_file() or path == result.path:
                    continue
                if path == staged_cfg.links_file and cfg.links_file.exists() and path.read_bytes() == cfg.links_file.read_bytes():
                    continue
                target = cfg.links_file if path == staged_cfg.links_file else cfg.state_dir / path.relative_to(staged_cfg.state_dir)
                if path.parent == staged_cfg.log_dir and target.exists():
                    path.write_bytes(target.read_bytes() + path.read_bytes())
                if path.suffix == ".html" or {"test-site", "export", "site"}.intersection(path.parts):
                    if path.suffix in {".json", ".html", ".txt"} and check_no_fictional(path.read_text(encoding="utf-8")):
                        raise _FictionalData("导出页面包含禁用虚构名")
                promotion.move(path, target)
            # 提交前移走旧库的 sidecar，避免旧 WAL 被套到新的数据库文件上。
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(cfg.db_path) + suffix)
                if sidecar.exists():
                    promotion.remove(sidecar)
            if _Budget(cfg, clock, scheduled=False).remaining() <= 0:
                raise RuntimeError("首期总超时")
            promotion.move(temp_db, cfg.db_path)
            promotion.move(result.path, final_path)
            _first_report(result, final_path)
            return 0
        except Exception as exc:
            if conn is not None:
                conn.close()
                conn = None
            promotion.rollback()
            print(f"首期失败，已整体回滚（{type(exc).__name__}）", file=sys.stderr)
            return 1
        finally:
            if conn is not None:
                conn.close()
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(temp_db) + suffix).unlink(missing_ok=True)


def _first_report(result, path):
    counts, hints = render.push_first_lines(result.digest["text"])
    print(result.digest["text"])
    print(f"基线条数：{result.baseline_items}（去重公司 {result.data.baseline_count} 家）")
    print(f"首期卡片数：{len(result.listing['cards'])}")
    print(f"首行文本行 1：{counts}")
    print(f"首行文本行 2：{hints}")
    print("未补全的信源和日期：" + ("；".join(f"{name}：{detail}" for name, detail in result.incomplete_sources.items()) or "无"))
    print(f"P0-9 备注：{result.notes or '无'}")
    digest = {key: result.digest.get(key) for key in ("date", "push", "sent_at", "sent_result")}
    if not digest["sent_at"]:
        digest["sent_at"] = "待 --sync-sent"
    digest["count"] = len(result.listing["cards"])
    print("首期 digest（发送时间/条数/结果）：" + json.dumps(digest, ensure_ascii=False))
    print(f"-first.json 路径：{path}")


def _main_file(cfg, day):
    if not cfg.test_mode:
        path = outbox.main_path(cfg.outbox_dir, day)
        return path if path.exists() else None
    return _test_issue_file(cfg, day, first=False)


def _push_handoffs(cfg, today):
    # 只识别普通主文件和首期文件；重发、预览、补充清单均不属于判定范围。
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2})" + (r"-\d{6}" if cfg.test_mode else "") + r"(-first)?\.json")
    records = []
    for path in cfg.outbox_dir.glob("*.json"):
        match = pattern.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        try:
            day = date.fromisoformat(match[1])
            if day > today:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("push") is True:
            records.append((day, path, payload, bool(match[2])))
    return records


def _test_issue_file(cfg, day, *, first):
    records = [record for record in _push_handoffs(cfg, day) if record[0] == day and record[3] == first]
    return max((record[1] for record in records), key=lambda path: path.name, default=None)


def _latest_handoff(cfg, today):
    records = _push_handoffs(cfg, today)
    if not records:
        return None
    latest = max(record[0] for record in records)
    records = [record for record in records if record[0] == latest]
    if cfg.test_mode:
        ordinary = [record for record in records if not record[3]]
        # 普通文件只在自己的 HHMMSS 范围取最大；首期使用自己的同主名回执。
        return max(ordinary or records, key=lambda record: record[1].name)
    if any(record[3] for record in records):
        records = [record for record in records if record[3]]
    received = [record for record in records if record[1].with_suffix(".sent").is_file()]
    return max(received or records, key=lambda record: record[1].name)


def _record_ts_notice(conn, cfg, today):
    """仅按最近已生成推送主文件的真实回执记录停抓提示日期。"""
    row = conn.execute("SELECT 1 FROM source WHERE id='ts_web' AND crawl_status='已停抓' "
                       "AND (stop_notice_date IS NULL OR stop_notice_date='')").fetchone()
    if row is None:
        return
    handoff = _latest_handoff(cfg, today)
    if handoff is None:
        return
    day, path, payload, _ = handoff
    try:
        receipt = outbox._read_receipt(path.with_suffix(".sent"))
        sent_day = _stamp(receipt.get("sent_at")).date() if receipt.get("sent_at") else None
    except (OSError, ValueError, TypeError, AttributeError):
        return
    phrases = ("Term Sheet 没抓到：对方不再允许抓取。", "Term Sheet 没抓到：对方要求停止抓取。")
    if (receipt.get("result") == "ok" and sent_day == day and "兜底" not in str(receipt.get("note", ""))
            and isinstance(payload.get("text"), str) and any(phrase in payload["text"] for phrase in phrases)):
        conn.execute("UPDATE source SET stop_notice_date=? WHERE id='ts_web' AND crawl_status='已停抓' "
                     "AND (stop_notice_date IS NULL OR stop_notice_date='')", (str(day),))


def _source_lines(outcomes, states):
    groups = defaultdict(list)
    for outcome in outcomes:
        groups[outcome.source.name].append(outcome)
    from .models import SourceStatusLine
    lines = []
    for name, group in sorted(groups.items(), key=lambda pair: min(display_rank(o.source.id) for o in pair[1])):
        active = [o for o in group if not (o.stopped_now or states[o.source.id].crawl_status == "已停抓")]
        failed = [o for o in active if not o.ok]
        count = sum(o.runs[-1].count if o.runs else 0 for o in active)
        label = f"{count} 条" if all(o.source.kind == "rss" for o in group) else next(
            (o.latest_label for o in group if o.latest_label), f"{count} 条")
        latest = max((o.latest_beijing_date for o in group if o.latest_beijing_date), default=None)
        baseline = next((states[o.source.id].baseline_at for o in active if states[o.source.id].baseline_at), None)
        missed, start, end = fetcher.merge_missed_ranges(
            [(o.missed_range, o.missed_start, o.missed_end) for o in active],
            baseline,
            reference=max((r.started_at for o in active for r in o.runs), default=None))
        lines.append(SourceStatusLine(name, not failed, not active, label,
                                      failed[0].error_type if failed else None, latest, missed,
                                      start, end,
                                      min(group, key=lambda o: display_rank(o.source.id)).source.id))
    return lines


def _with_missed_history(conn, lines, window_start, window_end):
    """并入推送日当天库里已有的漏抓段（PRD L702「报警行只看推送日当天」；13-#48 ③ 插入行按推送日当天的运行记录处理）。"""
    from .models import SourceStatusLine
    day_start = datetime.combine(window_end.astimezone(BJ).date(), datetime.min.time(), BJ)
    window_start = day_start if window_start is None or window_start < day_start else window_start
    grouped = {line.name: replace(line) for line in lines}
    segments = defaultdict(list)
    for line in lines:
        if not line.stopped:
            segments[line.name].append((line.missed_range, line.missed_start, line.missed_end))
    for row in scan.missed_runs_in_window(conn, window_start, window_end):
        name, source_id = row["name"], row["source_id"]
        line = grouped.setdefault(name, SourceStatusLine(name, True, False, "", source_id=source_id))
        if line.stopped:
            continue
        if line.source_id is None or display_rank(source_id) < display_rank(line.source_id):
            line.source_id = source_id
        segments[name].append((row["missed_range"], row["missed_start"], row["missed_end"]))
    baseline = db.first_issue_generated_at(conn)
    for name, spans in segments.items():
        line = grouped[name]
        line.missed_range, line.missed_start, line.missed_end = fetcher.merge_missed_ranges(
            spans, baseline, reference=window_end)
    return sorted(grouped.values(), key=lambda line: (display_rank(line.source_id), line.name))


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.ignored = 0
        self.parts, self.lines = [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.ignored += 1
        if tag == "p" and not self.ignored:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "p" and self.depth:
            self.depth -= 1
            self.lines.append(" ".join("".join(self.parts).split()))
            self.parts.clear()

    def handle_data(self, data):
        if self.depth and not self.ignored:
            self.parts.append(data)


def _article_bodies(entries, sources, transport, robots, cfg, clock, budget):
    candidates = [(item, baseline) for _, item, baseline in entries
                  if sources[item.source_id].kind == "rss" and len(item.body) < 1500
                  and _FUNDING.search(item.title + " " + item.excerpt)]
    candidates.sort(key=lambda pair: pair[0].published_at or datetime.min.replace(tzinfo=BJ), reverse=True)
    fetched = {}
    for item, _ in candidates:
        if len(fetched) >= 30 or budget.remaining() <= 0:
            break
        if item.url in fetched:
            item.body = fetched[item.url] or item.body
            continue
        fetched[item.url] = ""
        if not robots.allowed(item.url):
            continue
        response = robots.request(item.url, lambda: transport.get(item.url, headers={"User-Agent": UA}, timeout=30.0),
                                  lambda seconds: time.sleep(min(seconds, budget.remaining())))
        if response is None:
            continue
        http.save_raw_response(response, cfg.raw_dir, f"article-{len(fetched)}", 1, clock.now())
        if response.status is not None and 200 <= response.status < 300 and not fetcher._intercepted_html(response):
            parser = _Paragraphs()
            parser.feed(response.body)
            fetched[item.url] = "\n".join(line for line in parser.lines if line)[:8000]
            item.body = fetched[item.url] or item.body


def _eligible(item, ext, batches):
    country, _ = scan.judge_country(item, ext)
    normalized = scan.normalize_round(ext.round_text, is_fund=ext.is_fund_raise, is_debt_only=ext.is_debt_only)
    return not scan.filter_reasons(item, ext, country, normalized, batches)


def rule_extract(item):
    """衔接标题首字母大写的融资动词，保留原标题作为规则依据。"""
    ext = extract.rule_extract(item)
    if not ext.company_name:
        title = re.sub(r"\b(?:Raises?|Raised|Lands?|Secures?|Closes?|Announces?)\b", lambda match: match[0].lower(), item.title)
        candidate = extract.rule_extract(replace(item, title=title))
        if candidate.company_name:
            ext.company_name = candidate.company_name
            ext.quotes["company_name"] = item.title
            ext.complete_by_rules = all((ext.company_name, ext.hq_text, ext.round_text, ext.amount_text))
    return ext


def ingest_items(conn, entries, *, cfg, clock, budget, client, batches, fx_rates=None):
    """规则先跑；仅可能进名单的非基线条目并发补字段或写简介，库操作留在主线程。"""
    ready, jobs = [], []
    for item_id, item, baseline in entries:
        ext = rule_extract(item)
        if baseline or not _eligible(item, ext, batches):
            ready.append((item_id, item, baseline, ext))
        else:
            jobs.append((item_id, item, baseline, ext))

    def enrich(job):
        item_id, item, baseline, ext = job
        mode = "summary" if ext.complete_by_rules else "full"
        result = extract.model_extract(item, ext, client, mode, budget_s=budget.remaining())
        return item_id, item, baseline, result

    pool = ThreadPoolExecutor(max_workers=min(4, max(1, cfg.extract_concurrency)))
    from contextvars import copy_context
    futures = {pool.submit(copy_context().run, enrich, job) for job in jobs} if budget.remaining() > 0 else set()
    incomplete = bool(jobs and not futures)
    try:
        while futures and budget.remaining() > 0:
            done, futures = wait(futures, timeout=budget.remaining(), return_when=FIRST_COMPLETED)
            for future in done:
                ready.append(future.result())
        incomplete = incomplete or bool(futures)
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    done_ids = {entry[0] for entry in ready}
    for item_id, _, _ in entries:
        if item_id not in done_ids:
            conn.execute("UPDATE raw_item SET skip_reason='这次没轮到抽取' WHERE id=?", (item_id,))
    # 固定来源顺序入库，模型完成顺序不会影响去重和来源先后。
    ready.sort(key=lambda entry: entry[0])
    for item_id, item, baseline, ext in ready:
        if budget.remaining() <= 0:
            incomplete = True
            conn.execute("UPDATE raw_item SET skip_reason='这次没轮到抽取' WHERE id=?", (item_id,))
            continue
        ingested_at = clock.now()
        scan.ingest(conn, item_id, item, ext, now=ingested_at, is_baseline=baseline,
                    is_backfill=bool(item.structured.get("backfill")), latest_yc_batches=batches,
                    model=ext.model_used, first_ingested_at=ingested_at, fx_rates=fx_rates)
        conn.execute("UPDATE raw_item SET skip_reason='' WHERE id=? AND skip_reason='这次没轮到抽取'", (item_id,))
    return incomplete


def _listing_for_issue(conn, day: date, include_ids=(), only_ids=None):
    """保留同日已列事件；金额、来源和首次入库排序全部由 core 提供。"""
    pending = {row[0] for row in conn.execute("SELECT id FROM funding_event WHERE pending=1 AND baseline_unpushed=0")}
    ids = (pending | set(include_ids)) if only_ids is None else (pending | set(include_ids)) & set(only_ids)
    listing = scan.collect_listing(conn, day, weekly=True, event_ids=ids)
    sorting = {}
    for event_id in listing["event_ids"]:
        row = conn.execute("SELECT company_id, round_norm FROM funding_event WHERE id=?", (event_id,)).fetchone()
        sorting[row["company_id"], row["round_norm"]] = scan.card_sort_data(conn, event_id)
    unique = {}
    ranks = {"天使轮": 0, "种子轮": 1, "A 轮": 2}
    for card in listing["cards"]:
        round_key = "待核" if card.round_label == "轮次待确认" else card.round_label
        card.amount_usd, seen = sorting[card.company_id, round_key]
        current = unique.get(card.company_id)
        if current is None or (ranks.get(card.round_label, -1), card.event_date or date.min) > (ranks.get(current.round_label, -1), current.event_date or date.min):
            unique[card.company_id] = card
    listing["cards"] = list(unique.values())
    for card in listing["cards"]:
        round_key = "待核" if card.round_label == "轮次待确认" else card.round_label
        listing["first_seen"][card.company_id] = sorting[card.company_id, round_key][1]
    return listing


def _resend_fields(cfg, day):
    receipts = outbox.read_sent(cfg.outbox_dir, day)
    is_first = _on_first_day(cfg, day)
    resends = receipts["first_resends" if is_first else "resends"]
    prefix = "first-resend" if is_first else "resend"
    pattern = re.compile(re.escape(str(day)) + "-" + prefix + r"-([1-9]\d*)(?:-\d{6})?\.json")
    files = [path for path in cfg.outbox_dir.glob(f"{day}-{prefix}-*.json") if pattern.fullmatch(path.name)]
    if cfg.test_mode:
        for path in files:
            if path.with_suffix(".sent").exists():
                resends[int(pattern.fullmatch(path.name)[1])] = json.loads(path.with_suffix(".sent").read_text())
    values = {"date": day, "resend_count": len(files)}
    if resends:
        latest = resends[max(resends)]
        values.update(resend_sent_at=latest.get("sent_at"), resend_sent_result=latest.get("result"))
    return values


def sync_sent(conn, cfg: Config, day: date) -> None:
    """回写普通或首期主文件及重发回执，随后按主文件确认停抓提示日期。"""
    if cfg.test_mode:
        handoff = _latest_handoff(cfg, day)
        target = handoff[0] if handoff else day
        path = handoff[1].with_suffix(".sent") if handoff else None
        main = outbox._read_receipt(path) if path is not None and path.is_file() else None
    else:
        target = day
        receipts = outbox.read_sent(cfg.outbox_dir, day)
        main = receipts["first"] or receipts["main"]
    values = _resend_fields(cfg, target)
    if main:
        values.update(sent_at=main.get("sent_at"), sent_result=main.get("result"))
    scan.upsert_digest(conn, values)
    _record_ts_notice(conn, cfg, day)


def _resend(conn, cfg, args, clock, today):
    first = _on_first_day(cfg, today)
    path = _first_file(cfg, today) if first else _main_file(cfg, today)
    digest = conn.execute("SELECT * FROM digest WHERE date=?", (str(today),)).fetchone()
    if path is not None:
        payload = json.loads(path.read_text())
    elif digest is not None and digest["text"]:
        payload = {"text": digest["text"], "generated_at": digest["generated_at"], "alarm": digest["status"] in {"没跑完", "全部失败", "部分失败"}}
    else:
        raise ValueError("没有可重发的主文件或 digest")
    text = wording.legacy_to_display(payload["text"])
    if not cfg.review_site_passed or not cfg.base_url:
        text = render.remove_page_links(text)
    text = render.add_resend_notice(text)
    validate_delivery(conn, text, test_mode=cfg.test_mode)
    writer = outbox.write_first_resend if first else outbox.write_resend
    path = writer(cfg.outbox_dir, today, alarm=payload["alarm"], generated_at=_stamp(payload["generated_at"]),
                  text=text, test=cfg.test_mode, real_now=datetime.now(BJ))
    scan.upsert_digest(conn, _resend_fields(cfg, today))
    conn.commit()
    from .site import write_status
    write_status(conn, cfg, clock.now(), runtime={"date": str(today), "push": True, "alarm": payload["alarm"],
        "outbox": str(path), "simulated": bool(clock.simulated)})
    print(path)
    return 0


def _cleanup_raw(directory, today):
    for path in directory.iterdir() if directory.exists() else ():
        try:
            old = date.fromisoformat(path.name) < today - timedelta(days=14)
        except ValueError:
            continue
        if path.is_dir() and old:
            shutil.rmtree(path)


def _status(outcomes, lines, listing, incomplete):
    if incomplete or any(run.result == FetchResult.NOT_RUN for outcome in outcomes for run in outcome.runs):
        return "没跑完"
    active = [line for line in lines if not line.stopped]
    active_names = {line.name for line in active}
    if active and not any(outcome.ok for outcome in outcomes if outcome.source.name in active_names):
        return "全部失败"
    if any(not line.ok for line in active):
        return "部分失败"
    return "有新增" if listing["event_ids"] else "没有新增"


def _successful_sources(outcomes, states):
    enabled_ids = {source.id for source in ALL_SOURCES}
    return {outcome.source.name for outcome in outcomes if outcome.source.id in enabled_ids and outcome.ok
            and not outcome.stopped_now and states[outcome.source.id].crawl_status != "已停抓"}


def _coverage_note(notes, name, detail):
    if name not in notes:
        notes[name] = detail
    elif detail not in notes[name].split("；"):
        notes[name] += "；" + detail


def _ts_notice(conn, today, push):
    if not push:
        return None
    row = conn.execute("SELECT * FROM source WHERE id='ts_web' AND crawl_status='已停抓'").fetchone()
    if row is not None and row["stop_notice_date"] in (None, "", today.isoformat()):
        return row["stop_reason"] or "封禁"
    return None


def _last_summary(conn, day):
    for row in conn.execute("SELECT date, event_ids_json FROM digest WHERE push=1 AND date<? ORDER BY date DESC", (day.isoformat(),)):
        ids = json.loads(row["event_ids_json"])
        companies = set()
        for eid in ids:
            event = conn.execute("SELECT company_id, filter_reasons_json,original_first_ingested_at,"
                                 "EXISTS(SELECT 1 FROM meta WHERE key='backfill:normal:'||funding_event.id) AS ordinary "
                                 "FROM funding_event WHERE id=?", (eid,)).fetchone()
            if event and (not event["original_first_ingested_at"] or event["ordinary"]) and not json.loads(event["filter_reasons_json"]) and conn.execute(
                    "SELECT status FROM company WHERE id=?", (event["company_id"],)).fetchone()[0] == "已纳入":
                companies.add(event["company_id"])
        if companies:
            return date.fromisoformat(row["date"]), len(companies)
    return None


def _new_evidence_count(conn, window, end):
    start = window or end - timedelta(days=7)
    companies = set()
    for row in conn.execute("SELECT company_id, new_evidence_found_at, new_evidence_json FROM deepdive "
                            "WHERE new_evidence_found_at IS NOT NULL"):
        if start < _stamp(row["new_evidence_found_at"]) <= end and json.loads(row["new_evidence_json"]):
            companies.add(row["company_id"])
    return len(companies)


def _after_handoff(conn, cfg, args, clock, today, source_bodies):
    """交接后生成本地站点并调度深度，所有异常仅记录日志。"""
    log = _RunLog(cfg.log_dir / f"{today}.log")

    def record(event):
        try:
            log.write(event)
        except Exception:
            pass

    def build(conn, cfg, clock, company_id=None, run_date=None, *, slot=None):
        try:
            online = clock.now()
            conn.execute("UPDATE deepdive SET online_at=? WHERE status='完成' AND online_at IS NULL", (online.isoformat(),))
            site.build_site(conn, cfg, now=online, snapshot_slot=slot)
            conn.commit()
            record({"event": "site_built", "at": online.isoformat(), "company_id": company_id})
            return online
        except Exception:
            conn.rollback()
            record({"event": "site_build_failed", "at": clock.now().isoformat(), "company_id": company_id})
            return None

    try:
        from . import deep, site
        build(conn, cfg, clock)
        if args.no_deep or not cal.is_cn_workday(today):
            return
        def checkpoint(conn, cfg, clock):
            slot = "1030" if clock.now().time() >= args.deep_final else "0930"
            return build(conn, cfg, clock, slot=slot)
        deep.run_for_today(conn, cfg, clock, fixture=args.fixture, source_bodies=source_bodies,
                           publish=build, checkpoint=checkpoint,
                           deep_deadline=datetime.combine(today, args.deep_deadline, tzinfo=BJ),
                           deep_final=datetime.combine(today, args.deep_final, tzinfo=BJ))
    except Exception as exc:
        conn.rollback()
        record({"event": "deep_failed", "type": type(exc).__name__, "at": clock.now().isoformat()})
    finally:
        if not cfg.test_mode:
            try:
                db.backup(conn, cfg.state_dir / "backup" / f"vc-radar-{today:%Y%m%d}.db")
            except Exception:
                record({"event": "deep_backup_failed", "at": clock.now().isoformat()})


def run(cfg: Config, args, clock) -> int:
    """执行一次运行；守卫拒绝返回 3，首期失败整体回滚并返回 1。"""
    cfg = replace(cfg, test_mode=config.is_test_run(args, cfg),
                  base_url="" if args.no_links else cfg.base_url,
                  review_site_passed=False if args.no_links else cfg.review_site_passed,
                  total_timeout_s=args.total_timeout if args.total_timeout is not None else cfg.total_timeout_s)
    cfg.links_file = config.effective_links_file(cfg)
    if args.prod_rules and args.first_issue:
        print("--prod-rules 不能与 --first-issue 同用", file=sys.stderr)
        return 2
    clock = _ClockView(clock, args.date is not None or args.now is not None)
    today = args.date if cfg.test_mode and args.date is not None else clock.now().date()
    if args.deep_only:
        if cfg.db_path.exists():
            with db.connect(cfg.db_path) as conn:
                _record_ts_notice(conn, cfg, today)
        from .deep import run_cli
        result = run_cli(cfg, args, clock)
        if cfg.db_path.exists():
            from .site import write_status
            with db.connect(cfg.db_path) as conn:
                write_status(conn, cfg, clock.now())
        return result
    if message := preflight(cfg, args, today):
        if _stale_paths(cfg):
            with run_lock(cfg):
                cleanup_stale(cfg)
        print(message, file=sys.stderr)
        return 3
    with run_lock(cfg) as main_lock:
        cleanup_stale(cfg)
        if args.reset_test_db:
            reset_test_db(cfg)
            if not (args.fixture or args.dry_run or args.to or args.first_issue or args.sync_sent):
                print("测试库、测试 raw、测试缓存和 outbox-test 已重置")
                return 0
        if message := preflight(cfg, args, today):
            print(message, file=sys.stderr)
            return 3
        if args.first_issue:
            return _first_issue(cfg, args, clock, today)
        conn = db.connect(cfg.db_path)
        try:
            _record_ts_notice(conn, cfg, today)
            conn.commit()
            if args.sync_sent:
                sync_sent(conn, cfg, today)
                conn.commit()
                from .site import write_status
                write_status(conn, cfg, clock.now())
                if not cfg.test_mode:
                    db.backup(conn, cfg.state_dir / "backup" / f"vc-radar-{today:%Y%m%d}.db")
                return 0
            if args.resend:
                try:
                    return _resend(conn, cfg, args, clock, today)
                except (OSError, ValueError) as exc:
                    conn.rollback()
                    print(f"重发失败（{type(exc).__name__}）", file=sys.stderr)
                    return 1
            try:
                result = _run_connected(conn, cfg, args, clock, today)
                if isinstance(result, _RunResult):
                    # 深度期间让 09:10 的 --sync-sent 能获取主流程锁；公司任务另由库中 claim 防重。
                    fcntl.flock(main_lock.fileno(), fcntl.LOCK_UN)
                    _after_handoff(conn, cfg, args, clock, today, result.source_bodies or {})
                    print(result.path)
                    return 0
                return result
            except (_FictionalData, _PlaceholderData) as exc:
                conn.rollback()
                print(f"运行失败：{exc}", file=sys.stderr)
                return 1
            except Exception as exc:
                conn.rollback()
                try:
                    _failure_file(conn, cfg, args, clock, today)
                except (_FictionalData, _PlaceholderData) as delivery_error:
                    print(f"运行失败：{delivery_error}", file=sys.stderr)
                    return 1
                print(f"运行失败（{type(exc).__name__}）", file=sys.stderr)
                return 1
        finally:
            conn.close()


def _run_connected(conn, cfg, args, clock, today):
    from .placenames import place_audit
    log = _RunLog(cfg.log_dir / f"{today}.log")
    with scan.country_log(log.write), place_audit(log.write):
        return _run_connected_with_log(conn, cfg, args, clock, today)


def _issue_window(conn, cfg, today, existing=None):
    window = outbox.last_push_generated_at(cfg.outbox_dir, today)
    if cfg.test_mode:
        previous = conn.execute("SELECT generated_at FROM digest WHERE push=1 AND date<? AND generated_at IS NOT NULL ORDER BY date DESC LIMIT 1", (str(today),)).fetchone()
        if previous:
            window = _stamp(previous[0])
    if existing and existing["window_start"]:
        window = _stamp(existing["window_start"])
    return window


def _run_connected_with_log(conn, cfg, args, clock, today):
    today = args.date if cfg.test_mode and args.date is not None else today
    _record_ts_notice(conn, cfg, today)
    log = _RunLog(cfg.log_dir / f"{today}.log")
    try:
        workday = cal.is_cn_workday(today)
        scheduled = cal.is_push_day(today, cfg.push_mode, cfg.push_weekday, cfg.holiday_shift)
    except cal.CalendarMissing:
        log.write({"event": "environment_missing", "reason": "缺少日历", "date": str(today)})
        validate_delivery(conn, "", test_mode=cfg.test_mode)
        path = outbox.write_main(cfg.outbox_dir, today, push=False, alarm=True, generated_at=clock.now(), text="", test=cfg.test_mode, real_now=datetime.now(BJ))
        from .site import write_status
        write_status(conn, cfg, clock.now(), runtime={"date": str(today), "status": "环境缺失", "push": False,
            "alarm": True, "outbox": str(path), "simulated": bool(clock.simulated)})
        return 5
    first_file = _first_file(cfg, today)
    anchor = first_file if args.first_issue or (args.resend and first_file is not None) else _main_file(cfg, today)
    original = json.loads(anchor.read_text()) if anchor is not None else None
    push = args.first_issue or (workday and scheduled and first_file is None)
    existing = conn.execute("SELECT * FROM digest WHERE date=?", (today.isoformat(),)).fetchone()
    window = _issue_window(conn, cfg, today, existing)
    budget = _Budget(cfg, clock, scheduled=not args.first_issue and not (
        cfg.test_mode and args.now is not None and args.now >= daytime(8, 50)))
    configured_sources = ALL_SOURCES if (cfg.enable_extended_sources or getattr(args, "extended_sources", False)) else P0_SOURCES
    db.sync_sources(conn, configured_sources)
    initial = db.is_empty(conn)
    states = db.load_source_states(conn)
    sources = [source for source in configured_sources if conn.execute("SELECT enabled FROM source WHERE id=?", (source.id,)).fetchone()[0]]
    outcomes, entries = [], []
    incomplete = False
    coverage_key = f"pipe:first_incomplete:{scan.baseline_cutoff(today)}"
    coverage = conn.execute("SELECT value FROM meta WHERE key=?", (coverage_key,)).fetchone()
    incomplete_until = date.fromisoformat(coverage[0]) if coverage else None
    session = None
    incomplete_sources = {}
    fx_rates = None
    if workday or args.first_issue:
        if initial and "yc_launches" in states:
            states["yc_launches"].last_ok = False
        client = http.FixtureTransport(args.fixture) if args.fixture else http.UrllibTransport()
        transport = _Transport(client, budget, cfg, log)
        cache = cfg.state_dir / ("test-cache" if cfg.test_mode else "cache")
        robots = RobotsChecker(transport, cache, today)

        def pause(seconds):
            if args.fixture:
                clock.advance(seconds)
            else:
                time.sleep(min(seconds, budget.remaining()))

        try:
            outcomes = fetcher.fetch_all(sources, states, transport, robots, clock.now(), cal.us_workdays_between, pause,
                                         deadline_monotonic=time.monotonic() + budget.remaining())
            first_seen = clock.now()
            run_id = uuid4().hex
            source_map = {source.id: source for source in sources}
            for outcome in outcomes:
                if initial:
                    for item in outcome.items:
                        item.structured.pop("backfill", None)
                    for record in outcome.runs:
                        record.is_backfill = False
                    if outcome.source.id == "yc_launches" and states[outcome.source.id].known_external_ids == set():
                        outcome.missed_range = ""
                        outcome.missed_start = outcome.missed_end = None
                        for record in outcome.runs:
                            record.missed_range = ""
                            record.missed_start = record.missed_end = None
                known_latest = states[outcome.source.id].last_latest_at
                if known_latest and "上次已知最新时间（状态未提供）" in outcome.missed_range:
                    outcome.missed_range = outcome.missed_range.replace("上次已知最新时间（状态未提供）", known_latest.strftime("%Y-%m-%d %H:%M"))
                    for record in outcome.runs:
                        if record.missed_range:
                            record.missed_range = outcome.missed_range
                scan.record_fetch(conn, run_id, outcome, bool(clock.simulated))
                dates = [item.published_at.astimezone(BJ).date() for item in outcome.items if item.published_at]
                if initial and dates and min(dates) > scan.baseline_cutoff(today):
                    uncovered = min(today, min(dates) - timedelta(days=1))
                    incomplete_until = max(incomplete_until or uncovered, uncovered)
                    _coverage_note(incomplete_sources, outcome.source.name, f"{scan.baseline_cutoff(today)} 至 {uncovered} 部分未能补全")
                if args.first_issue and (not outcome.ok or not outcome.runs or outcome.stopped_now):
                    _coverage_note(incomplete_sources, outcome.source.name, f"{scan.baseline_cutoff(today)} 至 {today}：{outcome.runs[-1].error_summary if outcome.runs else '已停抓'}")
                for item in outcome.items:
                    if budget.remaining() <= 0:
                        incomplete = True
                        break
                    baseline = scan.is_baseline_item(item, today, initial)
                    item_id, new = scan.upsert_raw_item(conn, item, first_seen, baseline)
                    retry = item_id and conn.execute("SELECT skip_reason FROM raw_item WHERE id=?", (item_id,)).fetchone()[0] == "这次没轮到抽取"
                    if not (new or retry):
                        continue
                    if source_map[item.source_id].kind == "rss" and not _FUNDING.search(item.title + " " + item.excerpt):
                        conn.execute("UPDATE raw_item SET skip_reason='不是融资报道' WHERE id=?", (item_id,))
                        continue
                    if retry:
                        baseline = bool(conn.execute("SELECT is_baseline FROM raw_item WHERE id=?", (item_id,)).fetchone()[0])
                    entries.append((item_id, item, baseline))
            _article_bodies(entries, source_map, transport, robots, cfg, clock, budget)
        finally:
            transport.close()
        batches = canonical_latest_batches(
            (str(item.structured["yc_batch"]) for outcome in outcomes for item in outcome.items if item.structured.get("yc_batch")),
            as_of=today,
        )
        raw_client = (_MissingModel(cfg.model) if cfg.test_mode and not args.fixture else
                      model_client(cfg, args.fixture, len(entries)))
        if isinstance(raw_client, _MissingModel):
            log.write({"event": "model_unavailable", "reason": "缺少模型密钥"})
        session = ModelSession(raw_client, cfg, clock, budget, log)
        fx_transport = _Transport(client, budget, cfg, log)
        fx_rates = fx.rates_for_run(cfg, args, fetcher=fx_transport, now=clock.now(), log=log.write)
        try:
            fx_rates.refresh()
            incomplete = ingest_items(conn, entries, cfg=cfg, clock=clock, budget=budget, client=session,
                                      batches=batches, fx_rates=fx_rates) or incomplete
            scan.retry_unconverted_amounts(conn, fx_rates)
            # 查证客户端沿用抽取模型，但不经过抽取用量账本。
            from .verify import run_verification, services_for
            changed_ids = [row[0] for item_id, _, baseline in entries if not baseline for row in conn.execute(
                "SELECT e.company_id FROM event_source s JOIN funding_event e ON e.id=s.event_id "
                "WHERE s.raw_item_id=? AND e.baseline_unpushed=0 AND e.filter_reasons_json='[]'", (item_id,))]
            try:
                log.write({"event": "verification_start", "at": clock.now().isoformat()})
                verification_results = run_verification(conn, cfg, clock, company_ids=changed_ids,
                    services=services_for(cfg, clock, fixtures=getattr(args, "search_fixtures", None),
                                          model_client=raw_client, transport=client),
                    parent_budget=budget, first_issue=args.first_issue,
                    no_verify=getattr(args, "no_verify", False), rates=fx_rates)
                if any(str(s.get("status")) in {"401", "402"} for result in verification_results.values() for s in result["searches"]):
                    log.write({"event": "verify_auth_stopped", "date": str(today), "reason": "key 或余额问题（401 / 402）"})
            except Exception:
                # 查证的异常不参与信源成功数或推送报警。
                log.write({"event": "verification_failed", "at": clock.now().isoformat()})
            finally:
                log.write({"event": "verification_end", "at": clock.now().isoformat()})
            try:
                from .homepage import run_homepage
                run_homepage(conn, cfg, clock, company_ids=changed_ids, source_bodies=transport.pages,
                             fixtures=getattr(args, "search_fixtures", None), parent_budget=budget, log=log.write)
            except Exception:
                log.write({"event": "homepage_failed", "at": clock.now().isoformat()})
        finally:
            fx_transport.close()
            session.close()
        incomplete = incomplete or budget.remaining() <= 0
    if initial and incomplete_until is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (coverage_key, incomplete_until.isoformat()))
    generated = (_stamp(original["generated_at"]) if args.resend and original and original.get("push") else clock.now())
    previous_ids = json.loads(existing["event_ids_json"]) if existing and existing["push"] else []
    sent = (anchor is not None and anchor.with_suffix(".sent").exists()) or (
        not cfg.test_mode and not args.first_issue and not args.resend and (cfg.outbox_dir / f"{today}.sent").exists())
    if cfg.test_mode:
        acknowledged = outbox.test_sent_file(cfg.outbox_dir, today)
        sent = acknowledged is not None
        if sent and anchor is None:
            anchor = _first_file(cfg, today) or acknowledged
            original = json.loads(anchor.read_text())
    frozen = previous_ids if sent or (args.resend and existing) else None
    listing = _listing_for_issue(conn, today, previous_ids, only_ids=frozen)
    lines = _source_lines(outcomes, states)
    status = _status(outcomes, lines, listing, incomplete) if workday or args.first_issue else "非推送日"
    if (push or args.resend) and not args.first_issue:
        lines = _with_missed_history(conn, lines, window, generated)
    if args.first_issue and (status == "没跑完" or not _successful_sources(outcomes, states)):
        raise RuntimeError("首期没跑完或成功信源数为 0")
    if args.first_issue:
        status = "基线"
    failed_days = scan.failed_days_in_window(conn, window, today, outbox_dir=cfg.outbox_dir) if push or args.resend else 0
    baseline_count = scan.baseline_company_count(conn) if args.first_issue and push else 0
    from .backfill import collect_supplement, mark_supplement
    previous_supplement = json.loads(existing["supplement_event_ids_json"]) if existing and existing["push"] else []
    sections, supplement_ids = collect_supplement(conn, cfg, today,
        event_ids=previous_supplement or ([] if sent else None)) if push else ([], [])
    data = DigestData(today, window, generated, status, resend=args.resend, baseline_count=baseline_count,
                      cards=listing["cards"], round_pending=listing["round_pending"],
                      filtered=listing["filtered"], yc_new_batch=listing["yc_new_batch"], sources=lines,
                      ts_stopped_notice=_ts_notice(conn, today, args.resend or (push and not sent)),
                      new_evidence_count=_new_evidence_count(conn, window, generated),
                      last_push_summary=_last_summary(conn, today),
                      base_url=render.link_base_url(cfg.base_url, cfg.link_path_template, today),
                      review_site_passed=cfg.review_site_passed, extra_sections=sections)
    data.baseline_at = db.first_issue_generated_at(conn)
    text = render.render_push(data, early_push=cal.is_early_push(today), failed_days=failed_days,
                              incomplete_until=incomplete_until if args.first_issue else None,
                              since_last_push=cal.since_last_push(window.date() if window else None, today),
                              filtered_count=listing["filtered_total"], first_seen=listing["first_seen"],
                              links_file=cfg.links_file) if push or args.resend else ""
    if sent and not args.resend and original:
        text = original["text"]
        generated = _stamp(original["generated_at"])
    if args.first_issue and budget.remaining() <= 0:
        raise RuntimeError("首期总超时")
    validate_delivery(conn, text, test_mode=cfg.test_mode)
    alarm = render.is_alarm(data, failed_days=failed_days, push=push or args.resend)
    kwargs = dict(alarm=alarm, generated_at=generated, text=text, test=cfg.test_mode, real_now=datetime.now(BJ))
    if sent and not args.resend and anchor is not None:
        path = anchor
    elif args.first_issue and push:
        path = outbox.write_first(cfg.outbox_dir, today, **kwargs)
    elif args.resend:
        path = outbox.write_resend(cfg.outbox_dir, today, **kwargs)
    else:
        path = outbox.write_main(cfg.outbox_dir, today, push=push, **kwargs)
    if push or args.resend:
        if not sent and not args.resend:
            from .homepage import record_digest_snapshot
            record_digest_snapshot(conn, cfg, today, listing["cards"], generated)
        scan.mark_listed(conn, listing["event_ids"], today)
        if not args.resend:
            mark_supplement(conn, supplement_ids, today)
    # 首期替代当日定时推送，日期主键的 digest 保留首期记录。
    notes = "；".join(f"{name}：{detail}" for name, detail in incomplete_sources.items())
    if (first_file is None or args.first_issue) and not (args.resend and existing):
        counts = _digest_counts(data, listing, push=bool(push or args.resend), failed_days=failed_days,
                                first_issue=args.first_issue, incomplete_until=incomplete_until)
        if sent and existing:
            counts = json.loads(existing["counts_json"])
        log.write({"event": "digest_counts", "date": str(today), "counts": counts})
        digest_row = {"date": today, "push": bool(push or args.resend), "status": status,
                                  "counts_json": counts,
                                  "card_sources_json": existing["card_sources_json"] if sent and existing else
                                  _card_source_snapshot(conn, listing["event_ids"] + supplement_ids) if push or args.resend else {},
                                  "error_sources": [line.name for line in lines if not line.ok and not line.stopped],
                                  "event_ids": listing["event_ids"] if push or args.resend else [], "text": text,
                                  "supplement_event_ids_json": supplement_ids,
                                  "send_target": "test" if cfg.test_mode else "ownerx", "generated_at": generated,
                                  "simulated": bool(clock.simulated), "window_start": window}
        if args.first_issue:
            digest_row["note"] = notes
        scan.upsert_digest(conn, digest_row, first_issue=args.first_issue)
    if sent or args.resend:
        sync_sent(conn, cfg, today)
    conn.commit()
    _cleanup_raw(cfg.raw_dir, today)
    if not cfg.test_mode and args.first_issue:
        db.backup(conn, cfg.state_dir / "backup" / f"vc-radar-{today:%Y%m%d}.db")
    from .site import write_status
    write_status(conn, cfg, clock.now(), runtime={"date": str(today), "status": status, "push": bool(push or args.resend),
        "alarm": alarm, "outbox": str(path), "simulated": bool(clock.simulated),
        "fx": {**fx_rates.status, "currencies": sorted({c for entry in fx_rates.cache["days"].values() for c in entry["rates"]})}
        if fx_rates else fx.status_from_files(cfg)})
    log.write({"event": "run_end", "status": status, "push": bool(push or args.resend), "simulated": bool(clock.simulated)})
    if args.first_issue and budget.remaining() <= 0:
        raise RuntimeError("首期总超时")
    digest = dict(conn.execute("SELECT * FROM digest WHERE date=?", (str(today),)).fetchone())
    baseline_items = conn.execute("SELECT COUNT(*) FROM raw_item WHERE is_baseline=1").fetchone()[0]
    return _RunResult(path, data, listing, incomplete_sources, notes, baseline_items, digest,
                      {item.url: item.body for _, item, _ in entries if item.body})


def _failure_file(conn, cfg, args, clock, today):
    today = args.date if cfg.test_mode and args.date is not None else today
    _record_ts_notice(conn, cfg, today)
    log = _RunLog(cfg.log_dir / f"{today}.log")
    log.write({"event": "run_error", "date": str(today)})
    push = args.first_issue or args.resend or cal.is_push_day(today)
    existing = conn.execute("SELECT * FROM digest WHERE date=?", (str(today),)).fetchone()
    anchor = _first_file(cfg, today) if args.first_issue else _main_file(cfg, today)
    sent = (anchor is not None and anchor.with_suffix(".sent").exists()) or (
        not cfg.test_mode and (cfg.outbox_dir / f"{today}.sent").exists())
    if cfg.test_mode:
        acknowledged = outbox.test_sent_file(cfg.outbox_dir, today)
        sent = acknowledged is not None
        if sent and anchor is None:
            anchor = _first_file(cfg, today) or acknowledged
    previous_ids = json.loads(existing["event_ids_json"]) if existing and existing["push"] else []
    frozen = previous_ids if sent or (args.resend and existing) else None
    listing = _listing_for_issue(conn, today, previous_ids, only_ids=frozen)
    window = _issue_window(conn, cfg, today, existing)
    failed_days = scan.failed_days_in_window(conn, window, today, outbox_dir=cfg.outbox_dir) if push else 0
    from .backfill import collect_supplement, mark_supplement
    previous_supplement = json.loads(existing["supplement_event_ids_json"]) if existing and existing["push"] else []
    sections, supplement_ids = collect_supplement(conn, cfg, today,
        event_ids=previous_supplement or ([] if sent or args.resend and existing else None)) if push else ([], [])
    digest = DigestData(today, window, clock.now(), "全部失败", cards=listing["cards"],
                        round_pending=listing["round_pending"],
                        filtered=listing["filtered"],
                        baseline_count=scan.baseline_company_count(conn) if args.first_issue else 0,
                        extra_sections=sections,
                        base_url=render.link_base_url(cfg.base_url, cfg.link_path_template, today),
                        review_site_passed=cfg.review_site_passed)
    if push and not args.first_issue:
        digest.sources = _with_missed_history(conn, [], window, digest.window_end)
    digest.baseline_at = db.first_issue_generated_at(conn)
    digest.ts_stopped_notice = _ts_notice(conn, today, push)
    text = render.render_push(digest, filtered_count=listing["filtered_total"], first_seen=listing["first_seen"],
                              links_file=cfg.links_file) if push else ""
    if sent and not args.resend and anchor is not None:
        original = json.loads(anchor.read_text())
        text = original["text"]
        digest.window_end = _stamp(original["generated_at"])
    validate_delivery(conn, text, test_mode=cfg.test_mode)
    kwargs = dict(alarm=True, generated_at=digest.window_end, text=render.add_resend_notice(text) if args.resend else text,
                  test=cfg.test_mode, real_now=datetime.now(BJ))
    if sent and not args.resend and anchor is not None:
        path = anchor
    elif args.first_issue:
        path = outbox.write_first(cfg.outbox_dir, today, **kwargs)
    elif args.resend:
        path = outbox.write_resend(cfg.outbox_dir, today, **kwargs)
    else:
        path = outbox.write_main(cfg.outbox_dir, today, push=push, **kwargs)
    counts = _digest_counts(digest, listing, push=push, failed_days=failed_days, first_issue=args.first_issue)
    if (sent or args.resend) and existing:
        counts = json.loads(existing["counts_json"])
    if not (args.resend and existing):
        if push:
            scan.mark_listed(conn, listing["event_ids"], today)
            mark_supplement(conn, supplement_ids, today)
        scan.upsert_digest(conn, {"date": today, "push": push, "status": "全部失败", "text": kwargs["text"],
                                  "event_ids": listing["event_ids"] if push else [], "generated_at": digest.window_end,
                                  "supplement_event_ids_json": supplement_ids,
                                  "counts_json": counts,
                                  "card_sources_json": existing["card_sources_json"] if sent and existing else
                                  _card_source_snapshot(conn, listing["event_ids"] + supplement_ids) if push else {},
                                  "window_start": window, "simulated": clock.simulated})
    conn.commit()
    log.write({"event": "digest_counts", "date": str(today), "counts": counts})
    from .site import write_status
    write_status(conn, cfg, clock.now(), runtime={"date": str(today), "status": "全部失败", "push": bool(push),
        "alarm": True, "outbox": str(path), "simulated": bool(clock.simulated), "fx": fx.status_from_files(cfg)})
    print(path)


def _digest_counts(data, listing, *, push, failed_days=0, first_issue=False, incomplete_until=None):
    if not push:
        return {}
    supplement = next((s for s in data.extra_sections if s.key == "backfill" and s.cards), None)
    return {"admitted": len(data.cards), "ai": sum("AI相关" in c.tag_label for c in data.cards),
            "social": sum("社交/社群相关" in c.tag_label for c in data.cards),
            "new_round": sum("新一轮" in c.marks for c in data.cards), "large": sum("大额" in c.marks for c in data.cards),
            "round_pending": len(data.round_pending), "filtered": listing["filtered_total"],
            "baseline": data.baseline_count if first_issue else None,
            "supplement_m": min(10, len(supplement.cards)) if supplement else None,
            "supplement_k": (supplement.overflow_count or max(0, len(supplement.cards) - 10)) if supplement else None,
            "new_evidence": data.new_evidence_count, "failed_days": failed_days,
            "incomplete_until": str(incomplete_until) if first_issue and incomplete_until else None}


def _card_source_snapshot(conn, event_ids):
    from .site import _sources
    sources = _sources(conn)
    return {str(eid): [{key: s[key] for key in ("name", "url", "published_at")}
                      for s in sources.get(eid, [])] for eid in event_ids}


def preview_push(cfg: Config, args, clock) -> int:
    """只读测试库重渲染推送，写预览文件，不改 digest 或补充推送日期。"""
    from .backfill import collect_supplement, memory_copy
    from .models import ErrorType, SourceStatusLine
    if not cfg.test_mode:
        print("--preview-push 只在测试模式可用", file=sys.stderr)
        return 2
    today, generated = clock.now().date(), clock.now()
    with run_lock(cfg), _read_database(cfg.db_path) as original:
        conn = memory_copy(original)
        try:
            existing = conn.execute("SELECT * FROM digest WHERE push=1 AND date=?", (str(today),)).fetchone()
            window = outbox.last_push_generated_at(cfg.outbox_dir, today)
            if window is None:
                previous = conn.execute("SELECT generated_at FROM digest WHERE push=1 AND date<? ORDER BY date DESC LIMIT 1", (str(today),)).fetchone()
                window = _stamp(previous[0]) if previous and previous[0] else None
            if existing:
                ids = json.loads(existing["event_ids_json"])
                window = _stamp(existing["window_start"]) if existing["window_start"] else window
            else:
                ids = [row["id"] for row in conn.execute("SELECT * FROM funding_event WHERE pending=1 AND baseline_unpushed=0")
                       if (window is None or _stamp(row["first_ingested_at"] or row["first_seen"]) > window)
                       and _stamp(row["first_ingested_at"] or row["first_seen"]) <= generated]
            listing = _listing_for_issue(conn, today, ids, only_ids=ids)
            saved_supplement = json.loads(existing["supplement_event_ids_json"]) if existing else []
            sections, _ = collect_supplement(conn, cfg, today, event_ids=saved_supplement or None)
            sources = []
            for source in conn.execute("SELECT * FROM source WHERE enabled=1 ORDER BY id"):
                latest = conn.execute("SELECT * FROM fetch_run WHERE source_id=? ORDER BY ended_at DESC,id DESC LIMIT 1", (source["id"],)).fetchone()
                error = ErrorType(latest["error_type"]) if latest and latest["error_type"] else None
                sources.append(SourceStatusLine(source["name"], bool(latest and latest["result"] == "成功"),
                    source["crawl_status"] == "已停抓", f"{latest['count']} 条" if latest else "", error,
                    _stamp(latest["latest_at"]).date() if latest and latest["latest_at"] else None,
                    latest["missed_range"] if latest else "",
                    _stamp(latest["missed_start"]) if latest and latest["missed_start"] else None,
                    _stamp(latest["missed_end"]) if latest and latest["missed_end"] else None))
            status = existing["status"] if existing else "有新增" if listing["cards"] else "没有新增"
            data = DigestData(today, window, generated, status, cards=listing["cards"],
                round_pending=listing["round_pending"], filtered=listing["filtered"],
                yc_new_batch=listing["yc_new_batch"], sources=sources, extra_sections=sections,
                last_push_summary=_last_summary(conn, today), new_evidence_count=_new_evidence_count(conn, window, generated),
                base_url=render.link_base_url("" if args.no_links else cfg.base_url, cfg.link_path_template, today),
                review_site_passed=cfg.review_site_passed and not args.no_links)
            data.baseline_at = db.first_issue_generated_at(conn)
            failed_days = scan.failed_days_in_window(conn, window, today, outbox_dir=cfg.outbox_dir)
            text = render.render_push(data, early_push=cal.is_early_push(today), failed_days=failed_days,
                since_last_push=cal.since_last_push(window.date() if window else None, today),
                filtered_count=listing["filtered_total"], first_seen=listing["first_seen"], links_file=cfg.links_file)
            validate_delivery(conn, text, test_mode=True)
            path = cfg.outbox_dir / f"{today}-preview-{datetime.now(BJ):%H%M%S}.json"
            outbox.write_atomic(path, {"date": str(today), "push": True, "alarm": render.is_alarm(data, failed_days=failed_days),
                                      "generated_at": generated.strftime("%Y-%m-%d %H:%M:%S"), "text": text})
            print(text)
        finally:
            conn.close()
    return 0
