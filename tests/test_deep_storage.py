from datetime import date
import json
import sqlite3

import pytest

from vcradar import db, deep
from vcradar.http import FixtureTransport
from vcradar.llm import MockLLMClient
from test_deep_support import FIXTURE, ManualClock, database, payload, seed


def test_deep_schema_migrates_legacy_rows_and_pages_without_losing_values(tmp_path):
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    seed(conn)
    conn.execute("INSERT INTO deepdive(company_id, run_date, status, input_tokens, output_tokens, estimated_cost, result_json) "
                 "VALUES ('cedar', '2026-10-30', '完成', 123, 45, 0.12, '{\"legacy\":true}')")
    conn.execute("INSERT INTO deepdive_pages(company_id, run_date, page_no, url, fetched_at, body) VALUES "
                 "('cedar', '2026-10-30', 1, 'https://news.example/old', '2026-10-30T09:00:00+08:00', 'Handwritten retained source.')")
    for column in ("prompt_tokens", "prompt_cache_hit_tokens", "completion_tokens", "elapsed_s", "usage_json", "attempt_cost", "error", "run_token"):
        conn.execute(f"ALTER TABLE deepdive DROP COLUMN {column}")
    conn.commit()
    conn.close()
    for _ in range(2):
        conn = db.connect(path)
        row = conn.execute("SELECT * FROM deepdive").fetchone()
        assert row["status"] == "完成" and row["estimated_cost"] == 0.12
        assert row["prompt_tokens"] == row["input_tokens"] == 123
        assert row["completion_tokens"] == row["output_tokens"] == 45
        assert row["prompt_cache_hit_tokens"] == 0 and row["elapsed_s"] == 0
        assert json.loads(row["result_json"]) == {"legacy": True}
        assert json.loads(row["usage_json"]) == []
        assert row["run_token"] == ""
        assert conn.execute("SELECT COUNT(*) FROM deepdive_pages").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
        conn.close()


def test_legacy_aggregate_cost_is_included_when_month_ledger_is_rebuilt(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    conn.execute("INSERT INTO deepdive(company_id, run_date, status, estimated_cost, started_at) VALUES "
                 "('cedar', '2026-09-30', '完成', 1.25, '2026-09-30T08:40:00+08:00')")
    conn.commit()
    deep.write_usage(conn, cfg)
    first = (cfg.state_dir / "test/deep-usage.json").read_bytes()
    deep.write_usage(conn, cfg)
    assert (cfg.state_dir / "test/deep-usage.json").read_bytes() == first
    ledger = json.loads(first)
    assert ledger["2026-09"]["estimated_cost"] == 1.25
    assert ledger["2026-09"]["days"]["2026-09-30"]["estimated_cost"] == 1.25
    conn.close()


def test_page_retention_cleans_older_than_30_days_but_keeps_results_and_cutoff(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    seed(conn, "old", first="2026-09-01")
    for day in ("2026-09-30", "2026-10-01"):
        conn.execute("INSERT INTO deepdive(company_id, run_date, status, result_json) VALUES ('old', ?, '完成', '{\"historical\":true}')", (day,))
        conn.execute("INSERT INTO deepdive_pages(company_id, run_date, page_no, url, fetched_at, body) VALUES ('old', ?, 1, 'https://old.example/', ?, 'Synthetic retained body.')", (day, day))
    conn.commit()
    deep.run_for_today(conn, cfg, ManualClock(), transport=FixtureTransport(FIXTURE), client=MockLLMClient([payload()]), link_records=[])
    assert conn.execute("SELECT COUNT(*) FROM deepdive WHERE company_id='old'").fetchone()[0] == 2
    assert [row[0] for row in conn.execute("SELECT run_date FROM deepdive_pages WHERE company_id='old'")] == ["2026-10-01"]
    assert all(json.loads(row[0]) == {"historical": True} for row in conn.execute("SELECT result_json FROM deepdive WHERE company_id='old'"))
    conn.close()


def test_supplied_original_body_is_reused_without_refetch_and_quotes_remain_auditable(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn, body="Short source excerpt.")
    original = (FIXTURE / "source.html").read_text()
    transport = FixtureTransport(FIXTURE)
    results = deep.run_for_today(conn, cfg, ManualClock(), transport=transport, client=MockLLMClient([payload()]),
                                  source_bodies={"https://news.example/cedar": original}, link_records=[])
    assert results[0].status == "完成"
    assert not any(item["url"] == "https://news.example/cedar" for item in transport.request_log)
    result = json.loads(conn.execute("SELECT result_json FROM deepdive").fetchone()[0])
    stored = {row[0]: row[1] for row in conn.execute("SELECT page_no, body FROM deepdive_pages")}

    def check(value):
        if isinstance(value, dict):
            if value.get("quote") and "page" in value:
                assert deep.quote_matches(value["quote"], stored[value["page"]])
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(result)
    conn.close()


def test_empty_selection_does_not_construct_network_or_model_clients(tmp_path, monkeypatch):
    conn, cfg = database(tmp_path)
    monkeypatch.setattr(deep, "_client", lambda *a: pytest.fail("没有公司时不应加载客户端或密钥"))
    monkeypatch.setattr(deep.http, "UrllibTransport", lambda *a: pytest.fail("不能联网"))
    assert deep.run_for_today(conn, cfg, ManualClock()) == []
    assert not (cfg.state_dir / "deep-usage.json").exists()
    conn.close()
