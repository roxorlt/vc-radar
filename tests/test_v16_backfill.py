"""R：V16g 补充闭环及测试隔离；数据全部为手写合成样例。"""
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from hashlib import sha1, sha256
import csv
import json
from pathlib import Path
import sqlite3
import subprocess
import shutil

import pytest

from vcradar import backfill, cli, config, db, fx, http, outbox, pipeline, render, scan, site, wording
from vcradar.config import BJ, Config
from vcradar.models import DigestData, Extracted, HttpResponse, LabelDim, RawItem
from vcradar.sources.registry import P0_SOURCES

NOW = datetime(2026, 10, 31, 8, 40, tzinfo=BJ)
PUSH = datetime(2026, 11, 6, 8, 45, tzinfo=BJ)
FIRST = "2026-10-31"
BASE = "https://radar.test/private"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(backfill, "datetime", FixedDatetime)
    def forbidden(*args, **kwargs):
        raise AssertionError("R 测试禁止网络、真实模型和密钥读取")
    monkeypatch.setattr(config, "load_api_key", forbidden)
    monkeypatch.setattr(http.urllib.request, "urlopen", forbidden)


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def configured(tmp_path, *, test=True):
    links = tmp_path / "links.json"
    links.write_text("[]")
    return Config(state_dir=tmp_path / "state", test_mode=test, links_file=links, base_url=BASE)


def add(conn, name, *, amount="$5M", country="London", status="已纳入", filtered=("总部非美国",),
        baseline=False, round_text="Seed", batch=None, source="tc_venture", event_day="2026-10-29", eid_suffix=""):
    url = "https://news.test/" + scan.name_key(name) + eid_suffix
    item = RawItem(source, url, url, name + " financing", datetime.fromisoformat(event_day + "T12:00:00+08:00"),
                   excerpt=(f"{name} is headquartered in {country}." if country else "A synthetic financing story.") + (f" The company raised {amount}." if amount else ""),
                   structured={"company_name": name, "website": "https://" + scan.name_key(name) + ".test/",
                               **({"yc_batch": batch} if batch else {}),
                               **({"location": country} if source == "yc_directory" else {})})
    ext = Extracted(company_name=name, website=item.structured["website"], round_text=round_text,
                    amount_text=amount, hq_text=country, summary="公司为团队提供软件。产品用于协作。",
                    ai=LabelDim("否"), social=LabelDim("否"))
    raw_id, _ = scan.upsert_raw_item(conn, item, NOW.replace(hour=0, minute=35), baseline)
    eid = scan.ingest(conn, raw_id, item, ext, now=NOW.replace(hour=0, minute=35), is_baseline=baseline,
                      is_backfill=False, latest_yc_batches=["Fall 2026", "Summer 2026", "Spring 2026", "Winter 2026"], model=None)
    conn.execute("UPDATE funding_event SET filter_reasons_json=?,pending=0,push_date=?,last_pushed_date=? WHERE id=?",
                 (encode(list(filtered)), None if baseline else FIRST, None if baseline else FIRST, eid))
    conn.execute("UPDATE company SET status=? WHERE id=(SELECT company_id FROM funding_event WHERE id=?)", (status, eid))
    return eid


def seed(cfg, specs):
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, P0_SOURCES)
    ids = [add(conn, **spec) for spec in specs]
    scan.upsert_digest(conn, {"date": FIRST, "push": True, "status": "基线", "event_ids": [
        eid for eid in ids if not conn.execute("SELECT baseline_unpushed FROM funding_event WHERE id=?", (eid,)).fetchone()[0]],
        "generated_at": "2026-10-31 00:35:00", "text": "首期旧文本"}, first_issue=True)
    conn.execute("UPDATE source SET enabled=0")
    conn.commit()
    conn.close()
    return ids


def rates(cfg, **kwargs):
    return fx.FxRates(cfg, lambda url, **unused: HttpResponse(url, None, error="timeout"), now=NOW, persist=False, **kwargs)


def manifest(cfg, **kwargs):
    with pipeline._read_database(cfg.db_path) as conn:
        return backfill.create_manifest(conn, cfg, now=NOW, real_now=NOW, rates=rates(cfg), **kwargs)


def apply(cfg, data, *, now=NOW, file=None):
    conn = db.connect(cfg.db_path)
    try:
        path = file or cfg.outbox_dir / ("backfill-verify-" + sha256(encode(data).encode()).hexdigest()[:12] + ".json")
        return backfill.apply_manifest(conn, cfg, data, path, now=now, real_now=now,
            balance_client=lambda: {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "10"}]})
    finally:
        conn.close()


def contents(cfg):
    return sha256(cfg.db_path.read_bytes()).hexdigest(), cfg.db_path.stat().st_mtime_ns


def snapshot(cfg, now=NOW):
    with pipeline._read_database(cfg.db_path) as conn:
        return site._snapshot(conn, cfg, now)


def args_for(cfg, *options):
    path = cfg.state_dir.parent / "run.toml"
    path.write_text(f"state_dir={json.dumps(str(cfg.state_dir))}\nlinks_file={json.dumps(str(cfg.links_file))}\nbase_url={json.dumps(BASE)}\n")
    return ["--config", str(path), "--date", str(NOW.date()), *options]


