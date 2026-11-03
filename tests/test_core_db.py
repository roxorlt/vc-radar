import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from vcradar import db, scan
from vcradar.config import BJ
from vcradar.models import ErrorType, FetchOutcome, FetchResult, FetchRunRecord, RawItem, SourceDef


NOW = datetime(2026, 10, 30, 8, 30, tzinfo=BJ)
SOURCE = SourceDef("yc_directory", "YC 公司目录", "companies", "https://www.ycombinator.com/companies", "api", None, 2)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "data" / "test.db")
    db.sync_sources(connection, [SOURCE])
    yield connection
    connection.close()


def outcome(result=FetchResult.SUCCESS, *, clock=NOW, batch=None, total=None):
    run = FetchRunRecord(SOURCE.id, clock, clock + timedelta(seconds=1), 200, 2, result,
                         latest_at=clock, is_backfill=True, missed_range="10-28～10-29")
    return FetchOutcome(SOURCE, [run], [], result == FetchResult.SUCCESS, yc_batch=batch, yc_batch_total=total)


def test_all_tables_calendar_and_connect_idempotent(tmp_path):
    path = tmp_path / "schema.db"
    with db.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"source", "fetch_run", "raw_item", "company", "funding_event", "event_source", "digest",
                          "deepdive", "deepdive_pages", "tag_change", "calendar", "meta", "verify_log", "verify_search", "homepage_check"}
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert isinstance(connection.execute("SELECT * FROM meta").fetchone(), sqlite3.Row)
        assert db.is_empty(connection)
        count = connection.execute("SELECT COUNT(*) FROM calendar").fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM calendar WHERE country='中国' AND type='调休上班'").fetchone()[0] == 11
        deep_columns = {row[1] for row in connection.execute("PRAGMA table_info(deepdive)")}
        assert {"running", "push_judgement_json", "result_json", "materials_json", "search_count", "model_calls",
                "input_tokens", "output_tokens", "estimated_cost", "removed_points", "quality_pending",
                "new_evidence_json", "new_evidence_found_at", "started_at", "ended_at", "online_at", "simulated"} <= deep_columns
    with db.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM calendar").fetchone()[0] == count


def test_sync_preserves_runtime_fields(conn):
    conn.execute("UPDATE source SET crawl_status='已停抓', stopped_at='time', stop_reason='异议', "
                 "stop_notice_date='2026-10-30', enabled=0")
    db.sync_sources(conn, [replace(SOURCE, name="新名字", url="https://example.invalid/new", stale_days=3)])
    row = conn.execute("SELECT * FROM source").fetchone()
    assert (row["name"], row["url"], row["stale_days"]) == ("新名字", "https://example.invalid/new", 3)
    assert (row["crawl_status"], row["stopped_at"], row["stop_reason"], row["stop_notice_date"], row["enabled"]) == (
        "已停抓", "time", "异议", "2026-10-30", 0)


def test_latest_result_known_ids_issue_urls_and_yc_meta(conn):
    assert db.load_source_states(conn)[SOURCE.id].last_ok
    for index, issue in enumerate(["https://example.invalid/issue/1", "https://example.invalid/issue/1", "https://example.invalid/issue/2"]):
        scan.upsert_raw_item(conn, RawItem(SOURCE.id, str(index), f"https://example.invalid/{index}", "合成条目", None,
                                         structured={"issue_url": issue}), NOW, False)
    scan.record_fetch(conn, "ok", outcome(batch="Fall 2026", total=193), True)
    scan.record_fetch(conn, "bad", outcome(FetchResult.FAIL, clock=NOW + timedelta(days=1)), True)
    state = db.load_source_states(conn)[SOURCE.id]
    assert not state.last_ok
    assert state.known_external_ids == {"0", "1", "2"}
    assert state.known_issue_urls == {"https://example.invalid/issue/1", "https://example.invalid/issue/2"}
    assert (state.yc_last_batch, state.yc_last_batch_total) == ("Fall 2026", 193)
    scan.record_fetch(conn, "recovered", outcome(clock=NOW + timedelta(days=2)), False)
    assert db.load_source_states(conn)[SOURCE.id].last_ok
    assert not db.is_empty(conn)


def test_fetch_two_attempts_stop_and_restore(conn):
    first = FetchRunRecord(SOURCE.id, NOW, NOW, 403, 0, FetchResult.FAIL, ErrorType.RATE_LIMITED, note="HTTP 403")
    last = replace(first, started_at=NOW + timedelta(minutes=2), ended_at=NOW + timedelta(minutes=2), result=FetchResult.STOPPED)
    stopped = FetchOutcome(SOURCE, [first, last], [], False, ErrorType.TS_STOPPED, stopped_now=True)
    scan.record_fetch(conn, "blocked", stopped, False)
    scan.record_fetch(conn, "blocked", stopped, False)
    assert conn.execute("SELECT COUNT(*) FROM fetch_run").fetchone()[0] == 2
    row = conn.execute("SELECT * FROM source").fetchone()
    assert (row["crawl_status"], row["stop_reason"]) == ("已停抓", "封禁")
    assert row["stopped_at"] == (NOW + timedelta(minutes=2)).isoformat()
    db.restore_source(conn, SOURCE.id)
    row = conn.execute("SELECT * FROM source").fetchone()
    assert (row["crawl_status"], row["stopped_at"], row["stop_reason"], row["stop_notice_date"]) == ("正常", None, None, None)


