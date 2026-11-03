import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from vcradar import cal, db, outbox, render, scan
from vcradar.config import BJ
from vcradar.models import Extracted, LabelDim, RawItem
from vcradar.sources import registry


NOW = datetime(2026, 10, 30, 8, 45, 12, 123456, tzinfo=BJ)
BATCHES = ["Fall 2026", "Summer 2026", "Spring 2026", "Winter 2026"]


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "v15e.db")
    db.sync_sources(connection, registry.P0_SOURCES)
    yield connection
    connection.close()


def add_item(conn, source_id="tc_venture", *, amount="$10M", seen=NOW, now=None, published=NOW,
             name="Synthetic", website="https://synthetic.example", baseline=False, round_text="Seed",
             first_ingested_at=None):
    serial = str(conn.execute("SELECT COUNT(*) FROM raw_item").fetchone()[0] + 1)
    source = next(source for source in registry.P0_SOURCES if source.id == source_id)
    item = RawItem(source_id, serial, source.url.split("?", 1)[0].rstrip("/") + f"/synthetic/{serial}",
                   "手写 V15e 融资样例", published, excerpt="手写测试摘录。",
                   structured={"yc_batch": "Fall 2026", "location": "San Francisco, CA, USA"})
    ext = Extracted(company_name=name, website=website, round_text=round_text, amount_text=amount,
                    hq_text="San Francisco", summary="这是手写的测试简介。",
                    ai=LabelDim("否"), social=LabelDim("否"))
    raw_id, _ = scan.upsert_raw_item(conn, item, seen, baseline)
    event_id = scan.ingest(conn, raw_id, item, ext, now=now or seen, is_baseline=baseline,
                           is_backfill=False, latest_yc_batches=BATCHES, model=None, first_ingested_at=first_ingested_at)
    return event_id, raw_id


def test_first_ingested_at_is_actual_event_insert_time_and_never_changes(conn):
    eid, _ = add_item(conn, seen=NOW - timedelta(days=10), now=NOW)
    row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
    assert row["first_ingested_at"] == "2026-10-30 08:45:12"
    assert scan.card_sort_data(conn, eid) == (10e6, NOW.replace(microsecond=0))
    add_item(conn, "cbn_main", seen=NOW - timedelta(days=20), now=NOW + timedelta(hours=1))
    scan.mark_listed(conn, [eid], NOW.date())
    add_item(conn, "cbn_seed", seen=NOW + timedelta(days=91), now=NOW + timedelta(days=91))
    row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
    assert row["first_ingested_at"] == "2026-10-30 08:45:12"
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["first_seen"]["synthetic"] == NOW.replace(microsecond=0)


def test_first_ingested_at_uses_beijing_seconds_and_is_distinct_for_each_round(conn):
    utc = datetime(2026, 10, 29, 23, 45, 11, 999999, tzinfo=timezone.utc)
    seed, _ = add_item(conn, now=utc)
    series_a, _ = add_item(conn, round_text="Series A", now=utc + timedelta(seconds=2))
    assert scan.card_sort_data(conn, seed)[1] == datetime(2026, 10, 30, 7, 45, 11, tzinfo=BJ)
    assert scan.card_sort_data(conn, series_a)[1] == datetime(2026, 10, 30, 7, 45, 13, tzinfo=BJ)
    with pytest.raises(KeyError):
        scan.card_sort_data(conn, 999999)


def test_company_merge_keeps_surviving_events_first_ingested_at(conn):
    add_item(conn, name="Original", website=None, now=NOW - timedelta(days=2))
    target, _ = add_item(conn, name="Alias", website="https://unified.example", now=NOW - timedelta(days=1))
    original_stamp = scan.card_sort_data(conn, target)[1]
    merged, _ = add_item(conn, "cbn_main", name="Original", website="https://unified.example", now=NOW)
    assert merged == target
    assert scan.card_sort_data(conn, target)[1] == original_stamp
    assert conn.execute("SELECT COUNT(*) FROM company").fetchone()[0] == 1


def test_old_schema_migration_backfills_once_and_renames_amount_status(tmp_path):
    path = tmp_path / "old.db"
    old = db.connect(path)
    old.execute("ALTER TABLE funding_event DROP COLUMN first_ingested_at")
    old.execute("INSERT INTO company(id, name_key, name, first_seen) VALUES ('old', 'old', 'Old', '2026-10-30')")
    old.execute("INSERT INTO funding_event(company_id, round_norm, event_date, first_seen, amount_status) "
                "VALUES ('old', '种子轮', '2026-10-30', '2026-10-29T23:30:04+00:00', '有分歧')")
    old.commit()
    old.close()
    migrated = db.connect(path)
    assert tuple(migrated.execute("SELECT first_ingested_at, amount_status FROM funding_event").fetchone()) == (
        "2026-10-30 07:30:04", "金额待核")
    migrated.execute("UPDATE funding_event SET first_seen='2026-01-01T00:00:00+08:00'")
    migrated.commit()
    migrated.close()
    reopened = db.connect(path)
    assert reopened.execute("SELECT first_ingested_at FROM funding_event").fetchone()[0] == "2026-10-30 07:30:04"
    reopened.close()


