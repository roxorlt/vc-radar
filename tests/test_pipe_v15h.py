"""V15f/g/h 的生产守卫、首期事务和禁用名交付验收。"""
from datetime import date, time as daytime
import json
from pathlib import Path
import time

import pytest

from test_pipe_pipeline import SOURCES, arguments, offline, rows, run_at, write_fixture
from vcradar import cli, db, outbox, pipeline, scan
from vcradar.config import Config
from vcradar.guard import check_no_fictional
from vcradar.replay import run_replay


def clock(day="2026-10-30", at="08:40"):
    return pipeline.Clock(date.fromisoformat(day), daytime.fromisoformat(at))


def snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def seed_empty(cfg):
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, list(SOURCES.values()))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("test_mode", [False, True])
def test_empty_production_rules_reject_before_creating_any_files(tmp_path, test_mode, capsys):
    cfg = Config(state_dir=tmp_path / "state", test_mode=test_mode)
    args = arguments(prod_rules=test_mode, dry_run=test_mode)
    assert pipeline.run(cfg, args, clock()) == 3
    assert "库为空，请先 --first-issue" in capsys.readouterr().err
    assert not cfg.state_dir.exists()


@pytest.mark.parametrize("business_table", ["raw_item", "company", "funding_event"])
def test_first_issue_rejects_each_nonempty_business_table_without_writing(tmp_path, business_table, capsys):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture")
    seed_empty(cfg)
    conn = db.connect(cfg.db_path)
    if business_table == "raw_item":
        conn.execute("INSERT INTO raw_item(source_id, external_id, url, title, first_seen_at) "
                     "VALUES ('tc_venture', 'one', 'https://synthetic.example/one', '手写条目', '2026-10-30 08:40:00')")
    else:
        conn.execute("INSERT INTO company(id, name_key, name, first_seen) VALUES ('aster', 'aster', 'Aster', '2026-10-30')")
        if business_table == "funding_event":
            conn.execute("INSERT INTO funding_event(company_id, round_norm, event_date, first_seen) "
                         "VALUES ('aster', '种子轮', '2026-10-30', '2026-10-30 08:40:00')")
    conn.commit()
    conn.close()
    before = snapshot(cfg.state_dir)
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 3
    assert snapshot(cfg.state_dir) == before
    assert "库不为空，不能再跑 --first-issue" in capsys.readouterr().err


@pytest.mark.parametrize("receipt_only", [False, True])
@pytest.mark.parametrize("test_mode", [False, True])
def test_first_day_guards_look_for_both_first_json_and_receipt(tmp_path, receipt_only, test_mode, capsys):
    cfg = Config(state_dir=tmp_path / "state", test_mode=test_mode)
    seed_empty(cfg)
    first = outbox.first_path(cfg.outbox_dir, date(2026, 10, 30))
    if test_mode:
        first = first.with_name("2026-10-30-010203-first.json")
    target = first.with_suffix(".sent") if receipt_only else first
    outbox.write_atomic(target, {"sent_at": "2026-10-30 08:49:00", "result": "ok"} if receipt_only else {"push": True})
    before = snapshot(cfg.state_dir)
    assert pipeline.run(cfg, arguments(prod_rules=test_mode, dry_run=test_mode), clock()) == 3
    assert "今天是首期日，只允许 --resend" in capsys.readouterr().err
    assert snapshot(cfg.state_dir) == before