def test_manual_stop_record_is_independent_and_idempotent(conn):
    db.manual_stop(conn, SOURCE.id, "异议", NOW)
    db.manual_stop(conn, SOURCE.id, "异议", NOW + timedelta(minutes=1))
    rows = conn.execute("SELECT * FROM fetch_run").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert len(row["run_id"]) == 32
    assert (row["result"], row["note"], row["http_code"], row["count"], row["new_count"]) == ("已停抓", "人工停抓", None, 0, 0)
    assert row["started_at"] == row["ended_at"] == NOW.isoformat()
    assert db.load_source_states(conn)[SOURCE.id].crawl_status == "已停抓"
    db.restore_source(conn, SOURCE.id)
    db.manual_stop(conn, SOURCE.id, "异议", NOW + timedelta(hours=1))
    assert conn.execute("SELECT COUNT(DISTINCT run_id) FROM fetch_run").fetchone()[0] == 2


def test_recovered_attempt_is_success_and_new_count(conn):
    item = RawItem(SOURCE.id, "new", "https://example.invalid/new", "合成条目", NOW)
    initial = FetchRunRecord(SOURCE.id, NOW, NOW, 429, 0, FetchResult.FAIL)
    recovered = FetchRunRecord(SOURCE.id, NOW + timedelta(minutes=2), NOW + timedelta(minutes=2), 200, 1, FetchResult.SUCCESS)
    fetched = FetchOutcome(SOURCE, [initial, recovered], [item], True)
    scan.record_fetch(conn, "retry", fetched, True)
    rows = conn.execute("SELECT * FROM fetch_run ORDER BY id").fetchall()
    assert [row["new_count"] for row in rows] == [0, 1]
    assert all(row["simulated"] == 1 for row in rows)
    assert db.load_source_states(conn)[SOURCE.id].last_ok
    assert conn.execute("SELECT crawl_status FROM source").fetchone()[0] == "正常"


def test_backup_is_full_and_independent(conn, tmp_path):
    scan.upsert_raw_item(conn, RawItem(SOURCE.id, "one", "https://example.invalid/one", "原始条目", None), NOW, False)
    conn.commit()
    target = tmp_path / "backup" / "snapshot.db"
    db.backup(conn, target)
    with sqlite3.connect(target) as saved:
        assert saved.execute("SELECT title FROM raw_item").fetchone()[0] == "原始条目"
        assert saved.execute("SELECT COUNT(*) FROM calendar").fetchone()[0] > 0
        assert json.loads(saved.execute("SELECT structured_json FROM raw_item").fetchone()[0]) == {}


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO funding_event(company_id, round_norm, event_date, first_seen) VALUES ('missing', '种子轮', '2026-10-30', '2026-10-30')")


def test_backup_uncommitted_transaction_fails_fast(conn, tmp_path):
    with pytest.raises(ValueError, match="提交或回滚"):
        db.backup(conn, tmp_path / "uncommitted.db")


def test_load_states_retains_latest_success_timestamp_after_a_failed_run(conn):
    assert db.load_source_states(conn)[SOURCE.id].last_latest_at is None
    scan.record_fetch(conn, "success", outcome(), False)
    scan.record_fetch(conn, "failure", outcome(FetchResult.FAIL, clock=NOW + timedelta(days=1)), False)
    state = db.load_source_states(conn)[SOURCE.id]
    assert not state.last_ok
    assert state.last_latest_at == NOW


def test_restore_source_clears_all_stop_fields_including_notice_date(conn):
    db.manual_stop(conn, SOURCE.id, "异议", NOW)
    conn.execute("UPDATE source SET stop_notice_date='2026-10-30' WHERE id=?", (SOURCE.id,))
    db.restore_source(conn, SOURCE.id)
    row = conn.execute("SELECT crawl_status, stopped_at, stop_reason, stop_notice_date FROM source").fetchone()
    assert tuple(row) == ("正常", None, None, None)
    assert conn.execute("SELECT COUNT(*) FROM fetch_run").fetchone()[0] == 1


def test_connect_migrates_missing_digest_push_column_without_losing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE digest(date TEXT PRIMARY KEY, status TEXT NOT NULL)")
    old.execute("INSERT INTO digest VALUES ('2026-10-23', '有新增')")
    old.commit()
    old.close()
    connection = db.connect(path)
    row = connection.execute("SELECT date, status, push FROM digest").fetchone()
    assert tuple(row) == ("2026-10-23", "有新增", 1)
    connection.close()
    connection = db.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM digest").fetchone()[0] == 1
    connection.close()


def test_connect_migrates_old_marks_idempotently(conn, tmp_path):
    conn.execute("INSERT INTO company(id, name_key, name, first_seen) VALUES ('old', 'old', 'Old', '2026-10-30')")
    conn.execute("INSERT INTO funding_event(company_id, round_norm, event_date, first_seen, marks_json) VALUES "
                 "('old', '种子轮', '2026-10-30', '2026-10-30', ?)",
                 (json.dumps(["金额有分歧", "假期补发", "补抓", "大额", "新一轮"], ensure_ascii=False),))
    conn.commit()
    path = tmp_path / "data" / "test.db"
    for _ in range(2):
        reopened = db.connect(path)
        assert json.loads(reopened.execute("SELECT marks_json FROM funding_event").fetchone()[0]) == ["新一轮", "大额", "补抓", "金额待核"]
        reopened.close()
