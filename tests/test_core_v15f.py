import json
from datetime import date, datetime, timedelta, timezone

import pytest

from vcradar import db, outbox, render, scan
from vcradar.config import BJ
from vcradar.models import RawItem
from test_core_v15e import NOW, add_item, conn


def start_fetch(conn, stamp=NOW - timedelta(days=4)):
    conn.execute("INSERT INTO fetch_run(run_id, source_id, started_at, ended_at, result) "
                 "VALUES ('synthetic-start', 'tc_venture', ?, ?, '成功')", (stamp.isoformat(), stamp.isoformat()))


@pytest.mark.parametrize("test", [False, True])
def test_first_resends_use_separate_numbering_and_preserve_first_file(tmp_path, test):
    directory = tmp_path / ("outbox-test" if test else "outbox")
    first = outbox.write_first(directory, NOW.date(), alarm=False, generated_at=NOW,
                               text="手写首期", test=test, real_now=NOW)
    before = first.read_bytes()
    outbox.write_atomic(directory / f"{NOW.date()}-resend-7.json", {"text": "普通重发，不计首期序号"})
    paths = [outbox.write_first_resend(directory, NOW.date(), alarm=True, generated_at=NOW,
                                       text="重发 · 手写首期", test=test, real_now=NOW) for _ in range(2)]
    assert [path.name for path in paths] == ["2026-10-30-first-resend-1.json", "2026-10-30-first-resend-2.json"]
    assert first.read_bytes() == before
    payload = json.loads(paths[-1].read_text())
    assert list(payload) == ["date", "push", "alarm", "generated_at", "text"]
    assert payload["push"] is True and payload["alarm"] is True
    assert payload["generated_at"] == "2026-10-30 08:45:12"
    assert not list(directory.glob("*.tmp"))


def test_first_resend_counts_only_same_dates_json_files(tmp_path):
    for name in ["2026-10-30-first-resend-9.json", "2026-10-30-first-resend-1.sent",
                 "2026-10-29-first-resend-1.json", "2026-10-30-resend-1.json", "2026-10-30-first-resend-bad.json"]:
        (tmp_path / name).write_text("{}")
    path = outbox.write_first_resend(tmp_path, NOW.date(), alarm=False, generated_at=NOW, text="重发", test=False)
    assert path.name == "2026-10-30-first-resend-2.json"


def test_first_resend_sequence_collision_never_overwrites(tmp_path):
    for number in (1, 3):
        (tmp_path / f"2026-10-30-first-resend-{number}.json").write_text("original")
    with pytest.raises(FileExistsError):
        outbox.write_first_resend(tmp_path, NOW.date(), alarm=False, generated_at=NOW, text="重发", test=True)
    assert (tmp_path / "2026-10-30-first-resend-3.json").read_text() == "original"


def test_first_resend_receipts_are_read_without_colliding_with_normal_resends(tmp_path):
    for prefix, number, sent in [("first-resend", 2, "2026-10-30 12:00:00"),
                                  ("first-resend", 10, "2026-10-30T02:00:00+00:00"),
                                  ("resend", 2, "2026-10-30 11:00:00")]:
        outbox.write_atomic(tmp_path / f"{NOW.date()}-{prefix}-{number}.sent", {"sent_at": sent, "result": "ok", "note": prefix})
    receipts = outbox.read_sent(tmp_path, NOW.date())
    assert list(receipts["first_resends"]) == [2, 10]
    latest = receipts["first_resends"][max(receipts["first_resends"])]
    assert latest["sent_at"] == "2026-10-30 10:00:00"
    assert receipts["resends"][2]["note"] == "resend"
    assert receipts["first_resends"][2]["note"] == "first-resend"


def test_first_filename_uses_supplied_date_and_real_beijing_clock(tmp_path):
    clock = datetime(2026, 10, 30, 23, 1, 2, tzinfo=timezone.utc)
    path = outbox.write_first(tmp_path, date(2026, 9, 30), alarm=False, generated_at=NOW,
                               text="手写首期", test=True, real_now=clock)
    assert path.name == "2026-09-30-070102-first.json"
    assert json.loads(path.read_text())["date"] == "2026-09-30"


def test_first_issue_date_ignores_first_resends_and_nonpush_files(tmp_path):
    outbox.write_atomic(tmp_path / "2026-10-26-first-resend-1.json", {"push": True})
    outbox.write_atomic(tmp_path / "2026-10-27-first.json", {"push": False})
    outbox.write_atomic(tmp_path / "2026-10-28-104500-first.json", {"push": True})
    assert outbox.first_issue_date(tmp_path) == date(2026, 10, 28)


def test_failed_days_excludes_first_issue_day_from_digest_metadata(conn):
    start_fetch(conn)
    scan.upsert_digest(conn, {"date": "2026-10-28", "status": "有新增", "push": False}, first_issue=True)
    assert conn.execute("SELECT push FROM digest").fetchone()[0] == 1
    assert scan.failed_days_in_window(conn, None, NOW.date()) == 1
    scan.upsert_digest(conn, {"date": "2026-10-28", "sent_result": "ok"})
    assert scan.failed_days_in_window(conn, None, NOW.date()) == 1
    scan.upsert_digest(conn, {"date": "2026-10-29", "status": "没有新增"})
    assert scan.failed_days_in_window(conn, None, NOW.date()) == 0


def test_failed_days_excludes_legacy_baseline_first_digest_date(conn):
    start_fetch(conn)
    scan.upsert_digest(conn, {"date": "2026-10-28", "status": "基线", "push": True})
    assert scan.failed_days_in_window(conn, None, NOW.date()) == 1