def test_fixture_without_prod_rules_can_run_empty_and_first_day(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    payload, _ = run_at(cfg, fixture, "2026-10-26")
    assert payload["push"] is False and rows(cfg, "raw_item")
    outbox.write_atomic(cfg.outbox_dir / "2026-10-26-010203-first.json", {"push": True, "generated_at": "2026-10-26 08:40:00"})
    assert pipeline.run(cfg, arguments(fixture), clock("2026-10-26")) == 0


def test_first_day_allows_sync_deep_and_separately_numbered_resends(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-28")
    _, first = run_at(cfg, fixture, "2026-10-28", first_issue=True)
    original = rows(cfg, "digest")[0]
    original_bytes = first.read_bytes()
    outbox.write_atomic(first.with_suffix(".sent"), {"sent_at": "2026-10-28 08:49:00", "result": "ok"})

    def no_fetch(*args, **kwargs):
        raise AssertionError("首期当天的重发不应重新抓取")

    monkeypatch.setattr(pipeline.fetcher, "fetch_all", no_fetch)
    assert pipeline.run(cfg, arguments(fixture, sync_sent=True, prod_rules=True), clock("2026-10-28", "09:10")) == 0
    assert pipeline.run(cfg, arguments(fixture, deep_only=True, prod_rules=True), clock("2026-10-28", "10:29")) == 0
    outbox.write_atomic(cfg.outbox_dir / "2026-10-28-resend-9.json", {})
    for _ in range(2):
        assert pipeline.run(cfg, arguments(fixture, resend=True, prod_rules=True), clock("2026-10-28", "10:29")) == 0
    files = sorted(cfg.outbox_dir.glob("2026-10-28-first-resend-*.json"))
    assert [path.name for path in files] == ["2026-10-28-first-resend-1.json", "2026-10-28-first-resend-2.json"]
    assert all("重发" in json.loads(path.read_text())["text"].splitlines()[2].split(" · ") for path in files)
    assert first.read_bytes() == original_bytes
    for number, stamp in [(2, "2026-10-28 12:00:00"), (10, "2026-10-28 10:00:00")]:
        outbox.write_atomic(cfg.outbox_dir / f"2026-10-28-first-resend-{number}.sent", {"sent_at": stamp, "result": "ok"})
    assert pipeline.run(cfg, arguments(sync_sent=True, prod_rules=True, dry_run=True), clock("2026-10-28")) == 0
    digest = rows(cfg, "digest")[0]
    assert digest["push"] == 1 and digest["sent_at"] == "2026-10-28 08:49:00"
    assert digest["resend_count"] == 2 and digest["resend_sent_at"] == "2026-10-28 10:00:00"
    for key in ("text", "generated_at", "window_start", "event_ids_json", "status"):
        assert digest[key] == original[key]


@pytest.mark.parametrize("failure", ["timeout", "all_failed", "stopped_only"])
def test_incomplete_or_zero_success_first_issue_rolls_back_and_can_retry(tmp_path, failure):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True, total_timeout_s=.03 if failure == "timeout" else 600)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30", delay=.3 if failure == "timeout" else 0,
                            fail=list(SOURCES) if failure == "all_failed" else [sid for sid in SOURCES if sid != "ts_web"] if failure == "stopped_only" else [],
                            stopped=failure == "stopped_only")
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 1
    assert not cfg.db_path.exists() and not list(cfg.outbox_dir.glob("*.json"))
    assert not list(cfg.state_dir.rglob("*.first-tmp.db*")) and not list(cfg.state_dir.rglob(".first-stage-*"))
    assert not (cfg.state_dir / "test-site").exists()
    cfg.total_timeout_s = 600
    write_fixture(fixture, "2026-10-30")
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 0
    assert rows(cfg, "digest")[0]["push"] == 1


@pytest.mark.parametrize("fault", ["first_file", "digest", "replace_db", "publish_first", "export"])
def test_first_issue_failure_after_partial_progress_restores_original_files(tmp_path, monkeypatch, fault):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30")
    seed_empty(cfg)
    with pipeline.run_lock(cfg):
        pass
    sentinel = cfg.state_dir / "test-site" / "previous" / "data.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text('{"company":"Aster"}')
    before = snapshot(cfg.state_dir)
    original_writer = outbox.write_first
    original_digest = scan.upsert_digest
    original_replace = pipeline.os.replace
    original_run = pipeline._run_connected

    def writer(*args, **kwargs):
        original_writer(*args, **kwargs)
        raise OSError("合成首期文件写后故障")

    def digest(*args, **kwargs):
        original_digest(*args, **kwargs)
        if kwargs.get("first_issue"):
            raise OSError("合成 digest 写后故障")

    def replace_file(source, target):
        target = Path(target)
        if (fault == "replace_db" and target == cfg.db_path) or (fault == "publish_first" and target.parent == cfg.outbox_dir):
            raise OSError("合成原子替换故障")
        return original_replace(source, target)

    def export(*args, **kwargs):
        result = original_run(*args, **kwargs)
        stage_cfg = args[1]
        path = stage_cfg.state_dir / "test-site" / "2026-10-30" / "0930" / "data.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"company":"Aster"}')
        raise OSError("合成导出后故障")

    if fault == "first_file":
        monkeypatch.setattr(outbox, "write_first", writer)
    elif fault == "digest":
        monkeypatch.setattr(scan, "upsert_digest", digest)
    elif fault == "export":
        monkeypatch.setattr(pipeline, "_run_connected", export)
    else:
        monkeypatch.setattr(pipeline.os, "replace", replace_file)
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 1
    assert snapshot(cfg.state_dir) == before
    with pipeline._read_database(cfg.db_path) as conn:
        assert db.is_empty(conn)


def test_first_delivery_report_notes_and_push_include_weekday_issue(tmp_path, capsys):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-28", fail=["tc_venture"])
    payload, first = run_at(cfg, fixture, "2026-10-28", first_issue=True)
    output = capsys.readouterr().out
    assert first.name.endswith("-first.json") and first.name.split("-")[-2].isdigit()
    assert payload["push"] is True and rows(cfg, "digest")[0]["push"] == 1
    for label in ("基线条数：", "首期卡片数：", "首行文本行 1：", "首行文本行 2：", "未补全的信源和日期：", "P0-9 备注：", "首期 digest（发送时间/条数/结果）：", "-first.json 路径："):
        assert label in output
    assert str(first) in output and "TechCrunch" in output and "2026-10-26" in output
    assert "TechCrunch" in rows(cfg, "digest")[0]["note"]
    assert not check_no_fictional(payload["text"])


