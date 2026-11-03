"""交接文件（任务 B）。PRD 第 10 节：{"date","push","alarm","generated_at","text"}，临时文件 + rename 原子写入。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import BJ


def write_atomic(path: Path, obj: dict) -> None:
    """在目标目录写临时文件并 fsync 后原子替换，异常时清掉临时文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(obj, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _beijing(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return dt.astimezone(BJ)


def _payload(d: date, push: bool, alarm: bool, generated_at: datetime, text: str) -> dict:
    return {"date": d.isoformat(), "push": bool(push), "alarm": bool(alarm),
            "generated_at": _beijing(generated_at).strftime("%Y-%m-%d %H:%M:%S"), "text": text}


def main_path(outbox_dir: Path, d: date) -> Path:
    """outbox/<日期>.json"""
    return Path(outbox_dir) / f"{d.isoformat()}.json"


def first_path(outbox_dir: Path, d: date) -> Path:
    """首期主文件 outbox/<北京日期>-first.json。"""
    return Path(outbox_dir) / f"{d.isoformat()}-first.json"


def write_first(outbox_dir: Path, d: date, *, alarm: bool, generated_at: datetime, text: str,
                test: bool, real_now: datetime) -> Path:
    """原子写首期 push=true 文件；测试加真实钟点，正式 -first.sent 存在时不覆盖。"""
    path = first_path(outbox_dir, d)
    if test:
        path = path.with_name(f"{d.isoformat()}-{_beijing(real_now):%H%M%S}-first.json")
    elif path.with_suffix(".sent").exists():
        return path
    write_atomic(path, _payload(d, True, alarm, generated_at, text))
    return path


def _has_first_issue(outbox_dir: Path, d: date, test: bool) -> bool:
    paths = [first_path(outbox_dir, d)] if not test else Path(outbox_dir).glob(f"{d}-*-first.json")
    pattern = re.compile(re.escape(d.isoformat()) + r"(?:-\d{6})?-first\.json")
    return any(path.is_file() and pattern.fullmatch(path.name)
               and json.loads(path.read_text(encoding="utf-8")).get("push") is True for path in paths)


def test_sent_file(outbox_dir: Path, d: date) -> Path | None:
    """找出同日已有同主名回执的测试交接文件，包含首期和重发。"""
    pattern = re.compile(re.escape(str(d)) + r"-(?:\d{6}(?:-first)?|(?:first-)?resend-[1-9]\d*(?:-\d{6})?)\.json")
    paths = [path for path in Path(outbox_dir).glob(f"{d}-*.json")
             if pattern.fullmatch(path.name) and path.is_file() and path.with_suffix(".sent").is_file()]
    return max(paths, key=lambda path: path.name, default=None)


def _new_test_main_path(outbox_dir: Path, d: date, real_now: datetime) -> Path:
    clock = _beijing(real_now)
    pattern = re.compile(re.escape(str(d)) + r"-(\d{6})\.json")
    for path in Path(outbox_dir).glob(f"{d}-*.json"):
        match = pattern.fullmatch(path.name)
        if match and path.is_file():
            try:
                stamp = datetime.strptime(match[1], "%H%M%S")
            except ValueError:
                continue
            # 同秒重跑或测试钟点回退时，仍给新文件一个更大的、不覆盖的钟点。
            seen = clock.replace(hour=stamp.hour, minute=stamp.minute, second=stamp.second, microsecond=0)
            if seen >= clock.replace(microsecond=0):
                following = seen + timedelta(seconds=1)
                if following.date() != clock.date():
                    raise FileExistsError("当天测试交接文件的 HHMMSS 已用尽")
                clock = following
    return Path(outbox_dir) / f"{d}-{clock:%H%M%S}.json"


def write_main(outbox_dir: Path, d: date, *, push: bool, alarm: bool, generated_at: datetime, text: str,
               test: bool, real_now: datetime) -> Path:
    """正式：outbox/<日期>.json；当天 .sent 已存在时不覆盖，返回已有路径并不写（防重复推送）。
    测试：任一同日交接文件已有回执就不写；否则新建不同 HHMMSS 的文件。"""
    if test:
        if sent := test_sent_file(outbox_dir, d):
            return sent
        path = _new_test_main_path(outbox_dir, d, real_now)
    else:
        path = main_path(outbox_dir, d)
        if path.with_suffix(".sent").exists():
            return path
    if push and _has_first_issue(outbox_dir, d, test):
        push, text = False, ""
    write_atomic(path, _payload(d, push, alarm, generated_at, text))
    return path


def write_resend(outbox_dir: Path, d: date, *, alarm: bool, generated_at: datetime, text: str,
                 test: bool, real_now: datetime) -> Path:
    """正式 outbox/<日期>-resend-<N>.json；测试 outbox-test/<日期>-resend-<N>-<HHMMSS>.json；
    N = 当天已有 resend 文件数 + 1；调用方将「重发」放入首行区的提示行。"""
    outbox_dir = Path(outbox_dir)
    clock = _beijing(real_now)
    file_date = d
    pattern = re.compile(re.escape(file_date.isoformat()) + r"-resend-[1-9]\d*"
                         + (r"-\d{6}" if test else "") + r"\.json")
    number = 1 + sum(pattern.fullmatch(path.name) is not None and path.is_file()
                     for path in outbox_dir.glob(f"{file_date}-resend-*.json"))
    suffix = f"-{clock:%H%M%S}" if test else ""
    path = outbox_dir / f"{file_date}-resend-{number}{suffix}.json"
    if path.exists():
        raise FileExistsError(f"重发文件编号不连续，不能覆盖：{path.name}")
    write_atomic(path, _payload(d, True, alarm, generated_at, text))
    return path


def write_first_resend(outbox_dir: Path, d: date, *, alarm: bool, generated_at: datetime, text: str,
                       test: bool, real_now: datetime | None = None) -> Path:
    """写 <日期>-first-resend-N.json；测试同名写调用方的 outbox-test，N=已有首期重发文件数+1。"""
    outbox_dir = Path(outbox_dir)
    pattern = re.compile(re.escape(d.isoformat()) + r"-first-resend-[1-9]\d*\.json")
    number = 1 + sum(path.is_file() and pattern.fullmatch(path.name) is not None
                     for path in outbox_dir.glob(f"{d}-first-resend-*.json"))
    path = outbox_dir / f"{d}-first-resend-{number}.json"
    if path.exists():
        raise FileExistsError(f"首期重发文件编号不连续，不能覆盖：{path.name}")
    write_atomic(path, _payload(d, True, alarm, generated_at, text))
    return path


def _read_receipt(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"发送回执必须是 JSON 对象：{path.name}")
    if value.get("sent_at"):
        stamp = datetime.fromisoformat(value["sent_at"])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=BJ)
        value["sent_at"] = _beijing(stamp).strftime("%Y-%m-%d %H:%M:%S")
    return value


def read_sent(outbox_dir: Path, d: date) -> dict:
    """读 main、first、resends、first_resends 回执；两类重发各自按 N 递增。"""

    outbox_dir = Path(outbox_dir)
    main = main_path(outbox_dir, d).with_suffix(".sent")
    first = first_path(outbox_dir, d).with_suffix(".sent")
    pattern = re.compile(re.escape(d.isoformat()) + r"-(first-)?resend-([1-9]\d*)\.sent")
    resends, first_resends = {}, {}
    for path in outbox_dir.glob(f"{d}-*.sent"):
        match = pattern.fullmatch(path.name)
        if match:
            target = first_resends if match[1] else resends
            target[int(match[2])] = _read_receipt(path)
    return {"main": _read_receipt(main) if main.exists() else None,
            "first": _read_receipt(first) if first.exists() else None, "resends": dict(sorted(resends.items())),
            "first_resends": dict(sorted(first_resends.items()))}


def first_issue_date(outbox_dir: Path) -> date | None:
    """首个有效首期主文件的北京日期，兼容生产和测试命名；首期重发不算主文件。"""
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2})(?:-\d{6})?-first\.json")
    dates = []
    for path in Path(outbox_dir).glob("*-first.json"):
        match = pattern.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("push") is True:
            dates.append(date.fromisoformat(match[1]))
    return min(dates, default=None)


def last_push_generated_at(outbox_dir: Path, before: date) -> datetime | None:
    """早于 before 的最近一个普通/首期 push=true 主文件时刻，排除取件程序x兜底和重发。"""
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2})(?:-first)?\.json")
    candidates = []
    for path in Path(outbox_dir).glob("*.json"):
        match = pattern.fullmatch(path.name)
        if not match or date.fromisoformat(match[1]) >= before:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("push") is True:
            receipt = path.with_suffix(".sent")
            if receipt.exists() and "兜底" in str(_read_receipt(receipt).get("note", "")):
                continue
            stamp = datetime.fromisoformat(payload["generated_at"])
            candidates.append(stamp.replace(tzinfo=BJ) if stamp.tzinfo is None else stamp.astimezone(BJ))
    return max(candidates, default=None)