def test_card_sources_follow_prd_order_and_each_publishers_first_ingestion(conn):
    source_ids = ["ts_web", "alleywatch", "strictlyvc", "launch_hn", "yc_directory", "yc_launches",
                  "cbn_venture", "cbn_seed", "cbn_main", "tc_startups", "tc_venture"]
    for index, sid in enumerate(source_ids):
        add_item(conn, sid, amount="$11M" if sid == "tc_startups" else "$10M", seen=NOW + timedelta(minutes=index))
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert [source.name for source in card.sources] == [
        "TechCrunch", "TechCrunch", "Crunchbase News", "Crunchbase News", "Crunchbase News",
        "YC Launches", "YC 公司目录", "Launch HN", "StrictlyVC", "AlleyWatch", "Term Sheet",
    ]
    assert card.sources[0].url.endswith("/10")
    assert card.sources[1].url.endswith("/11")
    assert card.amount_label == "1000 万美元"
    assert card.amount_usd == 10e6


def test_source_order_is_independent_of_fetch_priority(conn, monkeypatch):
    add_item(conn, amount="$10M")
    add_item(conn, "cbn_main", amount="$20M")
    changed = [replace(source, order=30) if source.name == "TechCrunch" else source for source in registry.P0_SOURCES]
    monkeypatch.setattr(registry, "P0_SOURCES", changed)
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.sources[0].name == "TechCrunch"
    assert card.amount_usd == 10e6
    assert next(source.order for source in registry.P0_SOURCES if source.id == "ts_web") == 0


def test_first_source_is_primary_even_when_added_later_and_less_credible(conn):
    eid, _ = add_item(conn, "cbn_main", amount="$20M", seen=NOW - timedelta(days=2), published=NOW - timedelta(days=3))
    add_item(conn, "tc_venture", amount="$10M", seen=NOW, published=NOW)
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "TechCrunch 写 1000 万美元，Crunchbase News 写 2000 万美元"
    assert card.amount_usd == 10e6
    assert conn.execute("SELECT amount_status FROM funding_event WHERE id=?", (eid,)).fetchone()[0] == "金额待核"


def test_displayed_amount_is_used_within_ten_percent(conn):
    eid, _ = add_item(conn, "cbn_main", amount="$11M")
    add_item(conn, "tc_venture", amount="$10M", seen=NOW + timedelta(minutes=1))
    assert scan.card_sort_data(conn, eid)[0] == 11e6
    assert conn.execute("SELECT amount_text, amount_status FROM funding_event WHERE id=?", (eid,)).fetchone()[:] == ("$11M", "披露")


def test_undisclosed_first_source_does_not_hide_other_disclosed_amount(conn):
    eid, _ = add_item(conn, "cbn_main", amount="$10M")
    add_item(conn, "tc_venture", amount=None)
    assert scan.card_sort_data(conn, eid)[0] == 10e6
    assert conn.execute("SELECT amount_status FROM funding_event WHERE id=?", (eid,)).fetchone()[0] == "披露"


def test_render_ranks_cards_using_immutable_first_ingested_times(conn):
    add_item(conn, name="Alpha", website="https://alpha.example", seen=NOW - timedelta(days=10), now=NOW)
    add_item(conn, name="Beta", website="https://beta.example", seen=NOW, now=NOW - timedelta(minutes=1))
    add_item(conn, name="Rich", website="https://rich.example", amount="$20M", now=NOW + timedelta(minutes=1))
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert [card.name for card in render.sort_cards(listing["cards"], first_seen=listing["first_seen"])] == ["Rich", "Beta", "Alpha"]


def write_first(directory, *, test=False, clock=NOW):
    return outbox.write_first(directory, NOW.date(), alarm=False, generated_at=NOW,
                               text="手写首期推送", test=test, real_now=clock)


def test_first_issue_paths_and_payload(tmp_path):
    path = write_first(tmp_path)
    assert path == outbox.first_path(tmp_path, NOW.date())
    assert path.name == "2026-10-30-first.json"
    payload = json.loads(path.read_text())
    assert list(payload) == ["date", "push", "alarm", "generated_at", "text"]
    assert payload["push"] is True
    assert payload["generated_at"] == "2026-10-30 08:45:12"
    test_path = write_first(tmp_path / "test", test=True, clock=NOW + timedelta(hours=5))
    assert test_path.name == "2026-10-30-134512-first.json"


