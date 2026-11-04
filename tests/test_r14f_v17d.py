"""R14f：V17d 回执确认、测试隔离、历史漏抓迁移和 RSS 单端语义。"""
from dataclasses import replace
from datetime import date, datetime, time as day_time, timedelta
import json
import sqlite3

import pytest

from vcradar import cli, config, db, fetcher, http, outbox, pipeline, render, scan
from vcradar.config import BJ, Config
from vcradar.models import DigestData, SourceState, SourceStatusLine
from test_pipe_pipeline import arguments, offline, run_at, write_fixture  # noqa: F401
from test_r14e_v17c import extra_source
from test_sources_support import SOURCES, fetch, rss


NOTICE = "Term Sheet 没抓到：对方不再允许抓取。本周汇总可能不全，不代表没有新公司。"
OBJECTION = "Term Sheet 没抓到：对方要求停止抓取。本周汇总可能不全，不代表没有新公司。"
FRIDAY = date(2026, 11, 6)
BASELINE = datetime(2026, 10, 31, 0, 36, tzinfo=BJ)


@pytest.fixture
def env(tmp_path, monkeypatch):
    # 真实钟点只决定测试文件名；每次递增以覆盖同日多份，不真实等待一秒。
    class FileClock(datetime):
        serial = 0

        @classmethod
        def now(cls, tz=None):
            cls.serial += 1
            stamp = datetime(2026, 11, 3, 12, tzinfo=BJ) + timedelta(seconds=cls.serial)
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)

    monkeypatch.setattr(pipeline, "datetime", FileClock)
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-31")
    run_at(cfg, fixture, "2026-10-31", first_issue=True, no_deep=True)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'state_dir = "{cfg.state_dir}"\nenv_file = "{tmp_path / "unused.env"}"\nruntime_file = "{tmp_path / "runtime.json"}"\n')
    return cfg, fixture, cfg_file


def stopped_date(cfg):
    with sqlite3.connect(cfg.db_path) as conn:
        return conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0]


def row(cfg, day="2026-11-06"):
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM digest WHERE date=?", (day,)).fetchone())


def sync(env, day="2026-11-06"):
    cfg, _, cfg_file = env
    assert cli.main(["--config", str(cfg_file), "--sync-sent", "--test", "--date", day]) == 0
    assert not (cfg.state_dir / "vc-radar.db").exists()


def receipt(path, *, day="2026-11-06", result="ok", note="", sent_at=None):
    outbox.write_atomic(path.with_suffix(".sent"), {"sent_at": sent_at or f"{day} 08:59:30", "result": result, "note": note})


def manual_stop(cfg, day="2026-11-05", reason="封禁"):
    with db.connect(cfg.db_path) as conn:
        conn.execute("UPDATE source SET crawl_status='已停抓',enabled=0,stopped_at=?,stop_reason=?,stop_notice_date=NULL WHERE id='ts_web'",
                     (day + " 08:42:00", reason))


def push_stopped(env, day="2026-11-06", *, yc_failure=False):
    cfg, fixture, _ = env
    write_fixture(fixture, day, stopped=True)
    if yc_failure:
        path = fixture / "manifest.json"
        data = json.loads(path.read_text())
        data[SOURCES["yc_directory"].url] = {"status": 429}
        path.write_text(json.dumps(data))
    return run_at(cfg, fixture, day, no_deep=True)