def test_manual_first_issue_can_run_after_deadline_and_on_weekend(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-31")
    args = cli.parse_args(["--fixture", str(fixture), "--first-issue", "--now", "18:00", "--no-deep"])
    assert pipeline.run(cfg, args, clock("2026-10-31", "18:00")) == 0
    assert rows(cfg, "digest")[0]["push"] == 1 and rows(cfg, "fetch_run")


@pytest.mark.parametrize("name", ["Quorvia", "qUoRvIa", "Tessaline", "TESSALINE"])
def test_forbidden_name_in_first_issue_rolls_back_every_delivery(tmp_path, name):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30", new=[name])
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 1
    assert not cfg.db_path.exists() and not list(cfg.outbox_dir.glob("*.json"))
    assert not list(cfg.state_dir.rglob("*.first-tmp.db*"))


def test_regular_run_and_replay_reject_forbidden_names_before_outbox(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    run_at(cfg, fixture, "2026-10-26")
    write_fixture(fixture, "2026-10-27", new=["Tessaline"])
    assert pipeline.run(cfg, arguments(fixture), clock("2026-10-27")) == 1
    assert not list(cfg.outbox_dir.glob("2026-10-27*.json"))
    with pipeline._read_database(cfg.db_path) as conn:
        assert not check_no_fictional("\n".join(conn.iterdump()))
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"date": "2026-10-30", "items": [{"source_id": "tc_venture", "external_id": "bad", "url": "https://synthetic.example/bad", "title": "Quorvia raises $2M in Seed funding", "excerpt": "Quorvia is a Boston-based company."}]}))
    assert run_replay(cfg, arguments(replay=replay), clock()) == 1
    assert not list(cfg.outbox_dir.glob("2026-10-30*.json"))


def test_export_guard_removes_staged_site_and_preserves_previous_site(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30")
    original = pipeline._run_connected

    def export(*args):
        result = original(*args)
        stage_cfg = args[1]
        page = stage_cfg.state_dir / "test-site" / "data.json"
        page.parent.mkdir(parents=True)
        page.write_text('{"name":"quorvia"}')
        return result

    monkeypatch.setattr(pipeline, "_run_connected", export)
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 1
    assert not cfg.db_path.exists() and not list(cfg.outbox_dir.glob("*.json"))
    assert not (cfg.state_dir / "test-site").exists()


def test_replay_forwards_fixed_ingestion_time_to_core(tmp_path, monkeypatch):
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"date": "2026-10-30", "items": [{"source_id": "tc_venture", "external_id": "aster", "url": "https://synthetic.example/aster", "title": "Aster raises $2M in Seed funding", "excerpt": "Aster is a Boston-based company.", "first_ingested_at": "2026-10-20 08:40:01"}]}))
    original = scan.ingest
    received = []

    def ingest(*args, **kwargs):
        event_id = original(*args, **kwargs)
        received.append(scan.card_sort_data(args[0], event_id)[1].strftime("%Y-%m-%d %H:%M:%S"))
        return event_id

    monkeypatch.setattr(scan, "ingest", ingest)
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    assert run_replay(cfg, arguments(replay=replay), clock()) == 0
    assert received == ["2026-10-20 08:40:01"]
    assert not list(cfg.state_dir.rglob("*.db"))


def test_replay_prod_rules_and_cli_switch(tmp_path):
    replay = tmp_path / "replay.json"
    replay.write_text('{"date":"2026-10-30","items":[]}')
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    assert cli.parse_args(["--prod-rules", "--dry-run"]).prod_rules
    assert run_replay(cfg, arguments(replay=replay, prod_rules=True), clock()) == 3
    assert not cfg.state_dir.exists()
    assert run_replay(cfg, arguments(replay=replay), clock()) == 0


def test_fallback_day_counts_even_with_healthy_digest(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    run_at(cfg, fixture, "2026-10-26", first_issue=True)
    for day in ("2026-10-27", "2026-10-28", "2026-10-29"):
        write_fixture(fixture, day)
        run_at(cfg, fixture, day)
    missing = next(cfg.outbox_dir.glob("2026-10-28-??????.json"))
    missing.unlink()
    outbox.write_atomic(cfg.outbox_dir / "2026-10-28.sent", {"sent_at": "2026-10-28 09:00:00", "result": "ok", "note": "兜底：没有交接文件"})
    conn = db.connect(cfg.db_path)
    try:
        assert scan.failed_days_in_window(conn, None, date(2026, 10, 30), outbox_dir=cfg.outbox_dir) == 1
    finally:
        conn.close()


def test_forbidden_name_check_is_case_insensitive_and_deduplicated():
    assert check_no_fictional("quorvia TESSALINE Quorvia") == ["Quorvia", "Tessaline"]
    assert check_no_fictional("Aster raises seed funding.") == []


@pytest.mark.parametrize("phase", ["render", "write_first"])
def test_first_timeout_during_delivery_rolls_back(tmp_path, monkeypatch, phase):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True, total_timeout_s=.1)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-26", old=True)
    owner, name = (pipeline.render, "render_push") if phase == "render" else (outbox, "write_first")
    original = getattr(owner, name)

    def slow(*args, **kwargs):
        time.sleep(.11)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, slow)
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock("2026-10-26")) == 1
    assert not cfg.db_path.exists() and not list(cfg.outbox_dir.glob("*.json"))
    assert not list(cfg.state_dir.rglob("*.first-tmp.db*"))