def test_manifest_does_not_change_database_or_rates_and_reports_are_complete(tmp_path, capsys):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Alpha", "amount": "$27M"}, {"name": "Beta", "country": None,
               "status": "总部待核", "filtered": ()}, {"name": "Baseline", "baseline": True},
               {"name": "Late", "round_text": "Series B"}])
    cfg.rates_file.write_text(encode({"source": fx.SOURCE, "rates": {"2026-10-30": {"EUR": 1, "USD": 1.2}}}))
    previous, cache = contents(cfg), cfg.rates_file.read_bytes()
    reports = tmp_path / "reports"
    assert cli.main(args_for(cfg, "--backfill-first-issue-gap", "--dry-run", "--report-dir", str(reports))) == 0
    assert contents(cfg) == previous and cfg.rates_file.read_bytes() == cache
    files = list(cfg.outbox_dir.glob("*.json"))
    assert len(files) == 1 and files[0].name.startswith("backfill-verify-2026-10-31-")
    data = json.loads(files[0].read_text())
    assert data["mode"] == "测试" and len(data["candidates"]) == 4
    assert not list(cfg.outbox_dir.glob("*-backfill.json"))
    rows = list(csv.DictReader((reports / "2026-10-31-backfill-gap.csv").open()))
    md = (reports / "2026-10-31-backfill-gap.md").read_text()
    assert {r["公司名"] for r in rows} == {"Alpha", "Beta", "Baseline", "Late"}
    assert all(r["实际搜索次数"] == "0" for r in rows)
    for phrase in ("重判家数", "进补充段家数", "仍过滤家数", "只作基线家数", "需要搜索的家数", "国家分布", "费用估算", "最坏", "¥0.005"):
        assert phrase in md
    assert next(r for r in rows if r["公司名"] == "Baseline")["会不会进补充段"] == "是"
    assert "重判家数：4" in capsys.readouterr().out
    assert not (cfg.state_dir / "outbox").exists()


def test_default_reports_are_optional_and_production_search_is_explicitly_unavailable(tmp_path, capsys):
    cfg = configured(tmp_path, test=False)
    seed(cfg, [{"name": "Unknown", "country": None, "status": "总部待核", "filtered": ()}])
    assert cli.main(args_for(cfg, "--backfill-first-issue-gap")) == 0
    data = json.loads((cfg.outbox_dir / "backfill-verify-2026-10-31.json").read_text())
    assert data["mode"] == "生产"
    assert data["searches"][-1]["conclusion"]["国家"]["reason"] == "搜索失败"
    assert "查证尚未接入" in capsys.readouterr().out
    assert not list(tmp_path.rglob("*.csv"))


def test_sort_happens_before_verification_and_scope_includes_old_pending_outside_top_ten(tmp_path):
    cfg = configured(tmp_path)
    specs = [{"name": f"Known {i:02}", "amount": f"${i}M"} for i in range(1, 13)]
    specs += [{"name": "Unknown currency", "amount": "5 crore", "event_day": "2026-10-30"},
              {"name": "Unknown B", "amount": None, "country": None, "status": "总部待核", "filtered": ()},
              {"name": "Unknown K", "amount": None, "country": None},
              {"name": "Known city B", "amount": None, "status": "总部待核", "filtered": ()}]
    seed(cfg, specs)
    calls = []
    def verify(c, **kwargs):
        calls.append((c["name"], c["rank"], c["in_supplement"]))
        return {"searches": [], "fields": [], "model_calls": []}
    data = manifest(cfg, verifier=verify)
    ranked = [c for c in data["candidates"] if c["rank"]]
    assert [c["name"] for c in ranked[:10]] == [f"Known {i:02}" for i in range(12, 2, -1)]
    assert ranked[12]["name"] == "Unknown currency"
    assert calls == [("Unknown B", None, False)]
    before = [(c["company_id"], c["rank"], c["in_supplement"]) for c in data["candidates"]]
    changed = manifest(cfg, verifier=lambda c, **kw: {"fields": [{"field": "国家", "result": "已确认"}]})
    assert before == [(c["company_id"], c["rank"], c["in_supplement"]) for c in changed["candidates"]]


def test_tie_sort_uses_event_date_then_case_insensitive_name(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Zulu", "amount": "$10M", "event_day": "2026-10-30"},
               {"name": "bravo", "amount": "$10M", "event_day": "2026-10-30"},
               {"name": "Alpha", "amount": "$10M", "event_day": "2026-10-29"}])
    assert [c["name"] for c in manifest(cfg)["candidates"]] == ["bravo", "Zulu", "Alpha"]


def test_apply_uses_saved_order_and_search_times_is_idempotent_and_preserves_original_time(tmp_path, capsys):
    cfg = configured(tmp_path)
    ids = seed(cfg, [{"name": f"Company {i:02}", "country": None, "amount": f"${i}M"} for i in range(1, 13)])
    def verify(c, **kwargs):
        return {"searches": [{"requested_at": "2026-10-31 08:40:01", "query": c["name"], "status": 200,
                            "result_count": 1, "billed": True, "cost": .005, "results": [{"url": "https://news.test/evidence"}]}],
                "model_calls": [{"cost": .0035}], "fields": [{"field": "国家", "result": "查过仍无法确认",
                            "reason": "搜索没找到", "urls": [], "tiers": [], "quotes": [], "verified_at": "2026-10-31 08:40:02"}]}
    data = manifest(cfg, verifier=verify)
    cfg.outbox_dir.mkdir(parents=True)
    verify_file = cfg.outbox_dir / "backfill-verify-2026-10-31-084000.json"
    outbox.write_atomic(verify_file, data)
    assert cli.main(args_for(cfg, "--backfill-first-issue-gap", "--apply", "--to", "test")) == 0
    assert "使用清单文件：" in capsys.readouterr().out
    record, = cfg.outbox_dir.glob("*-backfill.json")
    saved = json.loads(record.read_text())
    assert saved["kind"] == "backfill" and saved["push"] is False and saved["count"] == 10
    assert saved["verify_file"] == str(verify_file)
    assert len(saved["companies"]) == 12 and [c["rank"] for c in saved["companies"]] == list(range(1, 13))
    with pipeline._read_database(cfg.db_path) as conn:
        events_before = [tuple(r) for r in conn.execute("SELECT * FROM funding_event ORDER BY id")]
        companies = list(conn.execute("SELECT backfill,backfill_push_date FROM company"))
        assert CounterValue(companies, "首期漏掉") == 0 and CounterValue(companies, "补充未推送") == 12
        assert all(c[1] is None for c in companies)
        assert conn.execute("SELECT COUNT(*) FROM verify_search WHERE source='补充命令'").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM verify_log WHERE source='补充命令'").fetchone()[0] == 10
        assert {r[0] for r in conn.execute("SELECT requested_at FROM verify_search")} == {"2026-10-31 08:40:01"}
        assert {r[0] for r in conn.execute("SELECT verified_at FROM verify_log")} == {"2026-10-31 08:40:02"}
        assert json.loads(conn.execute("SELECT event_ids_json FROM digest WHERE date=?", (FIRST,)).fetchone()[0]) == ids
    assert apply(cfg, data, now=NOW + timedelta(days=1), file=verify_file) is None
    assert len(list(cfg.outbox_dir.glob("*-backfill.json"))) == 1
    with pipeline._read_database(cfg.db_path) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM funding_event ORDER BY id")] == events_before
        assert conn.execute("SELECT COUNT(*) FROM funding_event WHERE original_first_ingested_at='2026-10-31 00:35:00'").fetchone()[0] == len(ids)
    assert cli.main(args_for(cfg, "--backfill-first-issue-gap", "--apply", "--dry-run")) == 0
    assert "没有新的补充" in capsys.readouterr().out