def test_33_1_generation_keeps_date_empty_then_same_day_sync_records_receipt(env):
    cfg, fixture, _ = env
    payload, path = push_stopped(env)
    assert payload["text"].splitlines()[1] == NOTICE and payload["alarm"]
    assert stopped_date(cfg) is None
    with db.connect(cfg.db_path) as conn:
        runs = conn.execute("SELECT result,ended_at FROM fetch_run WHERE source_id='ts_web' AND started_at LIKE '2026-11-06%' ORDER BY id").fetchall()
        assert [r[0] for r in runs] == ["失败", "已停抓"] and runs[-1][1] <= "2026-11-06T08:45:00+08:00"
    sync(env)
    assert stopped_date(cfg) is None
    receipt(path)
    sync(env)
    assert stopped_date(cfg) == "2026-11-06"
    assert row(cfg)["sent_at"] == "2026-11-06 08:59:30" and row(cfg)["sent_result"] == "ok"
    again, _ = run_at(cfg, fixture, "2026-11-06", no_deep=True)
    resent, _ = run_at(cfg, fixture, "2026-11-06", resend=True, dry_run=True)
    assert NOTICE in again["text"].splitlines()[1] and NOTICE in resent["text"].splitlines()[1]
    write_fixture(fixture, "2026-11-13")
    future, _ = run_at(cfg, fixture, "2026-11-13", no_deep=True)
    assert "Term Sheet 没抓到" not in future["text"] and stopped_date(cfg) == "2026-11-06"


def test_49_1_yc_and_stopped_term_sheet_merge_then_record_only_after_sync(env):
    cfg, _, _ = env
    payload, path = push_stopped(env, yc_failure=True)
    assert payload["text"].splitlines()[1] == "YC 公司目录没抓到：对方限制了访问。本周汇总可能不全，不代表没有新公司。" + NOTICE
    assert stopped_date(cfg) is None
    receipt(path)
    sync(env)
    assert stopped_date(cfg) == "2026-11-06"


@pytest.mark.parametrize("kind,lead", [("unfinished", "部分来源没抓到：今天没能按时抓完。本周汇总可能不全，不代表没有新公司。"),
                                      ("failed", "所有来源都没抓到：今天每个网站都出了问题。本周汇总可能不全，不代表没有新公司。")])
def test_49_2_run_failure_warning_keeps_stop_sentence_and_receipt_records_date(env, monkeypatch, kind, lead):
    cfg, fixture, _ = env
    manual_stop(cfg)
    write_fixture(fixture, "2026-11-06", fail=list(SOURCES) if kind == "failed" else (), delay=25 if kind == "unfinished" else 0)
    if kind == "unfinished":
        original_client, original_get = http.FixtureTransport, pipeline._Transport.get

        class TimedFixture(original_client):
            def __init__(self, path):
                super().__init__(path)
                self.delays = {url: spec.pop("delay_s", 0) for url, spec in self.manifest.items()}

            def get(self, url, headers=None, timeout=30.0, data=None):
                delay = max((delay for key, delay in self.delays.items() if url.startswith(key)), default=0)
                if delay > timeout:
                    return http.HttpResponse(url, None, elapsed_s=timeout, error="timeout")
                return replace(super().get(url, headers, timeout, data), elapsed_s=delay)

        def timed_get(self, *args, **kwargs):
            response = original_get(self, *args, **kwargs)
            self.budget.clock.advance(response.elapsed_s)
            return response

        monkeypatch.setattr(http, "FixtureTransport", TimedFixture)
        monkeypatch.setattr(pipeline._Transport, "get", timed_get)
        monkeypatch.setattr(pipeline, "P0_SOURCES", [SOURCES[sid] for sid in ("tc_venture", "cbn_main", "alleywatch")])
    payload, path = run_at(cfg, fixture, "2026-11-06", total_timeout=60, no_deep=True)
    assert payload["text"].splitlines()[1] == lead + NOTICE
    assert stopped_date(cfg) is None
    receipt(path)
    sync(env)
    assert stopped_date(cfg) == "2026-11-06"


def test_49_3_skipped_push_day_is_not_recorded_and_later_delivery_stops_notice(env):
    cfg, fixture, _ = env
    manual_stop(cfg)
    assert not list(cfg.outbox_dir.glob("2026-11-06*.json"))
    payload, path = push_stopped(env, "2026-11-13")
    assert stopped_date(cfg) is None and payload["text"].splitlines()[1] == NOTICE
    receipt(path, day="2026-11-13")
    sync(env, "2026-11-13")
    assert stopped_date(cfg) == "2026-11-13"
    write_fixture(fixture, "2026-11-20")
    later, _ = run_at(cfg, fixture, "2026-11-20", no_deep=True)
    assert "Term Sheet 没抓到" not in later["text"]


