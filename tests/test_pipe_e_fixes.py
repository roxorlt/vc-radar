"""E1/2/5～14：仅使用手写 fixture 的入口和交付回归。"""
from datetime import date, datetime, time as daytime
import json
import subprocess

import pytest

from test_pipe_pipeline import SOURCES, arguments, offline, rows, run_at, write_fixture
from vcradar import cli, db, http, outbox, pipeline, render
from vcradar.config import BJ, Config
from vcradar.models import DigestData, ErrorType, SourceStatusLine


def clock(day="2026-10-26", at="08:40"):
    return pipeline.Clock(date.fromisoformat(day), daytime.fromisoformat(at))


def enable_only(cfg, source_id):
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, list(SOURCES.values()))
    conn.execute("UPDATE source SET enabled=(id=?)", (source_id,))
    conn.commit()
    conn.close()


def test_e1_fixture_default_accept_and_all_json_fetches_explicit_accept(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    original = http.FixtureTransport
    clients = []

    def capture(path):
        client = original(path)
        clients.append(client)
        return client

    monkeypatch.setattr(http, "FixtureTransport", capture)
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 0
    requests = clients[0].request_log
    json_requests = [entry for entry in requests if any(
        entry["url"].startswith(SOURCES[sid].url) for sid in ("yc_launches", "launch_hn", "ts_web"))
        or "-dsn.algolia.net/1/" in entry["url"]]
    assert json_requests and all(entry["headers"]["Accept"] == "application/json" for entry in json_requests)
    assert all(entry["headers"]["Accept"] == "*/*" for entry in requests if entry["url"].endswith("/robots.txt"))
    assert http._headers({"accept": "text/html"})["accept"] == "text/html"
    assert len([key for key in http._headers({"accept": "text/html"}) if key.lower() == "accept"]) == 1


def test_e2_e9_manual_first_at_2300_has_no_not_run_sources(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True, run_deadline="08:50")
    fixture = write_fixture(tmp_path / "fixture", old=True)
    args = cli.parse_args(["--fixture", str(fixture), "--first-issue", "--date", "2026-10-26", "--now", "23:00"])
    assert pipeline.run(cfg, args, clock(at="23:00")) == 0
    assert len(rows(cfg, "fetch_run")) == 11
    assert all(row["result"] == "成功" for row in rows(cfg, "fetch_run"))


def test_e9_manual_first_small_total_timeout_still_rolls_back(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", delay=.2, old=True)
    assert pipeline.run(cfg, arguments(fixture, first_issue=True, total_timeout=.03), clock(at="23:00")) == 1
    assert not cfg.db_path.exists() and not list(cfg.outbox_dir.glob("*.json"))


def test_e5b_resend_is_ordered_hint_after_early_push_and_before_alarm():
    data = DigestData(date(2026, 9, 30), None, datetime(2026, 9, 30, 8, 40, tzinfo=BJ), "部分失败",
                      baseline_count=2, sources=[SourceStatusLine("TechCrunch", False, False, "0 条", ErrorType.UNREACHABLE)])
    original = render.render_push(data, early_push=True, incomplete_until=date(2026, 9, 29))
    text = render.add_resend_notice(original)
    assert text.splitlines()[:2] == original.splitlines()[:2]
    hints = text.splitlines()[2].split(" · ")
    assert hints[:4] == ["首次运行收录 2 家以前的公司，不算新增", "本周一到 09-29 日的内容没抓全", "提前推送", "重发"]
    assert hints[4].startswith("TechCrunch 没抓到")
    assert render.add_resend_notice(text) == text


@pytest.mark.parametrize("status", ["部分失败", "全部失败", "没跑完"])
def test_e5c_zero_cards_keep_count_alarm_links_sources_and_previous_summary(status):
    sources = [SourceStatusLine(source.name, source.id != "tc_venture", False, "1 条", ErrorType.UNREACHABLE if source.id == "tc_venture" else None)
               for source in SOURCES.values() if source.id not in {"tc_startups", "cbn_seed", "cbn_venture"}]
    data = DigestData(date(2026, 10, 30), None, datetime(2026, 10, 30, 8, 40, tzinfo=BJ), status,
                      sources=sources, last_push_summary=(date(2026, 10, 23), 2),
                      base_url="https://review.example/weekly", review_site_passed=True)
    lines = render.render_push(data).splitlines()
    assert lines[1] == "本周没有发现新的天使轮、种子轮、A 轮融资。"
    assert lines[2] and ("没抓到" in lines[2] or "没抓完" in lines[2])
    assert lines[3].startswith("已接入 8 个来源网站，其中 1 个出错")
    assert lines[4] == "上一次有新公司是 10-23 那期，2 家"
    assert "看本周全部 0 家" in lines[5] and "#week" in lines[5]


def test_e6_zero_baseline_has_no_hint_and_report_does_not_mistake_card_for_hint(tmp_path, capsys):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture")
    enable_only(cfg, "tc_venture")
    payload, _ = run_at(cfg, fixture, "2026-10-26", first_issue=True)
    assert "作为基线" not in payload["text"]
    assert payload["text"].splitlines()[2].startswith("1. Anchor")
    report = capsys.readouterr().out
    assert "首行文本行 2：\n" in report


@pytest.mark.parametrize("selected", ["tc_venture", "cbn_main", "ts_web"])
def test_e10_one_address_success_is_enough_for_one_successful_source(tmp_path, selected):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True, fail=[sid for sid in SOURCES if sid != selected])
    payload, _ = run_at(cfg, fixture, "2026-10-26", first_issue=True)
    assert payload["push"] is True
    digest = rows(cfg, "digest")[0]
    assert digest["status"] == "基线" and digest["note"]
    if selected in {"tc_venture", "cbn_main"}:
        assert SOURCES[selected].name in json.loads(digest["error_sources"])


def test_e6_e10_stopped_term_sheet_does_not_count_as_success(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", stopped=True, fail=[sid for sid in SOURCES if sid != "ts_web"])
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 1
    assert not cfg.db_path.exists() and not list(cfg.outbox_dir.glob("*.json"))


def test_e11_e12_delivery_has_six_fields_pending_sent_time_and_failure_notes(tmp_path, capsys):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", fail=["tc_venture"])
    _, path = run_at(cfg, fixture, "2026-10-26", first_issue=True)
    output = capsys.readouterr().out
    for label in ("基线条数：", "首期卡片数：", "首行文本行 1：", "首行文本行 2：", "未补全的信源和日期：", "-first.json 路径：", "P0-9 备注："):
        assert label in output
    assert "待 --sync-sent" in output and str(path) in output
    assert "TechCrunch" in output and "2026-10-26" in output
    digest = rows(cfg, "digest")[0]
    assert digest["status"] == "基线" and digest["push"] == 1
    assert "TechCrunch" in digest["note"] and "页面打不开" in digest["note"]


def test_e11_production_first_never_launches_extra_validation_commands(tmp_path, monkeypatch):
    from test_pipe_edges import production_run, production_transport
    cfg = Config(state_dir=tmp_path / "state")
    fixture = write_fixture(tmp_path / "fixture", old=True)
    production_transport(monkeypatch, fixture)

    def forbidden(*args, **kwargs):
        raise AssertionError("生产首期不启动额外验证命令")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    production_run(cfg, "2026-10-26", first_issue=True)
    assert outbox.first_path(cfg.outbox_dir, date(2026, 10, 26)).exists()


def test_e12_empty_fixture_ordinary_run_uses_ordinary_status(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    run_at(cfg, fixture, "2026-10-26")
    assert rows(cfg, "digest")[0]["status"] == "没有新增"


@pytest.mark.parametrize("argv", [["--prod-rules", "--first-issue"], ["--prod-rules", "--first-issue", "--to", "test"],
    ["--prod-rules", "--first-issue", "--dry-run"], ["--prod-rules", "--first-issue", "--fixture", "missing"],
    ["--prod-rules", "--first-issue", "--replay", "missing"]])
def test_e8_invalid_prod_rules_fail_before_any_files(tmp_path, argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv + ["--config", str(tmp_path / "missing.toml")])
    assert exc.value.code == 2 and "--prod-rules" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("option", ["--fixture", "--dry-run", "--replay"])
def test_e8_prod_rules_accept_only_the_three_test_modes(option):
    argv = ["--prod-rules", option] + ([] if option == "--dry-run" else ["synthetic"])
    assert cli.parse_args(argv).prod_rules


def test_e8_prod_rules_recognize_first_file_for_selected_date(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture")
    outbox.write_atomic(cfg.outbox_dir / "2026-10-26-custom-first.json", {"push": True})
    assert pipeline.run(cfg, arguments(fixture, prod_rules=True, date=date(2026, 10, 26)), clock()) == 3
    assert not cfg.db_path.exists()


def test_e8_nonempty_test_database_allows_incremental_production_rules(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    run_at(cfg, fixture, "2026-10-26", first_issue=True)
    write_fixture(fixture, "2026-10-27", new=["Birch"])
    run_at(cfg, fixture, "2026-10-27", prod_rules=True)
    assert any(row["name"] == "Birch" for row in rows(cfg, "company"))


def test_e13_next_start_cleans_only_owned_temporary_artifacts(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    temporary = [cfg.db_path.parent / "killed.first-tmp.db", cfg.db_path.parent / "killed.first-tmp.db-wal",
                 cfg.db_path.parent / "killed.first-tmp.db-shm", cfg.db_path.parent / ".first-stage-killed" / "old.out",
                 cfg.outbox_dir / ".2026-10-26-first.json.killed.tmp",
                 cfg.state_dir / "test-site" / ".tmp-export" / "data.json"]
    kept = [cfg.state_dir / "test-site" / "2026-10-19" / "0930" / "data.json",
            cfg.state_dir / "outbox" / "production.json", cfg.raw_dir / "2026-10-26" / "keep.out"]
    for path in temporary + kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("handwritten")
    assert pipeline.run(cfg, arguments(fixture, first_issue=True), clock()) == 0
    assert not any(path.exists() for path in temporary)
    assert all(path.read_text() == "handwritten" for path in kept)


def test_e13_cleanup_waits_for_run_lock(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    with pipeline.run_lock(cfg):
        path = cfg.db_path.parent / "active.first-tmp.db"
        path.write_text("active")
        with pytest.raises(RuntimeError):
            pipeline.run(cfg, arguments(fixture, first_issue=True), clock())
        assert path.read_text() == "active"


def test_e13_rejected_empty_run_also_removes_killed_first_database(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    path = cfg.db_path.parent / "killed.first-tmp.db"
    path.parent.mkdir(parents=True)
    path.write_text("killed")
    assert pipeline.run(cfg, arguments(dry_run=True, prod_rules=True), clock()) == 3
    assert not path.exists() and not cfg.db_path.exists()


def test_e14_reset_removes_all_test_handoffs_and_preserves_production(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    for name in ("2026-10-26-010203-first.json", "2026-10-26-first.sent", "2026-10-26-first-resend-1.json", "left.tmp"):
        path = cfg.outbox_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("handwritten")
    production = cfg.state_dir / "outbox" / "2026-10-26.json"
    production.parent.mkdir(parents=True)
    production.write_text("keep")
    assert pipeline.run(cfg, arguments(reset_test_db=True), clock()) == 0
    assert not cfg.outbox_dir.exists() and production.read_text() == "keep"