def CounterValue(rows, marker):
    return sum(r[0] == marker for r in rows)


@pytest.mark.parametrize("age,success", [(0, True), (1, True), (7, True), (8, False)])
def test_apply_manifest_age_is_by_beijing_calendar_days(tmp_path, age, success, capsys):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Alpha"}])
    data = manifest(cfg)
    data["generated_at"] = (NOW - timedelta(days=age)).strftime("%Y-%m-%d 23:59:59")
    outbox.write_atomic(cfg.outbox_dir / "backfill-verify-any.json", data)
    result = cli.main(args_for(cfg, "--backfill-first-issue-gap", "--apply", "--dry-run"))
    assert (result == 0) is success
    if not success:
        assert "清单文件已超过 7 天，请重跑清单模式" in capsys.readouterr().err
        assert not list(cfg.outbox_dir.glob("*-backfill.json"))


def test_latest_manifest_uses_internal_generation_time_and_production_never_reads_test(tmp_path, capsys):
    cfg = configured(tmp_path, test=False)
    seed(cfg, [{"name": "Alpha"}])
    test = replace(cfg, test_mode=True)
    outbox.write_atomic(test.outbox_dir / "backfill-verify-future.json", {"generated_at": "2026-10-31 09:00:00", "mode": "测试", "candidates": []})
    assert cli.main(args_for(cfg, "--backfill-first-issue-gap", "--apply")) == 1
    assert "没有清单文件，请先跑清单模式" in capsys.readouterr().err
    outbox.write_atomic(cfg.outbox_dir / "backfill-verify-last-name.json", {"generated_at": "2026-10-30 09:00:00", "mode": "生产", "candidates": []})
    outbox.write_atomic(cfg.outbox_dir / "backfill-verify-first-name.json", {"generated_at": "2026-10-31 08:00:00", "mode": "测试", "candidates": []})
    assert cli.main(args_for(cfg, "--backfill-first-issue-gap", "--apply")) == 1
    assert "生产不能使用测试清单" in capsys.readouterr().err


def test_production_record_uses_number_suffix_and_does_not_overwrite(tmp_path):
    cfg = configured(tmp_path, test=False)
    seed(cfg, [{"name": "Alpha"}])
    first = apply(cfg, manifest(cfg))
    original = first.read_bytes()
    conn = db.connect(cfg.db_path)
    add(conn, "Beta")
    conn.commit()
    conn.close()
    second = apply(cfg, manifest(cfg))
    assert second.name == "2026-10-31-backfill-2.json" and first.read_bytes() == original


def test_country_filtered_baseline_is_supplement_but_late_batch_and_round_stay_filtered(tmp_path):
    cfg = configured(tmp_path)
    ids = seed(cfg, [{"name": "Old baseline", "baseline": True}, {"name": "Late batch", "batch": "Fall 2025", "source": "yc_directory"},
                     {"name": "Late round", "round_text": "Series B"}, {"name": "Pending round", "round_text": None},
                     *[{"name": "Batch " + b, "batch": b, "baseline": True, "source": "yc_directory"}
                       for b in ["Winter 2026", "Spring 2026", "Summer 2026", "Fall 2026"]]])
    data = manifest(cfg)
    assert {c["name"] for c in data["candidates"] if c["in_supplement"]} == {"Old baseline", "Pending round", *["Batch " + b for b in ["Winter 2026", "Spring 2026", "Summer 2026", "Fall 2026"]]}
    apply(cfg, data)
    snap = snapshot(cfg)
    assert set(snap["listing"]) == set(ids) - {ids[1], ids[2]}
    with pipeline._read_database(cfg.db_path) as conn:
        baseline, late_batch, late_round, pending = [conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone() for eid in ids[:4]]
        assert baseline["filter_reasons_json"] == "[]" and baseline["original_first_ingested_at"] is not None
        assert json.loads(late_batch["filter_reasons_json"]) == ["批次超过 12 个月"]
        assert json.loads(late_round["filter_reasons_json"]) == ["轮次不在范围"]
        assert pending["round_norm"] == "待核"
        assert len(backfill.collect_supplement(conn, cfg, NOW.date())[0][0].cards) == 6