@pytest.mark.parametrize("kind", ["fallback", "failed", "wrong-day", "resend-only"])
def test_49_3b_four_rejected_receipts_keep_date_empty_and_repeat_next_push(env, kind):
    cfg, fixture, _ = env
    manual_stop(cfg)
    _, path = push_stopped(env, "2026-11-13")
    if kind == "resend-only":
        _, resend = run_at(cfg, fixture, "2026-11-13", resend=True, dry_run=True)
        receipt(resend, day="2026-11-13")
    else:
        receipt(path, day="2026-11-14" if kind == "wrong-day" else "2026-11-13",
                result="fail" if kind == "failed" else "ok", note="兜底：没有交接文件" if kind == "fallback" else "")
    sync(env, "2026-11-13")
    assert stopped_date(cfg) is None
    write_fixture(fixture, "2026-11-20")
    later, _ = run_at(cfg, fixture, "2026-11-20", no_deep=True)
    assert later["text"].splitlines()[1] == NOTICE and stopped_date(cfg) is None


def test_49_3b_fallback_replaced_by_ok_is_rechecked_then_record_is_immutable(env):
    cfg, _, _ = env
    manual_stop(cfg)
    _, path = push_stopped(env, "2026-11-13")
    receipt(path, day="2026-11-13", note="兜底：没有交接文件")
    sync(env, "2026-11-13")
    assert stopped_date(cfg) is None
    receipt(path, day="2026-11-13")
    sync(env, "2026-11-13")
    assert stopped_date(cfg) == "2026-11-13"
    receipt(path, day="2026-11-13", result="fail")
    sync(env, "2026-11-13")
    assert stopped_date(cfg) == "2026-11-13" and row(cfg, "2026-11-13")["sent_result"] == "fail"


@pytest.mark.parametrize("entry", ["main", "failure", "resend", "deep"])
def test_recording_runs_before_main_failure_resend_and_deep_entrypoints(env, monkeypatch, entry):
    cfg, fixture, _ = env
    _, path = push_stopped(env)
    receipt(path, sent_at="2026-11-06 23:59:59")
    # 不先 sync_sent；入口须直接读主文件和回执，不能依赖 digest 回写。
    assert row(cfg)["sent_at"] is None
    if entry == "resend":
        run_at(cfg, fixture, "2026-11-06", resend=True, dry_run=True)
    elif entry == "deep":
        from vcradar import deep
        observed = []
        monkeypatch.setattr(deep, "run_cli", lambda *args: observed.append(stopped_date(cfg)) or 0)
        assert pipeline.run(cfg, arguments(deep_only=True, test=True), pipeline.Clock(FRIDAY, day_time(9, 15))) == 0
        assert observed == ["2026-11-06"]
    else:
        write_fixture(fixture, "2026-11-13")
        if entry == "failure":
            def fail(*args, **kwargs):
                raise RuntimeError("合成抓取故障")
            monkeypatch.setattr(fetcher, "fetch_all", fail)
            assert pipeline.run(cfg, arguments(fixture, no_deep=True), pipeline.Clock(date(2026, 11, 13), day_time(8, 40))) == 1
            assert NOTICE not in row(cfg, "2026-11-13")["text"]
        else:
            payload, _ = run_at(cfg, fixture, "2026-11-13", no_deep=True)
            assert NOTICE not in payload["text"]
    assert stopped_date(cfg) == "2026-11-06"


def test_recording_ignores_missing_sentence_and_does_not_use_digest_send_result(env):
    cfg, _, _ = env
    _, path = push_stopped(env)
    receipt(path)
    payload = json.loads(path.read_text())
    payload["text"] = "本周没有发现新的天使轮、种子轮、A 轮融资。"
    outbox.write_atomic(path, payload)
    with db.connect(cfg.db_path) as conn:
        conn.execute("UPDATE digest SET sent_at='2026-11-06 08:59:30',sent_result='ok' WHERE date='2026-11-06'")
        pipeline._record_ts_notice(conn, cfg, FRIDAY)
        assert conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0] is None
    sync(env)
    assert stopped_date(cfg) is None


