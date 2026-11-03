"""E4/5a/12：推送失败窗口、显示金额与备注迁移。"""
from datetime import date, datetime, timedelta
import sqlite3

import pytest

from test_core_v15e import NOW, add_item, conn
from test_pipe_pipeline import offline
from vcradar import cal, db, outbox, render, scan
from vcradar.config import BJ


@pytest.mark.parametrize("missing,start,today", [("2026-10-23", "2026-10-16", "2026-10-30"),
                                                ("2026-09-30", "2026-09-24", "2026-10-09")])
@pytest.mark.parametrize("artifact,expected", [("none", 1), ("fallback", 1), ("push_false", 1),
    ("resend_only", 1), ("valid", 0), ("first", 0), ("first_receipt", 0)])
def test_e4_missing_push_day_counts_despite_healthy_digest(conn, tmp_path, missing, start, today, artifact, expected):
    start, today = date.fromisoformat(start), date.fromisoformat(today)
    directory = tmp_path / "outbox"
    began = datetime.combine(start, datetime.min.time(), tzinfo=BJ)
    conn.execute("INSERT INTO fetch_run(run_id, source_id, started_at, ended_at, result) VALUES ('start', 'tc_venture', ?, ?, '成功')",
                 (began.isoformat(), began.isoformat()))
    outbox.write_first(directory, start, alarm=False, generated_at=began, text="手写首期", test=False, real_now=began)
    for offset in range(1, (today - start).days):
        day = start + timedelta(days=offset)
        if not cal.is_cn_workday(day):
            continue
        scan.upsert_digest(conn, {"date": day, "status": "没有新增"})
        if str(day) != missing:
            outbox.write_atomic(directory / f"{day}.json", {"push": cal.is_push_day(day)})
    path = directory / f"{missing}.json"
    if artifact == "fallback":
        outbox.write_atomic(path.with_suffix(".sent"), {"sent_at": f"{missing} 09:00:00", "result": "ok", "note": "兜底：没有交接文件"})
    elif artifact == "push_false":
        outbox.write_atomic(path, {"push": False})
    elif artifact == "resend_only":
        outbox.write_atomic(directory / f"{missing}-resend-1.json", {"push": True})
    elif artifact == "valid":
        outbox.write_atomic(path, {"push": True})
    elif artifact == "first":
        outbox.write_atomic(directory / f"{missing}-first.json", {"push": True})
    elif artifact == "first_receipt":
        outbox.write_atomic(directory / f"{missing}-first.sent", {"result": "ok"})
    assert scan.failed_days_in_window(conn, began, today, outbox_dir=directory) == expected


@pytest.mark.parametrize("secondary,expected,disputed", [("$10.5M", "1050 万美元", False), ("$11M", "1100 万美元", False),
                                                       ("$11.1M", "TechCrunch 写 1000 万美元，Crunchbase News 写 1110 万美元", True)])
def test_e5a_displayed_amount_and_sort_only_use_primary_for_disputes(conn, secondary, expected, disputed):
    event_id, _ = add_item(conn, amount="$10M")
    add_item(conn, "cbn_main", amount=secondary)
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == expected and ("金额待核" in card.marks) is disputed
    assert card.sources[0].name == "TechCrunch"
    assert scan.card_sort_data(conn, event_id)[0] == (10e6 if disputed else scan.parse_amount_usd(secondary))


def test_e5a_same_credibility_uses_earlier_publication_even_if_ingested_later(conn):
    add_item(conn, "tc_venture", amount="$10M", published=NOW)
    add_item(conn, "tc_startups", amount="$10.5M", published=NOW - timedelta(days=1), seen=NOW + timedelta(minutes=1))
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "1050 万美元" and card.amount_usd == 10.5e6


def test_e5a_nondisputed_displayed_amount_controls_rank(conn):
    add_item(conn, amount="$10M")
    add_item(conn, "cbn_main", amount="$11M")
    add_item(conn, amount="$10.75M", name="Other", website="https://other.example")
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    ranked = render.sort_cards(listing["cards"], first_seen=listing["first_seen"])
    assert [card.name for card in ranked] == ["Synthetic", "Other"]
    assert ranked[0].amount_label == "1100 万美元"


def test_e12_new_and_legacy_databases_have_note_with_preserved_rows(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(db.SCHEMA_SQL.replace(
        ", note TEXT NOT NULL DEFAULT ''\n);\nCREATE TABLE IF NOT EXISTS deepdive", "\n);\nCREATE TABLE IF NOT EXISTS deepdive"))
    assert "note" not in {row[1] for row in legacy.execute("PRAGMA table_info(digest)")}
    legacy.execute("INSERT INTO digest(date, text) VALUES ('2026-10-26', '原有交接文字')")
    legacy.commit()
    legacy.close()
    for attempt in range(2):
        connection = db.connect(path)
        row = connection.execute("SELECT text, note FROM digest").fetchone()
        assert row[0] == "原有交接文字" and row[1] == ("" if attempt == 0 else "TechCrunch 未补全")
        connection.execute("UPDATE digest SET note='TechCrunch 未补全'")
        connection.commit()
        connection.close()
    fresh = db.connect(tmp_path / "fresh.db")
    try:
        scan.upsert_digest(fresh, {"date": NOW.date(), "note": "手写覆盖备注"}, first_issue=True)
        row = fresh.execute("SELECT note, status, push FROM digest").fetchone()
        assert row["note"] == "手写覆盖备注" and row["push"] == 1
    finally:
        fresh.close()