def test_first_sent_is_read_and_protects_first_file(tmp_path):
    path = write_first(tmp_path)
    original = path.read_bytes()
    outbox.write_atomic(path.with_suffix(".sent"), {"sent_at": "2026-10-30T00:50:00+00:00", "result": "ok", "note": ""})
    write_first(tmp_path, clock=NOW + timedelta(hours=1))
    assert path.read_bytes() == original
    receipts = outbox.read_sent(tmp_path, NOW.date())
    assert receipts["main"] is None
    assert receipts["first"] == {"sent_at": "2026-10-30 08:50:00", "result": "ok", "note": ""}


@pytest.mark.parametrize("test", [False, True])
def test_first_issue_suppresses_same_days_scheduled_push(tmp_path, test):
    first = write_first(tmp_path, test=test)
    normal = outbox.write_main(tmp_path, NOW.date(), push=True, alarm=False, generated_at=NOW,
                               text="不应重复推送", test=test, real_now=NOW + timedelta(seconds=1))
    assert json.loads(first.read_text())["push"] is True
    payload = json.loads(normal.read_text())
    assert payload["push"] is False
    assert payload["text"] == ""


def test_last_push_window_includes_first_and_excludes_resend_test_and_fallback(tmp_path):
    outbox.write_atomic(tmp_path / "2026-10-23.json", {"push": True, "generated_at": "2026-10-23 08:55:00"})
    outbox.write_atomic(tmp_path / "2026-10-27-first.json", {"push": True, "generated_at": "2026-10-27 10:00:00"})
    for filename in ["2026-10-28-first-090000.json", "2026-10-28-resend-1.json", "2026-10-28-fallback.json"]:
        outbox.write_atomic(tmp_path / filename, {"push": True, "generated_at": "2026-10-28 10:00:00"})
    outbox.write_atomic(tmp_path / "2026-10-29.json", {"push": True, "generated_at": "2026-10-29 09:00:00"})
    outbox.write_atomic(tmp_path / "2026-10-29.sent", {"sent_at": "2026-10-29 08:58:00", "result": "ok", "note": "兜底：没有交接文件"})
    assert outbox.last_push_generated_at(tmp_path, NOW.date()) == datetime(2026, 10, 27, 10, tzinfo=BJ)


def test_failed_run_main_file_is_still_a_window_boundary(tmp_path):
    outbox.write_main(tmp_path, date(2026, 10, 23), push=True, alarm=True,
                       generated_at=NOW - timedelta(days=7), text="手写抓取未跑完提示", test=False, real_now=NOW)
    assert outbox.last_push_generated_at(tmp_path, NOW.date()) == (NOW - timedelta(days=7)).replace(microsecond=0)


def test_last_push_chooses_latest_generated_time_between_first_and_normal(tmp_path):
    for suffix, hour in [("-first", 10), ("", 9)]:
        outbox.write_atomic(tmp_path / f"2026-10-29{suffix}.json", {"push": True, "generated_at": f"2026-10-29 {hour:02d}:00:00"})
    assert outbox.last_push_generated_at(tmp_path, NOW.date()) == datetime(2026, 10, 29, 10, tzinfo=BJ)


@pytest.mark.parametrize("previous,today,expected", [
    (None, "2026-10-30", False), ("2026-10-23", "2026-10-30", False),
    ("2026-09-30", "2026-10-09", True), ("2026-10-22", "2026-10-30", True),
    ("2026-10-30", "2026-10-30", False), ("2026-10-31", "2026-10-30", False),
])
def test_since_last_push_uses_beijing_natural_dates(previous, today, expected):
    assert cal.since_last_push(date.fromisoformat(previous) if previous else None, date.fromisoformat(today)) is expected


def test_no_fetch_runs_means_not_launched_and_no_failed_days(conn):
    assert scan.failed_days_in_window(conn, None, NOW.date()) == 0


@pytest.mark.parametrize("first_fetch,expected", [
    ("2026-10-28T08:40:00+08:00", 2), ("2026-10-29T08:40:00+08:00", 1),
    ("2026-10-30T08:40:00+08:00", 0), ("2026-10-26T16:30:00+00:00", 3),
])
def test_failed_days_excludes_days_before_first_fetch(conn, first_fetch, expected):
    stamp = datetime.fromisoformat(first_fetch)
    conn.execute("INSERT INTO fetch_run(run_id, source_id, started_at, ended_at, result) VALUES (?, 'tc_venture', ?, ?, '失败')",
                 ("first", stamp.isoformat(), stamp.isoformat()))
    assert scan.failed_days_in_window(conn, None, NOW.date()) == expected