def test_sync_sent_direct_call_confirms_notice_after_digest_write(env, monkeypatch):
    cfg, _, _ = env
    _, path = push_stopped(env)
    receipt(path)
    original = scan.upsert_digest
    observed = []

    def upsert(conn, values, **kwargs):
        observed.append(conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0])
        return original(conn, values, **kwargs)

    monkeypatch.setattr(scan, "upsert_digest", upsert)
    with db.connect(cfg.db_path) as conn:
        pipeline.sync_sent(conn, cfg, FRIDAY)
        assert conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0] == "2026-11-06"
    assert observed == [None]


def test_latest_normal_file_selects_its_own_receipt_and_excludes_future_and_resends(env):
    cfg, _, _ = env
    manual_stop(cfg)
    for name, text, sent in [("2026-11-06-080001.json", NOTICE, True),
                             ("2026-11-13-080001.json", NOTICE, True),
                             ("2026-11-13-080002.json", OBJECTION, True),
                             ("2026-11-13-080003.json", "尚未送达的新文件", False),
                             ("2026-11-20-080001.json", NOTICE, True),
                             ("2026-11-13-resend-1-090000.json", NOTICE, True)]:
        path = cfg.outbox_dir / name
        outbox.write_atomic(path, {"push": True, "text": text})
        if sent:
            receipt(path, day=name[:10], result="fail" if name == "2026-11-13-080002.json" else "ok")
    sync(env, "2026-11-13")
    assert stopped_date(cfg) is None and row(cfg, "2026-11-13")["sent_result"] is None
    receipt(cfg.outbox_dir / "2026-11-13-080002.json", day="2026-11-13")
    sync(env, "2026-11-13")
    assert stopped_date(cfg) is None
    latest = cfg.outbox_dir / "2026-11-13-080003.json"
    receipt(latest, day="2026-11-13")
    sync(env, "2026-11-13")
    assert stopped_date(cfg) is None
    outbox.write_atomic(latest, {"push": True, "text": NOTICE})
    sync(env, "2026-11-13")
    assert stopped_date(cfg) == "2026-11-13"


def test_latest_generated_push_day_blocks_older_receipt_even_if_it_is_ok(env):
    cfg, _, _ = env
    manual_stop(cfg)
    for day in ("2026-11-06", "2026-11-13"):
        path = cfg.outbox_dir / f"{day}-080001.json"
        outbox.write_atomic(path, {"push": True, "text": NOTICE})
        if day == "2026-11-06":
            receipt(path, day=day)
    sync(env, "2026-11-13")
    assert stopped_date(cfg) is None


@pytest.mark.parametrize("first", [False, True])
def test_production_receipt_names_and_first_issue_priority_use_only_primary_files(tmp_path, first):
    cfg = Config(state_dir=tmp_path)
    with db.connect(cfg.db_path) as conn:
        db.sync_sources(conn, [SOURCES["ts_web"]])
        conn.execute("UPDATE source SET crawl_status='已停抓',stop_reason='异议' WHERE id='ts_web'")
        main = cfg.outbox_dir / "2026-11-06.json"
        outbox.write_atomic(main, {"push": True, "text": OBJECTION})
        receipt(main)
        if first:
            main = cfg.outbox_dir / "2026-11-06-first.json"
            outbox.write_atomic(main, {"push": True, "text": OBJECTION})
        pipeline._record_ts_notice(conn, cfg, FRIDAY)
        assert conn.execute("SELECT stop_notice_date FROM source").fetchone()[0] == (None if first else "2026-11-06")
        if first:
            receipt(main)
            pipeline._record_ts_notice(conn, cfg, FRIDAY)
            assert conn.execute("SELECT stop_notice_date FROM source").fetchone()[0] == "2026-11-06"