@pytest.mark.parametrize("test", [False, True])
def test_failed_days_uses_first_file_date_for_production_and_tests(conn, tmp_path, test):
    start_fetch(conn)
    directory = tmp_path / ("outbox-test" if test else "outbox")
    first_day = date(2026, 10, 28)
    outbox.write_first(directory, first_day, alarm=False, generated_at=NOW - timedelta(days=2),
                       text="手写首期", test=test, real_now=NOW)
    assert scan.failed_days_in_window(conn, None, NOW.date(), outbox_dir=directory) == 1
    if not test:
        assert scan.failed_days_in_window(conn, None, NOW.date()) == 1


def test_failed_days_skips_first_day_even_if_that_day_had_missed_range(conn):
    start_fetch(conn)
    conn.execute("UPDATE fetch_run SET missed_range='手写首期未补全' WHERE run_id='synthetic-start'")
    scan.upsert_digest(conn, {"date": "2026-10-26", "status": "全部失败"}, first_issue=True)
    assert scan.failed_days_in_window(conn, None, NOW.date()) == 3


def test_previous_push_window_still_limits_days_after_first_issue(conn):
    start_fetch(conn, NOW - timedelta(days=10))
    scan.upsert_digest(conn, {"date": "2026-10-20", "status": "有新增"}, first_issue=True)
    previous = datetime(2026, 10, 28, 8, 55, tzinfo=BJ)
    assert scan.failed_days_in_window(conn, previous, NOW.date()) == 1


def test_first_resend_is_not_a_new_push_window_boundary(tmp_path):
    outbox.write_first(tmp_path, date(2026, 10, 27), alarm=False, generated_at=NOW - timedelta(days=3),
                       text="首期", test=False, real_now=NOW)
    outbox.write_first_resend(tmp_path, date(2026, 10, 29), alarm=False, generated_at=NOW - timedelta(days=1),
                              text="重发", test=False)
    assert outbox.last_push_generated_at(tmp_path, NOW.date()) == (NOW - timedelta(days=3)).replace(microsecond=0)


@pytest.mark.parametrize("stamp", [
    "2026-10-20 07:40:01", datetime(2026, 10, 20, 7, 40, 1),
    datetime(2026, 10, 19, 23, 40, 1, tzinfo=timezone.utc),
])
def test_replay_first_ingested_at_overrides_now_only_on_first_insert(conn, stamp):
    eid, _ = add_item(conn, first_ingested_at=stamp)
    assert scan.card_sort_data(conn, eid)[1] == datetime(2026, 10, 20, 7, 40, 1, tzinfo=BJ)
    add_item(conn, "cbn_main", first_ingested_at="2026-10-21 08:00:00")
    assert conn.execute("SELECT first_ingested_at FROM funding_event WHERE id=?", (eid,)).fetchone()[0] == "2026-10-20 07:40:01"


def test_replay_equal_ingestion_timestamps_produce_deterministic_top_ten(conn):
    names = ["Maple", "larch", "Juniper", "Iris", "Hazel", "Ginkgo", "Fir", "Elm", "Dogwood", "Cedar", "birch", "Aster"]
    for index, name in enumerate(names):
        add_item(conn, name=name, website=f"https://{name.lower()}.example", now=NOW + timedelta(minutes=index),
                 first_ingested_at="2026-10-29 07:00:00")
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    ranked = render.sort_cards(listing["cards"], first_seen=listing["first_seen"])
    assert [card.name for card in ranked[:10]] == sorted(names, key=str.casefold)[:10]
    assert conn.execute("SELECT COUNT(DISTINCT first_ingested_at) FROM funding_event").fetchone()[0] == 1


def test_card_sort_data_uses_frozen_usd_instead_of_reparsing_raw_text(conn):
    eid, _ = add_item(conn, amount="$10M")
    conn.execute("UPDATE funding_event SET amount_text='$42M' WHERE id=?", (eid,))
    assert scan.card_sort_data(conn, eid)[0] == 10e6
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "1000 万美元"
    assert card.amount_usd == 10e6


def test_card_sort_data_uses_first_source_only_for_pending_amount(conn):
    eid, _ = add_item(conn, amount="$10M")
    add_item(conn, "cbn_main", amount="$30M")
    conn.execute("UPDATE funding_event SET amount_text='手写的备用金额 $99M' WHERE id=?", (eid,))
    assert scan.card_sort_data(conn, eid)[0] == 10e6
    assert "金额待核" in scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].marks


def test_displayed_amount_controls_large_round_mark_when_not_pending(conn):
    eid, _ = add_item(conn, amount="$49M")
    add_item(conn, "cbn_main", amount="$50M")
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "5000 万美元"
    assert scan.card_sort_data(conn, eid)[0] == 50e6
    assert card.marks == ["大额"]


def test_is_empty_requires_all_three_business_tables_to_be_empty(conn):
    assert db.is_empty(conn)
    scan.upsert_digest(conn, {"date": NOW.date(), "status": "没有新增"})
    assert db.is_empty(conn)
    conn.execute("INSERT INTO company(id, name_key, name, first_seen) VALUES ('only', 'only', 'Only', '2026-10-30')")
    assert not db.is_empty(conn)
    conn.execute("INSERT INTO funding_event(company_id, round_norm, event_date, first_seen) "
                 "VALUES ('only', '种子轮', '2026-10-30', '2026-10-30 08:45:00')")
    assert not db.is_empty(conn)
    conn.execute("DELETE FROM funding_event")
    conn.execute("DELETE FROM company")
    assert db.is_empty(conn)
    item = RawItem("tc_venture", "raw-only", "https://synthetic.example/raw", "仅有原始条目", NOW)
    scan.upsert_raw_item(conn, item, NOW, False)
    assert not db.is_empty(conn)