@pytest.mark.parametrize("passed", [False, True])
def test_pipeline_sends_once_separate_top_tens_and_preview_is_read_only(tmp_path, monkeypatch, passed):
    cfg = replace(configured(tmp_path), review_site_passed=passed)
    seed(cfg, [{"name": f"Extra {i:02}", "amount": f"${i}M"} for i in range(1, 13)])
    data = manifest(cfg)
    apply(cfg, data)
    conn = db.connect(cfg.db_path)
    for i in range(1, 12):
        eid = add(conn, f"Weekly {i:02}", amount=f"${i}M", filtered=(), event_day="2026-11-02")
        conn.execute("UPDATE funding_event SET pending=1,push_date=NULL,last_pushed_date=NULL,first_ingested_at='2026-11-02 08:41:00' WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "manifest.json").write_text("{}")
    args = cli.parse_args(["--fixture", str(fixture), "--no-deep", "--date", str(PUSH.date())])
    assert pipeline.run(cfg, args, pipeline.Clock(PUSH.date(), time(8, 40))) == 0
    main, = cfg.outbox_dir.glob("2026-11-06-??????.json")
    text = json.loads(main.read_text())["text"]
    lines = text.splitlines()
    assert lines[1].startswith("1. Weekly 11｜")
    assert "另补充此前漏掉的" not in text and "本周共" not in text
    start = lines.index(wording.BACKFILL_TITLE)
    assert len([line for line in lines[1:start] if line[:1].isdigit()]) == 10
    assert lines[start + 1].startswith("1. Extra 12｜") and lines[start + 10].startswith("10. Extra 03｜")
    assert lines[start + 11] == (f"还有一些没有列出，[在往期页看]({BASE}/#backfill)" if passed else "还有一些没有列出")
    assert "上次推送后共" not in text and "非美国" not in text and "美国公司" not in text
    assert pipeline.run(cfg, args, pipeline.Clock(PUSH.date(), time(8, 40))) == 0
    with pipeline._read_database(cfg.db_path) as conn:
        same_day = conn.execute("SELECT supplement_event_ids_json,text FROM digest WHERE date='2026-11-06'").fetchone()
        assert len(json.loads(same_day[0])) == 10 and wording.BACKFILL_TITLE in same_day[1]
    before = contents(cfg)
    preview_args = cli.parse_args(["--preview-push", "--to", "test", "--date", str(PUSH.date())])
    assert pipeline.preview_push(cfg, preview_args, pipeline.Clock(PUSH.date(), time(8, 40))) == 0
    assert contents(cfg) == before
    preview, = cfg.outbox_dir.glob("2026-11-06-preview-*.json")
    preview_text = json.loads(preview.read_text())["text"]
    assert "本周共" not in preview_text and "1. Weekly 11｜" in preview_text
    with pipeline._read_database(cfg.db_path) as conn:
        digest = conn.execute("SELECT * FROM digest WHERE date='2026-11-06'").fetchone()
        assert len(json.loads(digest["event_ids_json"])) == 11 and len(json.loads(digest["supplement_event_ids_json"])) == 10
        assert conn.execute("SELECT COUNT(*) FROM company WHERE backfill_push_date='2026-11-06'").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM funding_event WHERE original_first_ingested_at IS NOT NULL AND push_date='2026-11-06'").fetchone()[0] == 10
    snap = snapshot(cfg, PUSH)
    assert len(snap["week"]["cards"]) == 11 and snap["status"]["pending_ratios"]["denominator"] == 11
    assert pipeline.run(cfg, cli.parse_args(["--resend", "--to", "test", "--date", str(PUSH.date())]), pipeline.Clock(PUSH.date(), time(8, 40))) == 0
    resend, = cfg.outbox_dir.glob("2026-11-06-resend-*.json")
    assert wording.BACKFILL_TITLE in json.loads(resend.read_text())["text"]
    assert pipeline.run(cfg, cli.parse_args(["--fixture", str(fixture), "--no-deep", "--date", "2026-11-13"]), pipeline.Clock(date(2026, 11, 13), time(8, 40))) == 0
    next_file, = cfg.outbox_dir.glob("2026-11-13-??????.json")
    assert wording.BACKFILL_TITLE not in json.loads(next_file.read_text())["text"]


def test_no_new_push_keeps_source_and_last_summary_before_supplement(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Extra"}])
    apply(cfg, manifest(cfg))
    with pipeline._read_database(cfg.db_path) as conn:
        sections, _ = backfill.collect_supplement(conn, cfg, NOW.date())
    data = DigestData(PUSH.date(), NOW, PUSH, "没有新增", extra_sections=sections,
                      last_push_summary=(date(2026, 10, 30), 4))
    lines = render.render_push(data, links_file=cfg.links_file).splitlines()
    assert lines[1:3] == [wording.NO_NEW, wording.BACKFILL_TITLE]
    assert lines[3].startswith("1. Extra｜") and len(lines) == 4


def test_preview_before_push_excludes_supplement_and_does_not_mark_it(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Extra"}])
    apply(cfg, manifest(cfg))
    before = contents(cfg)
    assert cli.main(args_for(cfg, "--preview-push", "--to", "test")) == 0
    assert contents(cfg) == before
    path, = cfg.outbox_dir.glob("*-preview-*.json")
    text = json.loads(path.read_text())["text"]
    assert wording.NO_NEW in text and wording.BACKFILL_TITLE in text and "1. Extra｜" in text
    assert "另补充此前漏掉的" not in text
    assert "本周共 1 家" not in text
    with pipeline._read_database(cfg.db_path) as conn:
        assert conn.execute("SELECT backfill_push_date FROM company").fetchone()[0] is None


def test_production_preview_is_rejected_without_state_files(tmp_path, capsys):
    cfg = configured(tmp_path, test=False)
    assert cli.main(args_for(cfg, "--preview-push")) == 2
    assert "只在测试模式" in capsys.readouterr().err and not cfg.state_dir.exists()


@pytest.mark.parametrize("option", ["--first-issue", "--resend", "--sync-sent", "--deep-only", "--build-site", "--preview-push", "--reset-test-db"])
def test_backfill_option_conflicts_exit_two_before_writing(tmp_path, option):
    cfg = configured(tmp_path)
    with pytest.raises(SystemExit) as error:
        cli.main(args_for(cfg, "--backfill-first-issue-gap", option))
    assert error.value.code == 2 and not cfg.state_dir.exists()


@pytest.mark.parametrize("flag", [["--dry-run"], ["--fixture", "unused"], ["--search-fixtures", "unused"],
    ["--rates-file", "unused"], ["--rates-fail"], ["--now", "08:00"], ["--links-file", "unused"], ["--to", "test"]])
def test_all_test_flags_select_test_database_and_outbox(tmp_path, monkeypatch, flag):
    cfg = configured(tmp_path, test=False)
    observed = []
    def capture(actual, args, clock):
        observed.append(actual)
        return 0
    monkeypatch.setattr(backfill, "run_backfill", capture)
    assert cli.main(args_for(cfg, "--backfill-first-issue-gap", *flag)) == 0
    actual, = observed
    assert actual.test_mode and actual.db_path == cfg.state_dir / "test/vc-radar-test.db"
    assert actual.outbox_dir == cfg.state_dir / "outbox-test"


def test_review_switch_is_explicit_and_config_is_not_rewritten(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    path_args = args_for(cfg, "--dry-run", "--assume-review-site-passed")
    config_path = Path(path_args[path_args.index("--config") + 1])
    original = config_path.read_bytes()
    seen = []
    monkeypatch.setattr(pipeline, "run", lambda actual, *rest: seen.append(actual) or 0)
    assert cli.main(path_args) == 0 and seen[-1].review_site_passed
    assert config_path.read_bytes() == original
    assert cli.main([part for part in path_args if part != "--assume-review-site-passed"]) == 0
    assert not seen[-1].review_site_passed and Config().review_site_passed is False
    with pytest.raises(SystemExit) as exc:
        cli.main(args_for(cfg, "--assume-review-site-passed"))
    assert exc.value.code == 2 and not cfg.state_dir.exists()


@pytest.mark.parametrize("text,currency,value,label", [
    ("¥8000 万（约 110 万美元）", None, 80e6, "¥8000 万（币种待确认）"),
    ("¥500 万日元", "JPY", 5e6, "日元 500 万（暂未换算成美元）"),
    ("¥10 million yuan", "CNY", 10e6, "人民币 1000 万（暂未换算成美元）"),
    ("RMB ¥5M", "CNY", 5e6, "人民币 500 万（暂未换算成美元）"),
    ("¥5M USD", None, 5e6, "¥500 万（币种待确认）"),
    ("5 crore", None, 50e6, "5000 万（币种待确认）"),
    ("3 lakh", None, 300000, "30 万（币种待确认）"),
])
def test_v16g_currency_adjacent_words_and_indian_quantities(text, currency, value, label):
    assert fx.parse_amount(text) == (currency, value)
    assert fx.format_amount(fx.convert_amount(text), text) == label
    if "（约" not in text:
        assert fx.format_amount_text(label) == label


@pytest.mark.parametrize("status,delay,error", [(200, 0, None), (200, 14, None), (200, 16, "timeout"), ("timeout", 0, "timeout"), (403, 0, None), (500, 1, None)])
def test_fixed_page_meta_uses_injected_clock_without_sleep(tmp_path, monkeypatch, status, delay, error):
    url = "https://synthetic.test/about"
    stem = tmp_path / sha1(url.encode()).hexdigest()
    stem.with_suffix(".html").write_text("<p>Handwritten company information.</p>")
    stem.with_suffix(".meta.json").write_text(encode({"status": status, "delay_s": delay}))
    clock = pipeline.Clock(NOW.date(), NOW.time())
    before = clock.now()
    def no_sleep(*args):
        raise AssertionError("固定页不能真实睡眠")
    monkeypatch.setattr(http.time, "sleep", no_sleep)
    response = http.FixedPageTransport(tmp_path, clock).get(url)
    assert response.error == error
    assert bool(response.body) is (status == 200 and delay <= 15)
    assert (clock.now() - before).total_seconds() >= min(delay, 15)


def test_migration_is_additive_idempotent_and_source_defaults_to_daily(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    conn.execute("INSERT INTO company(id,name_key,name,first_seen,status) VALUES ('old','old','Old','2026-10-31','总部待核')")
    conn.execute("INSERT INTO funding_event(id,company_id,round_norm,event_date,first_seen,filter_reasons_json) VALUES (1,'old','种子轮','2026-10-31','2026-10-31 00:35:00',?)", (encode(["总部非美国"]),))
    conn.commit()
    conn.close()
    for _ in range(2):
        conn = db.connect(path)
        assert conn.execute("SELECT backfill,backfill_push_date,status FROM company").fetchone()[:] == ("", None, "总部待核")
        assert conn.execute("SELECT original_first_ingested_at FROM funding_event").fetchone()[0] is None
        assert conn.execute("SELECT filter_reasons_json FROM funding_event").fetchone()[0] == encode(["总部非美国"])
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
        conn.close()


def test_dynamic_transition_counts_and_archive_filters_before_and_after_apply(tmp_path):
    cfg = configured(tmp_path)
    specs = [{"name": f"Transition {i:02}", "amount": f"${i}M"} for i in range(1, 21)]
    specs += [{"name": "Double " + str(i), "round_text": "Series B", "filtered": ("总部非美国", "轮次不在范围")} for i in range(2)]
    specs += [{"name": "Pending " + str(i), "status": "总部待核", "filtered": (), "amount": "$1M"} for i in range(4)]
    specs += [{"name": "Pending excluded " + str(i), "status": "总部待核", "filtered": ("轮次不在范围",), "round_text": "Series B"} for i in range(7)]
    specs += [{"name": "Baseline " + str(i), "baseline": True} for i in range(35)]
    ids = seed(cfg, specs)
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE company SET country='美国' WHERE status='总部待核'")
    conn.commit()
    conn.close()
    before = snapshot(cfg)
    assert len(before["week"]["cards"]) == 4 and len(before["week"]["filtered"]) == 9
    assert before["status"]["pending_ratios"]["country"]["count"] == 4
    first_ids = set(before["digests"][0]["event_ids"])
    assert first_ids - set(before["week"]["event_ids"]) == set(ids[:20])
    before_index, _ = site._site_payloads(before)
    apply(cfg, manifest(cfg))
    after = snapshot(cfg)
    assert set(after["week"]["event_ids"]) == set(ids[20:33])
    assert len(after["week"]["filtered"]) == 9 and len(after["week"]["cards"]) == 4
    assert set(ids[-35:]) <= set(after["listing"])
    overflow = [e for e in after["events"] if e["supplement_unsent"]]
    assert len(overflow) == 55
    assert all(e["status"] == "补充未推送" and not e["backfill"] and not e["next_push_label"] for e in overflow)
    index, _ = site._site_payloads(after)
    if not shutil.which("node"):
        pytest.skip("离线前端筛选验证需要 node")
    data_path, old_path = tmp_path / "data.json", tmp_path / "before.json"
    data_path.write_text(encode(index))
    old_path.write_text(encode(before_index))
    script = r"""
      const app=require(process.argv[1]),fs=require('fs');
      const data=JSON.parse(fs.readFileSync(process.argv[2]));
      const before=JSON.parse(fs.readFileSync(process.argv[3]));
      const filters={period:'all',tag:'all',round:'all',status:'all'};
      const select=(d,f)=>app.selectEvents(d,{...filters,...f}).map(e=>e.id);
      console.log(JSON.stringify({all:select(data,{}),backfill:select(data,{status:'backfill'}),
        admitted:select(data,{status:'已纳入'}),recent:select(data,{period:'30'}),
        dated:select(data,{period:'date:2026-10-31'}),tag:select(data,{tag:'都不是'}),
        beforeAdmitted:select(before,{status:'已纳入'}),html:app.listHtml(data,filters)}));
    """
    result = json.loads(subprocess.run(["node", "-e", script, str(site.ASSETS / "app.js"), str(data_path), str(old_path)], capture_output=True, text=True, check=True).stdout)
    unsent_ids = {e["id"] for e in overflow}
    assert unsent_ids <= set(result["all"]) and not result["backfill"]
    assert not unsent_ids & set(result["admitted"] + result["recent"] + result["dated"])
    assert unsent_ids <= set(result["tag"])
    assert len(result["beforeAdmitted"]) == 4
    assert '<span class="state-flag">补充未推送</span>' in result["html"] and "此前漏掉" in result["html"]
    assert 'class="dgroup-h"' in result["html"]
    assert "总部待核" not in result["html"] and "非美国" not in result["html"]


def test_supplement_is_visible_in_archive_by_push_period_and_recent_days(tmp_path):
    cfg = configured(tmp_path)
    ids = seed(cfg, [{"name": "Extra"}])
    apply(cfg, manifest(cfg))
    conn = db.connect(cfg.db_path)
    sections, supplement_ids = backfill.collect_supplement(conn, cfg, PUSH.date())
    backfill.mark_supplement(conn, supplement_ids, PUSH.date())
    scan.upsert_digest(conn, {"date": PUSH.date(), "push": True, "event_ids": [], "supplement_event_ids_json": supplement_ids,
                              "generated_at": PUSH, "status": "没有新增"})
    conn.commit()
    path = site.build_site(conn, cfg, now=PUSH)
    conn.close()
    data = json.loads((path / "data.json").read_text())
    assert not data["week"]["event_ids"] and ids[0] in data["listing"]
    event = next(e for e in data["events"] if e["id"] == ids[0])
    assert event["push_date"] == "2026-11-06" and event["backfill"] == "此前漏掉"
    detail = json.loads((path / "c/extra.json").read_text())
    assert detail["companies"][0]["backfill"] == "此前漏掉"
    assert snapshot(cfg, PUSH)["status"]["pending_ratios"]["denominator"] == 0
    assert "status" not in data
    if shutil.which("node"):
        script = r"""
          const app=require(process.argv[1]),fs=require('fs'),data=JSON.parse(fs.readFileSync(process.argv[2]));
          const f={period:'all',tag:'all',round:'all',status:'backfill'};
          console.log(JSON.stringify(['all','30','date:2026-11-06','week'].map(period=>app.selectEvents(data,{...f,period}).map(e=>e.id))));
        """
        matches = json.loads(subprocess.run(["node", "-e", script, str(site.ASSETS / "app.js"), str(path / "data.json")], check=True, text=True, capture_output=True).stdout)
        assert matches == [ids, ids, ids, []]


def test_verification_logs_feed_detail_but_daily_quota_and_counts_exclude_backfill(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Unknown", "country": None}])
    def verify(candidate, **kwargs):
        return {"searches": [{"requested_at": "2026-10-31 08:40:01", "query": "Unknown software funding", "status": 200,
                              "result_count": 1, "billed": True, "cost": .005, "results": []}],
                "model_calls": [{"cost": .0035}], "fields": [
                    {"field": "国家", "result": "已确认", "urls": ["https://news.test/confirmed"], "tiers": ["独立媒体"],
                     "quotes": ["A synthetic company is based in London."], "verified_at": "2026-10-31 08:40:02"},
                    {"field": "标签", "result": "查过仍无法确认", "reason": "搜索没找到", "urls": [], "quotes": [], "verified_at": "2026-10-31 08:40:02"}]}
    apply(cfg, manifest(cfg, verifier=verify))
    snap = snapshot(cfg)
    verify_stats = snap["status"]["verification"]
    assert {k: verify_stats[k] for k in ["companies", "fields", "unsearched", "searches"]} == dict.fromkeys(["companies", "fields", "unsearched", "searches"], 0)
    assert verify_stats["month_used"] == 0 and verify_stats["backfill_cost"] == 0
    _, details = site._site_payloads(snap)
    messages = details["c/unknown.json"]["companies"][0]["verification"]
    assert messages[0]["text"] == "入库时已查证" and messages[0]["urls"] == ["https://news.test/confirmed"]
    assert messages[1]["text"] == "已查过 1 次，仍无法确认"
    conn = db.connect(cfg.db_path)
    assert db.daily_verify_search_count(conn, NOW.date()) == 0
    conn.execute("INSERT INTO verify_search(company_id,run_date,requested_at,query,status,billed,cost,source) VALUES ('unknown',?,'2026-10-31 08:41:00','daily','200',1,.005,'日常')", (FIRST,))
    conn.execute("INSERT INTO verify_search(company_id,run_date,requested_at,query,status,billed,cost,source) VALUES ('unknown',?,'2026-10-31 08:42:00','first','200',1,.005,'首期')", (FIRST,))
    conn.commit()
    assert db.daily_verify_search_count(conn, NOW.date()) == 1
    assert site._snapshot(conn, cfg, NOW)["status"]["verification"]["searches"] == 1
    conn.close()


def test_failed_apply_rolls_back_then_normal_push_still_runs(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Extra"}])
    data = manifest(cfg)
    with pipeline._read_database(cfg.db_path) as conn:
        before = list(conn.iterdump())
    original_writer = outbox.write_atomic
    def fail_record(path, payload):
        if payload.get("kind") == "backfill":
            raise OSError("合成写入故障")
        return original_writer(path, payload)
    monkeypatch.setattr(outbox, "write_atomic", fail_record)
    with pytest.raises(OSError):
        apply(cfg, data)
    with pipeline._read_database(cfg.db_path) as conn:
        assert list(conn.iterdump()) == before
        assert backfill.collect_supplement(conn, cfg, PUSH.date()) == ([], [])
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "manifest.json").write_text("{}")
    assert pipeline.run(cfg, cli.parse_args(["--fixture", str(fixture), "--no-deep", "--date", str(PUSH.date())]), pipeline.Clock(PUSH.date(), time(8, 40))) == 0
    main, = cfg.outbox_dir.glob("2026-11-06-??????.json")
    assert wording.BACKFILL_TITLE not in json.loads(main.read_text())["text"]


def test_fixed_rates_flags_freeze_by_event_date_and_never_write_input(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Euro", "amount": "€11M"}, {"name": "Dollar", "amount": "$20M"}])
    fixed = tmp_path / "fixed-rates.json"
    fixed.write_text(encode({"base": "USD", "source": "合成汇率", "days": {"2026-10-29": {
        "fetched_at": "2026-10-29 08:41:00", "rates": {"EUR": .5}}}}))
    before, input_bytes = contents(cfg), fixed.read_bytes()
    args = cli.parse_args(["--rates-file", str(fixed)])
    session = fx.rates_for_run(cfg, args, now=NOW, persist=False)
    with pipeline._read_database(cfg.db_path) as conn:
        data = backfill.create_manifest(conn, cfg, now=NOW, rates=session)
    assert data["candidates"][0]["name"] == "Euro"
    event = data["rejudge"][0]["snapshot"]["events"][0]
    assert event["amount_usd"] == 22e6 and event["fx_date"] == "2026-10-29"
    assert contents(cfg) == before and fixed.read_bytes() == input_bytes and not cfg.rates_file.exists()
    args = cli.parse_args(["--rates-file", str(fixed), "--rates-fail"])
    failing = fx.rates_for_run(cfg, args, now=NOW)
    assert failing.snapshot(date(2026, 10, 29), ("EUR",))["stale"] == 1
    assert fixed.read_bytes() == input_bytes


def test_old_schema_manifest_is_read_only_and_lock_prevents_parallel_apply(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Alpha"}])
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("ALTER TABLE company DROP COLUMN backfill")
    conn.execute("ALTER TABLE company DROP COLUMN backfill_push_date")
    conn.execute("ALTER TABLE funding_event DROP COLUMN original_first_ingested_at")
    conn.execute("ALTER TABLE digest DROP COLUMN supplement_event_ids_json")
    conn.commit()
    conn.close()
    before = contents(cfg)
    assert manifest(cfg)["candidates"][0]["in_supplement"]
    assert contents(cfg) == before
    with pipeline.run_lock(cfg):
        result = backfill.run_backfill(cfg, cli.parse_args(["--backfill-first-issue-gap"]), pipeline.Clock(NOW.date(), time(8, 40)))
    assert result == 1 and not list(cfg.outbox_dir.glob("*.json"))


def test_review_switch_controls_all_four_page_references(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": f"Extra {i:02}"} for i in range(11)])
    apply(cfg, manifest(cfg))
    with pipeline._read_database(cfg.db_path) as conn:
        sections, _ = backfill.collect_supplement(conn, cfg, NOW.date())
    data = DigestData(PUSH.date(), NOW, PUSH, "没有新增", base_url=BASE, extra_sections=sections,
                      new_evidence_count=2, yc_new_batch=("Fall 2026", 31))
    before = render.render_push(data, links_file=cfg.links_file)
    assert "YC 新一批 31 家" in before and "资料有出入" not in before
    assert "还有一些没有列出" in before
    assert "/#" not in before and "见网页" not in before and "在往期页看" not in before
    after = render.render_push(replace(data, review_site_passed=True), links_file=cfg.links_file)
    for route in ("backfill", "week"):
        assert f"{BASE}/#{route}" in after
    assert f"{BASE}/#yc" in after and "#sources" not in after
    assert render.remove_page_links(after) == before


@pytest.mark.parametrize("flag", ["--replay", "--assume-review-site-passed"])
def test_remaining_parameter_errors_do_not_create_state(tmp_path, flag):
    cfg = configured(tmp_path)
    params = ["--backfill-first-issue-gap", "--replay", "unused.json"] if flag == "--replay" else [flag]
    with pytest.raises(SystemExit) as error:
        cli.main(args_for(cfg, *params))
    assert error.value.code == 2 and not cfg.state_dir.exists()


def test_new_round_and_unlisted_company_new_report_follow_normal_rules(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": f"Extra {i:02}", "amount": f"${i}M"} for i in range(1, 12)])
    data = manifest(cfg)
    apply(cfg, data)
    conn = db.connect(cfg.db_path)
    original = conn.execute("SELECT original_first_ingested_at FROM funding_event WHERE company_id='extra-01'").fetchone()[0]
    for name, round_text, suffix in [("Extra 11", "Series A", "-new-round"), ("Extra 01", "Seed", "-new-report")]:
        item = RawItem("cbn_main", suffix, "https://news.test/" + suffix, name + " financing",
                       PUSH, excerpt=name + " is headquartered in London.")
        ext = Extracted(company_name=name, website="https://" + scan.name_key(name) + ".test/",
                        round_text=round_text, amount_text="$30M", hq_text="London")
        raw_id, _ = scan.upsert_raw_item(conn, item, PUSH, False)
        scan.ingest(conn, raw_id, item, ext, now=PUSH, is_baseline=False, is_backfill=False, latest_yc_batches=[], model=None)
    listing = pipeline._listing_for_issue(conn, PUSH.date())
    assert {c.name for c in listing["cards"]} == {"Extra 11", "Extra 01"}
    assert conn.execute("SELECT original_first_ingested_at FROM funding_event WHERE company_id='extra-01'").fetchone()[0] == original
    scan.mark_listed(conn, listing["event_ids"], PUSH.date())
    scan.upsert_digest(conn, {"date": PUSH.date(), "push": True, "event_ids": listing["event_ids"], "generated_at": PUSH})
    conn.commit()
    snap = site._snapshot(conn, cfg, PUSH)
    assert set(snap["week"]["cards"]) == set(listing["event_ids"])
    conn.close()


def test_company_merge_keeps_verification_and_supplement_foreign_keys(tmp_path):
    cfg = configured(tmp_path)
    seed(cfg, [{"name": "Alpha", "country": None}, {"name": "Beta", "filtered": ()}])
    data = manifest(cfg)
    apply(cfg, data)
    conn = db.connect(cfg.db_path)
    alpha_id = conn.execute("SELECT id FROM funding_event WHERE company_id='alpha'").fetchone()[0]
    conn.execute("INSERT INTO verify_log(company_id,run_date,run_started_at,field,result,verified_at,target_event_id) "
                 "VALUES ('alpha','2026-10-31','2026-10-31 08:40:00','国家','本次未查','2026-10-31 08:40:00',?)", (alpha_id,))
    conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (f"backfill:normal:{alpha_id}", "2026-11-06 08:40:00"))
    scan.upsert_digest(conn, {"date": PUSH.date(), "supplement_event_ids_json": [alpha_id], "event_ids": []})
    scan._merge_company(conn, "alpha", "beta")
    conn.commit()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT company_id FROM verify_log").fetchone()[0] == "beta"
    assert conn.execute("SELECT backfill FROM company WHERE id='beta'").fetchone()[0] == "补充未推送"
    assert conn.execute("SELECT value FROM meta WHERE key='backfill:rank:beta'").fetchone()
    target_id = conn.execute("SELECT id FROM funding_event WHERE company_id='beta'").fetchone()[0]
    assert conn.execute("SELECT value FROM meta WHERE key=?", (f"backfill:normal:{target_id}",)).fetchone()
    assert not conn.execute("SELECT 1 FROM company WHERE id='alpha'").fetchone()
    conn.close()


def test_short_yen_amount_retains_adjacent_currency_from_original_source(tmp_path):
    cfg = configured(tmp_path)
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, P0_SOURCES)
    item = RawItem("tc_venture", "yen", "https://news.test/yen", "Synthetic financing", NOW,
                   body="A synthetic company raised ¥10 million yuan.")
    ext = Extracted(company_name="Yen", round_text="Seed", amount_text="¥10 million")
    raw_id, _ = scan.upsert_raw_item(conn, item, NOW, False)
    session = rates(cfg)
    session.cache = {"base": "USD", "source": "合成汇率", "days": {"2026-10-31": {
        "fetched_at": "2026-10-31 08:40:00", "rates": {"CNY": 10}}}}
    session.fixed = True
    eid = scan.ingest(conn, raw_id, item, ext, now=NOW, is_baseline=False, is_backfill=False,
                      latest_yc_batches=[], model=None, fx_rates=session)
    row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
    assert row["amount_currency"] == "CNY" and row["amount_usd"] == 1e6
    conn.close()


@pytest.mark.parametrize("test_mode", [False, True])
def test_review_passed_without_base_errors_in_production_and_warns_in_test(tmp_path, monkeypatch, capsys, test_mode):
    cfg = configured(tmp_path)
    path = tmp_path / "empty-base.toml"
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"review_site_passed":true}')
    path.write_text(f"state_dir={json.dumps(str(cfg.state_dir))}\nruntime_file={json.dumps(str(runtime))}\nbase_url=''\n")
    seen = []
    monkeypatch.setattr(pipeline, "run", lambda actual, *rest: seen.append(actual) or 0)
    result = cli.main(["--config", str(path), *(["--dry-run"] if test_mode else [])])
    assert result == (0 if test_mode else 1) and not cfg.state_dir.exists()
    assert "base_url" in capsys.readouterr().err
    assert (seen and not seen[0].review_site_passed) if test_mode else not seen