@pytest.mark.parametrize("failure", [False, True])
def test_52_4_unsent_rerun_updates_snapshot_and_sent_rerun_and_resend_keep_bytes(env, monkeypatch, failure):
    cfg, fixture, _ = env
    write_fixture(fixture, "2026-11-06", new=["Snapshot Company"])
    _, first = run_at(cfg, fixture, "2026-11-06", no_deep=True)
    before = row(cfg)
    eid = json.loads(before["event_ids_json"])[0]
    with db.connect(cfg.db_path) as conn:
        added = extra_source(conn, eid)
    _, second = run_at(cfg, fixture, "2026-11-06", no_deep=True)
    frozen = row(cfg)["card_sources_json"]
    assert first != second and frozen != before["card_sources_json"]
    assert any(s["url"] == added for s in json.loads(frozen)[str(eid)])
    receipt(second)
    old_bytes = second.read_bytes()
    with db.connect(cfg.db_path) as conn:
        extra_source(conn, eid, source_id="cbn_main", name="Crunchbase News")
    if failure:
        def fail(*args, **kwargs):
            raise RuntimeError("合成失败路径")
        monkeypatch.setattr(fetcher, "fetch_all", fail)
        assert pipeline.run(cfg, arguments(fixture, no_deep=True), pipeline.Clock(FRIDAY, day_time(8, 45))) == 1
    else:
        run_at(cfg, fixture, "2026-11-06", no_deep=True)
    assert row(cfg)["card_sources_json"] == frozen and second.read_bytes() == old_bytes
    run_at(cfg, fixture, "2026-11-06", resend=True, dry_run=True)
    assert row(cfg)["card_sources_json"] == frozen
    if not failure:
        write_fixture(fixture, "2026-11-09")
        run_at(cfg, fixture, "2026-11-09", no_deep=True)
        assert row(cfg, "2026-11-09")["card_sources_json"] == "{}"


@pytest.mark.parametrize("flags", [["--test"], ["--to", "test"], ["--sync-sent", "--test", "--date", "2026-11-06"],
                                   ["--test", "--assume-review-site-passed"]])
def test_test_flag_parses_and_uses_single_test_isolation_contract(flags):
    args = cli.parse_args(flags)
    assert args.test_flags and config.is_test_run(args, Config(test_mode=False))


@pytest.mark.parametrize("kind", ["a", "b", "c"])
def test_48_3_historical_bounds_migrate_on_cli_run_and_keep_original_rows(env, kind):
    cfg, _, _ = env
    text = "不详" if kind == "c" else "2026-10-23 17:28 ~ 2026-10-30 19:56（北京时间）"
    expected = ("2026-10-23T17:28:00+08:00", "2026-10-30T19:56:00+08:00")
    with db.connect(cfg.db_path) as conn:
        for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"):
            scan.upsert_digest(conn, {"date": day, "push": False, "status": "没有新增"})
            outbox.write_atomic(cfg.outbox_dir / f"{day}-120000.json", {"push": False})
        conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,result,missed_range,missed_start,missed_end) "
                     "VALUES ('r14f-history','strictlyvc','2026-11-06T08:40:00+08:00','2026-11-06T08:41:00+08:00','成功',?,?,?)",
                     (text, *(expected if kind == "a" else (None, None))))
    sync(env)
    with sqlite3.connect(cfg.db_path) as conn:
        actual = conn.execute("SELECT missed_start,missed_end,missed_range FROM fetch_run WHERE run_id='r14f-history'").fetchone()
        assert actual == (*(expected if kind != "c" else (None, None)), text)
    args = arguments(preview_push=True, test=True, no_links=True)
    assert pipeline.preview_push(cfg, args, pipeline.Clock(FRIDAY, day_time(8, 45))) == 0
    path = max(cfg.outbox_dir.glob("2026-11-06-preview-*.json"), key=lambda p: p.stat().st_mtime_ns)
    preview = json.loads(path.read_text())
    if kind == "c":
        assert preview["alarm"]
        assert preview["text"].splitlines()[1] == "StrictlyVC 没抓到：有一段时间的内容没补上。本周汇总可能不全，不代表没有新公司。"
    else:
        assert not preview["alarm"] and "StrictlyVC 没抓到" not in preview["text"]
    sync(env)
    with db.connect(cfg.db_path) as conn:
        assert tuple(conn.execute("SELECT missed_start,missed_end,missed_range FROM fetch_run WHERE run_id='r14f-history'").fetchone()) == actual
        assert db.migrate_missed_bounds(conn) == ((1, 0) if kind == "c" else (0, 0))
        assert scan.failed_days_in_window(conn, BASELINE, FRIDAY, outbox_dir=cfg.outbox_dir) == 0
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "15"


