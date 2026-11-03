import json
import os
from datetime import date, datetime, timezone

import pytest

from vcradar import outbox
from vcradar.config import BJ


DAY = date(2026, 10, 30)
SIMULATED = datetime(2026, 9, 30, 8, 55, 12, tzinfo=BJ)
REAL = datetime(2026, 10, 30, 21, 40, 31, tzinfo=BJ)


def write_main(directory, **overrides):
    kwargs = dict(push=True, alarm=False, generated_at=SIMULATED, text="合成推送", test=False, real_now=REAL)
    kwargs.update(overrides)
    return outbox.write_main(directory, DAY, **kwargs)


def write_resend(directory, **overrides):
    kwargs = dict(alarm=True, generated_at=SIMULATED, text="重发\n合成推送", test=False, real_now=REAL)
    kwargs.update(overrides)
    return outbox.write_resend(directory, DAY, **kwargs)


def test_atomic_replace_and_ordered_json(tmp_path, monkeypatch):
    actual_replace = os.replace
    replacements = []

    def check_replace(source, target):
        assert source.parent == target.parent
        assert source != target
        assert json.loads(source.read_text())["text"] == "合成推送"
        assert not target.exists()
        replacements.append((source, target))
        actual_replace(source, target)

    monkeypatch.setattr(outbox.os, "replace", check_replace)
    path = write_main(tmp_path / "outbox")
    assert path == tmp_path / "outbox" / "2026-10-30.json"
    payload = json.loads(path.read_text())
    assert list(payload) == ["date", "push", "alarm", "generated_at", "text"]
    assert payload["generated_at"] == "2026-09-30 08:55:12"
    assert len(replacements) == 1
    assert list(path.parent.iterdir()) == [path]


def test_atomic_failure_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "file.json"
    path.write_text("old")

    def fail(*args):
        raise OSError("合成磁盘故障")

    monkeypatch.setattr(outbox.os, "replace", fail)
    with pytest.raises(OSError):
        outbox.write_atomic(path, {"text": "new"})
    assert path.read_text() == "old"
    assert list(tmp_path.iterdir()) == [path]


def test_sent_guard_and_unsent_rerun(tmp_path):
    path = write_main(tmp_path)
    write_main(tmp_path, text="更新")
    assert json.loads(path.read_text())["text"] == "更新"
    path.with_suffix(".sent").write_text('{"result":"ok"}')
    before = path.read_bytes()
    assert write_main(tmp_path, text="不应覆盖") == path
    assert path.read_bytes() == before


def test_nonpush_and_timezone_format(tmp_path):
    stamp = datetime(2026, 10, 30, 0, 55, 15, tzinfo=timezone.utc)
    payload = json.loads(write_main(tmp_path, push=False, text="", generated_at=stamp).read_text())
    assert payload == {"date": "2026-10-30", "push": False, "alarm": False, "generated_at": "2026-10-30 08:55:15", "text": ""}


def test_test_filename_uses_supplied_date_real_clock_and_simulated_payload(tmp_path):
    path = outbox.write_main(tmp_path, date(2025, 1, 1), push=True, alarm=False,
                             generated_at=SIMULATED, text="合成", test=True, real_now=REAL)
    assert path.name == "2025-01-01-214031.json"
    assert json.loads(path.read_text())["date"] == "2025-01-01"
    assert json.loads(path.read_text())["generated_at"] == "2026-09-30 08:55:12"


def test_production_resend_counts_files_not_receipts_or_max_number(tmp_path):
    (tmp_path / "2026-10-30-resend-9.json").write_text("{}")
    (tmp_path / "2026-10-30-resend-1.sent").write_text("{}")
    (tmp_path / "2026-10-29-resend-1.json").write_text("{}")
    (tmp_path / "2026-10-30-resend-bad.json").write_text("{}")
    (tmp_path / "2026-10-30.json").write_text("{}")
    path = write_resend(tmp_path)
    assert path.name == "2026-10-30-resend-2.json"
    assert write_resend(tmp_path).name == "2026-10-30-resend-3.json"
    assert json.loads(path.read_text())["push"] is True
    assert json.loads(path.read_text())["text"].startswith("重发\n")


def test_test_resends_real_date_and_own_directory(tmp_path):
    directory = tmp_path / "outbox-test"
    one = write_resend(directory, test=True)
    two = write_resend(directory, test=True)
    assert one.name == "2026-10-30-resend-1-214031.json"
    assert two.name == "2026-10-30-resend-2-214031.json"
    assert not (tmp_path / "outbox").exists()


def test_read_sent_and_latest_numeric_resend(tmp_path):
    outbox.write_atomic(tmp_path / "2026-10-30.sent", {"sent_at": "2026-10-30 09:00:00", "result": "ok", "note": ""})
    for number, stamp in [(2, "2026-10-30 12:00:00"), (10, "2026-10-30 10:00:00")]:
        outbox.write_atomic(tmp_path / f"2026-10-30-resend-{number}.sent", {"sent_at": stamp, "result": "ok", "note": ""})
    data = outbox.read_sent(tmp_path, DAY)
    assert data["main"]["result"] == "ok"
    assert list(data["resends"]) == [2, 10]
    assert data["resends"][max(data["resends"])]["sent_at"] == "2026-10-30 10:00:00"
    assert outbox.read_sent(tmp_path, date(2026, 10, 29)) == {"main": None, "first": None, "resends": {}, "first_resends": {}}


def test_previous_push_window_ignores_nonpush_test_resends_and_today(tmp_path):
    for value, push in [("2026-10-23", True), ("2026-10-28", False), ("2026-10-30", True)]:
        outbox.write_atomic(tmp_path / f"{value}.json", {"push": push, "generated_at": f"{value} 08:55:00"})
    outbox.write_atomic(tmp_path / "2026-10-29-resend-1.json", {"push": True, "generated_at": "2026-10-29 09:00:00"})
    outbox.write_atomic(tmp_path / "2026-10-29-080000.json", {"push": True, "generated_at": "2026-10-29 08:00:00"})
    assert outbox.last_push_generated_at(tmp_path, DAY) == datetime(2026, 10, 23, 8, 55, tzinfo=BJ)
    assert outbox.last_push_generated_at(tmp_path, date(2026, 10, 23)) is None


def test_test_resend_date_and_count_follow_supplied_date_not_real_date(tmp_path):
    (tmp_path / "2026-09-30-resend-7-111111.json").write_text("{}")
    (tmp_path / "2026-10-30-resend-1-111111.json").write_text("{}")
    path = outbox.write_resend(tmp_path, date(2026, 9, 30), alarm=False, generated_at=SIMULATED,
                               text="重发", test=True, real_now=REAL)
    assert path.name == "2026-09-30-resend-2-214031.json"


def test_real_clock_uses_beijing_hours_even_if_utc_rolls_date(tmp_path):
    clock = datetime(2026, 10, 30, 22, 1, 2, tzinfo=timezone.utc)
    path = write_main(tmp_path, test=True, real_now=clock)
    assert path.name == "2026-10-30-060102.json"


def test_read_sent_formats_utc_as_naive_beijing_timestamp(tmp_path):
    outbox.write_atomic(tmp_path / "2026-10-30.sent", {"sent_at": "2026-10-30T01:00:05+00:00", "result": "ok", "note": ""})
    assert outbox.read_sent(tmp_path, DAY)["main"]["sent_at"] == "2026-10-30 09:00:05"