@pytest.mark.parametrize("start,expected", [("2026-11-04T19:56:00+08:00", True), ("2026-10-28T09:00:00+08:00", False)])
def test_48_3a_start_only_history_is_preserved_and_filtered_by_baseline(env, start, expected):
    cfg, _, _ = env
    with db.connect(cfg.db_path) as conn:
        conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,result,missed_range,missed_start) "
                     "VALUES ('r14f-start','strictlyvc','2026-11-06T08:40:00+08:00','2026-11-06T08:41:00+08:00','成功','手写仅起点',?)", (start,))
    sync(env)
    assert pipeline.preview_push(cfg, arguments(preview_push=True, test=True, no_links=True), pipeline.Clock(FRIDAY, day_time(8, 45))) == 0
    path = max(cfg.outbox_dir.glob("2026-11-06-preview-*.json"), key=lambda p: p.stat().st_mtime_ns)
    payload = json.loads(path.read_text())
    assert payload["alarm"] is expected
    if expected:
        assert payload["text"].splitlines()[1] == "StrictlyVC 没抓到：11-04 19:56 之后的内容没补上。本周汇总可能不全，不代表没有新公司。"


def test_missed_migration_counts_skips_whitespace_preserves_partial_bounds_and_is_idempotent(tmp_path):
    with db.connect(tmp_path / "migration.db") as conn:
        db.sync_sources(conn, [SOURCES["strictlyvc"]])
        samples = [("2026-10-23 17:28 ~ 2026-10-30 19:56（北京时间）", None, None),
                   ("不详", None, None), (" \t\n　", None, None),
                   ("2026-10-23 17:28 ~ 2026-10-30 19:56（北京时间）", "保留原起点", None),
                   ("11-04 19:56 之前", None, None)]
        conn.executemany("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,result,missed_range,missed_start,missed_end) "
                         "VALUES ('r14f-migrate','strictlyvc','2026-11-06T08:40:00+08:00','2026-11-06T08:41:00+08:00','成功',?,?,?)", samples)
        assert db.migrate_missed_bounds(conn) == (3, 2)
        assert db.migrate_missed_bounds(conn) == (1, 0)
        rows = [tuple(r) for r in conn.execute("SELECT missed_range,missed_start,missed_end FROM fetch_run ORDER BY id")]
        assert [r[0] for r in rows] == [r[0] for r in samples]
        assert rows[3][1:] == ("保留原起点", None)
        assert rows[-1][1:] == (None, "2026-11-04T19:56:00+08:00")


def test_connect_missed_migration_follows_v17_in_same_transaction_and_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    text = "2026-10-23 17:28 ~ 2026-10-30 19:56（北京时间）"
    with db.connect(path) as conn:
        db.sync_sources(conn, [SOURCES["strictlyvc"]])
        conn.execute("ALTER TABLE fetch_run DROP COLUMN missed_start")
        conn.execute("ALTER TABLE fetch_run DROP COLUMN missed_end")
        conn.execute("UPDATE meta SET value='14' WHERE key='schema_version'")
        conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,result,missed_range) "
                     "VALUES ('r14f-legacy','strictlyvc','2026-11-02T08:40:00+08:00','2026-11-02T08:41:00+08:00','成功',?)", (text,))
    original_v17, original_bounds = db.migrate_v17, db.migrate_missed_bounds
    observed = []

    def v17(conn):
        original_v17(conn)
        assert conn.in_transaction
        observed.append("v17")

    def bounds(conn):
        assert conn.in_transaction and observed == ["v17"]
        assert original_bounds(conn) == (1, 1)
        observed.append("bounds")
        raise RuntimeError("合成迁移中断")

    monkeypatch.setattr(db, "migrate_v17", v17)
    monkeypatch.setattr(db, "migrate_missed_bounds", bounds)
    with pytest.raises(RuntimeError, match="合成迁移中断"):
        db.connect(path)
    assert observed == ["v17", "bounds"]
    with sqlite3.connect(path) as conn:
        assert not {"missed_start", "missed_end"} & {r[1] for r in conn.execute("PRAGMA table_info(fetch_run)")}
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "14"
        assert conn.execute("SELECT missed_range FROM fetch_run").fetchone()[0] == text
    monkeypatch.setattr(db, "migrate_v17", original_v17)
    monkeypatch.setattr(db, "migrate_missed_bounds", original_bounds)
    with db.connect(path) as conn:
        assert tuple(conn.execute("SELECT missed_start,missed_end FROM fetch_run").fetchone()) == (
            "2026-10-23T17:28:00+08:00", "2026-10-30T19:56:00+08:00")


@pytest.mark.parametrize("boundary,known,expected_start,expected_end,valid", [
    (datetime(2026, 11, 4, 19, 56, tzinfo=BJ), None, None, datetime(2026, 11, 4, 19, 56, tzinfo=BJ), True),
    (None, datetime(2026, 11, 4, 19, 56, tzinfo=BJ), datetime(2026, 11, 4, 19, 56, tzinfo=BJ), None, True),
    (datetime(2026, 10, 28, 9, tzinfo=BJ), None, None, None, False),
    (None, datetime(2026, 10, 28, 9, tzinfo=BJ), None, None, False),
    (datetime(2026, 11, 1, 9, tzinfo=BJ), datetime(2026, 10, 29, 10, tzinfo=BJ), BASELINE, datetime(2026, 11, 1, 9, tzinfo=BJ), True),
    (None, None, None, None, True),
])
def test_mark_missed_boundary_is_end_and_known_is_start(boundary, known, expected_start, expected_end, valid):
    state = SourceState("tc_venture", baseline_at=BASELINE)
    session = fetcher._Session(SOURCES["tc_venture"], state, None, None, datetime(2026, 11, 6, 8, 40, tzinfo=BJ), None, None)
    session.mark_missed(boundary, known)
    assert bool(session.stats.missed) is valid
    assert (session.stats.missed_start, session.stats.missed_end) == (expected_start, expected_end)
    if valid and (expected_start is None) != (expected_end is None):
        source = SourceStatusLine("TechCrunch", True, False, "", missed_range=session.stats.missed,
                                  missed_start=expected_start, missed_end=expected_end)
        data = DigestData(FRIDAY, BASELINE, datetime(2026, 11, 6, 8, 40, tzinfo=BJ), "没有新增", sources=[source], baseline_at=BASELINE)
        suffix = "之前" if expected_end else "之后"
        assert render.fetch_warning(data) == "TechCrunch 没抓到：11-04 19:56 " + suffix + "的内容没补上。本周汇总可能不全，不代表没有新公司。"


def test_rss_backfill_without_previous_latest_has_only_end(tmp_path):
    source = SOURCES["tc_venture"]
    overrides = {source.url: {"content_type": "text/xml", "body": rss(("one", "2026-11-06T00:00:00Z"))},
                 source.url + "?paged=2": {"content_type": "text/xml", "body": rss(("two", "2026-11-05T00:00:00Z"))},
                 source.url + "?paged=3": {"content_type": "text/xml", "body": rss(("three", "2026-11-04T00:00:00Z"))}}
    outcome, _ = fetch(tmp_path, "tc_venture", overrides, state=SourceState("tc_venture", last_ok=False, baseline_at=BASELINE),
                       now=datetime(2026, 11, 6, 8, 40, tzinfo=BJ))
    assert outcome.ok and outcome.missed_start is None
    assert outcome.missed_end == datetime(2026, 11, 4, 8, tzinfo=BJ)
