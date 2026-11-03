"""W：V16m 金额审计、清单逐行比较和汇率提示，全部使用合成数据。"""
from datetime import timedelta
import json

import pytest

from vcradar import backfill, cli, db, fx, scan, site, verify
from vcradar.config import Config
from test_v_backfill import NOW, add_event, apply, env, fixed_rates, manifest, offline


def evidence(candidate, **kwargs):
    return {"searches": [{"requested_at": "2026-10-31 14:00:01", "query": candidate["name"] + " product",
                         "status": 200, "result_count": 1, "results": [{"url": "https://evidence.test/"}],
                         "billed": True, "cost": .005}], "model_calls": [{"cost": .0035}],
            "fields": [{"field": "国家", "result": "查过仍无法确认", "reason": "搜索没找到",
                        "verified_at": "2026-10-31 14:00:02"}]}


def test_39_top_level_rejudge_and_searches(env, monkeypatch):
    conn, cfg = env
    add_event(conn, "Pending", country=None, pending=True, filtered=())
    add_event(conn, "Mapped", country="London", pending=True, filtered=())
    add_event(conn, "Candidate", country=None)
    data = manifest(env, verifier=evidence)
    assert len(data["rejudge"]) == 3 and len(data["searches"]) == 2
    assert {s["kind"] for s in data["searches"]} == {"A", "B"}
    assert all(s["seq"] == 1 and s["conclusion"] for s in data["searches"])
    assert all(not ({"rejudge", "searches", "fields", "model_calls"} & c.keys()) for c in data["candidates"])
    assert next(r for r in data["rejudge"] if r["name"] == "Pending")["old"]["status"] == "总部待核"
    monkeypatch.setattr(scan, "country_from_hq", lambda *a, **k: pytest.fail("apply 不得重判"))
    apply(env, data)
    assert conn.execute("SELECT COUNT(*) FROM verify_search").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM verify_log WHERE event_id IS NULL").fetchone()[0] == 2
    for row in data["rejudge"]:
        assert dict(conn.execute("SELECT country,status FROM company WHERE id=?", (row["company_id"],)).fetchone()) == row["new"]


def test_39_apply_row_skip(env, capsys):
    conn, cfg = env
    changed = add_event(conn, "BioGeometry", old=1, amount="$10M")
    sava = add_event(conn, "Sava", old=2, amount="$8M", pending=True, country=None, filtered=())
    country = add_event(conn, "Country", pending=True, country=None, filtered=())
    good = add_event(conn, "Good", old=3, amount="$4M")
    data = manifest(env, verifier=evidence)
    cid = conn.execute("SELECT company_id FROM funding_event WHERE id=?", (country,)).fetchone()[0]
    conn.execute("UPDATE funding_event SET amount_status='金额待核',marks_json='[\"金额待核\"]' WHERE id=?", (changed,))
    conn.execute("UPDATE funding_event SET amount_usd=9 WHERE id=?", (sava,))
    conn.execute("UPDATE company SET country='法国' WHERE id=?", (cid,))
    conn.commit()
    before = dict(conn.execute("SELECT * FROM funding_event WHERE id=?", (changed,)).fetchone())
    apply(env, data)
    output = capsys.readouterr().out
    assert f"跳过：BioGeometry，{changed}，清单生成后库里已变化" in output
    assert f"跳过：Sava，{sava}，清单生成后库里已变化" in output
    assert "跳过：Country，公司，清单生成后库里已变化" in output
    assert output.rstrip().endswith("跳过 3 条")
    assert (cfg.log_dir / "2026-11-01-backfill.log").read_text() == output
    current = dict(conn.execute("SELECT * FROM funding_event WHERE id=?", (changed,)).fetchone())
    for key in ("amount_status", "amount_original", "amount_usd", "marks_json"):
        assert current[key] == before[key]
    assert conn.execute("SELECT amount_usd FROM funding_event WHERE id=?", (sava,)).fetchone()[0] == 9
    assert not conn.execute("SELECT 1 FROM verify_log WHERE event_id IN (?,?)", (changed, sava)).fetchone()
    assert not conn.execute("SELECT 1 FROM verify_search WHERE company_id=?", (cid,)).fetchone()
    assert not conn.execute("SELECT 1 FROM verify_log WHERE company_id=?", (cid,)).fetchone()
    assert conn.execute("SELECT amount_usd FROM funding_event WHERE id=?", (good,)).fetchone()[0] == 4e6


def test_39_candidate_rejudge_skip_preserves_whole_company_and_rank(env, capsys):
    conn, _ = env
    for i in range(11):
        add_event(conn, f"Company {i:02}", amount=f"${30-i}M")
    data = manifest(env)
    candidate = data["candidates"][0]
    conn.execute("UPDATE company SET country='法国' WHERE id=?", (candidate["company_id"],))
    conn.commit()
    before = tuple(conn.execute("SELECT * FROM company WHERE id=?", (candidate["company_id"],)).fetchone())
    path, file = apply(env, data)
    assert tuple(conn.execute("SELECT * FROM company WHERE id=?", (candidate["company_id"],)).fetchone()) == before
    record = json.loads(path.read_text())
    assert record["count"] == 9 and [c["rank"] for c in record["companies"]] == list(range(2, 12))
    assert capsys.readouterr().out.count("跳过：") == 1
    assert json.loads(file.read_text())["applied_at"]


def test_39_12_amount_audit_uses_exact_generation_time_and_never_triggers_search(env):
    conn, cfg = env
    a = add_event(conn, "Two Events", amount="$8M", old=8, pending=True, filtered=(), country="London")
    b = add_event(conn, "Two Events", amount="$12M", old=12, pending=True, filtered=(), country="London", round_text="Series A")
    conn.execute("UPDATE funding_event SET amount_original=8,amount_usd=8 WHERE id=?", (a,))
    unknown = add_event(conn, "Unknown", amount="$1", old=1, quote="The amount was not disclosed.")
    data = manifest(env)
    apply(env, data)
    rows = list(conn.execute("SELECT * FROM verify_log WHERE field='金额'"))
    assert {r["event_id"] for r in rows} == {a, b, unknown}
    assert {r["verified_at"] for r in rows} == {data["generated_at"]}
    assert {r["result"] for r in rows} == {"已更正", "金额待确认"}
    context = verify.company_context(conn, conn.execute("SELECT company_id FROM funding_event WHERE id=?", (a,)).fetchone()[0], cfg, event_id=a)
    conn.execute("UPDATE funding_event SET amount_status='金额待核' WHERE id=?", (a,))
    context["event"]["amount_status"] = "金额待核"
    assert "金额" not in verify._due_fields(conn, context, cfg, NOW + timedelta(days=40))
    _, details = site._verification(conn, NOW, cfg)
    corrections = [v for values in details.values() for v in values if v.get("correction")]
    assert len(corrections) == 3
    assert {v["text"] for v in corrections} == {"金额已按来源原文更正", "来源里没有写明金额"}
    assert all("已查过" not in v["text"] and "入库时已查证" not in v["text"] for v in corrections)


def test_39_12_migration_removes_old_unique_constraint_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO company(id,name_key,name,first_seen) VALUES ('c','c','C','2026-10-31')")
    conn.execute("DROP TABLE verify_log")
    conn.execute("CREATE TABLE verify_log(id INTEGER PRIMARY KEY,company_id TEXT,run_date TEXT,run_started_at TEXT,field TEXT,result TEXT,reason TEXT DEFAULT '',urls_json TEXT DEFAULT '[]',tiers_json TEXT DEFAULT '[]',known_fact_hit INTEGER DEFAULT 0,known_fact_json TEXT DEFAULT '[]',quotes_json TEXT DEFAULT '[]',verified_at TEXT,UNIQUE(company_id,field,verified_at))")
    conn.execute("INSERT INTO verify_log(company_id,run_date,field,result,verified_at) VALUES ('c','2026-10-31','国家','本次未查','2026-10-31 08:40:00')")
    conn.commit(); conn.close()
    conn = db.connect(path)
    for eid in (1, 2):
        conn.execute("INSERT INTO verify_log(company_id,event_id,run_date,field,result,verified_at) VALUES ('c',?,'2026-10-31','金额','已更正','2026-10-31 08:40:00')", (eid,))
    assert conn.execute("SELECT COUNT(*) FROM verify_log").fetchone()[0] == 3
    conn.commit(); conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM verify_log").fetchone()[0] == 3
    conn.close()


@pytest.mark.parametrize("table,missing,message", [({}, ["INR"], "汇率没有取到"),
    ({"EUR": .9}, ["INR"], "汇率没有取到：INR"),
    ({"EUR": .9}, ["KRW", "INR"], "汇率没有取到：INR、KRW")])
def test_37_missing_rate_wording(tmp_path, table, missing, message):
    cfg = Config(state_dir=tmp_path, test_mode=True)
    cfg.rates_file.parent.mkdir(parents=True)
    cfg.rates_file.write_text(json.dumps({"base": "USD", "source": "合成汇率", "days": {
        "2026-10-30": {"fetched_at": "2026-10-30 08:40:00", "rates": table}} if table else {}}))
    rates = fx.rates_for_run(cfg, cli.parse_args(["--dry-run"]), now=NOW)
    for code in missing:
        assert not rates.snapshot(NOW.date(), [code])["rates"]
    assert rates.status["message"] == message


def test_37_status_page_missing_codes_can_coexist_with_old_rate_warning(env):
    conn, cfg = env
    fixed_rates(cfg)
    add_event(conn, "Rupee", amount="₹10M", filtered=())
    add_event(conn, "Won", amount="₩10M", filtered=())
    assert site._snapshot(conn, cfg, NOW)["status"]["fx"]["message"] == "汇率没有取到：INR、KRW"
    (cfg.state_dir / "test/status.json").write_text(json.dumps({"fx": {"ok": False,
        "message": "汇率没更新，用的是 10-30 的汇率", "currencies": ["EUR"], "date": "2026-10-30"}}))
    assert fx.status_from_files(cfg, currencies=["INR", "KRW"])["message"] == "汇率没更新，用的是 10-30 的汇率 · 汇率没有取到：INR、KRW"


def test_40_corrected_event_does_not_start_amount_search_in_normal_run(env):
    from test_t_verify import Clock
    conn, cfg = env
    eid = add_event(conn, "Sava", old=1, amount="$5M", pending=True, filtered=())
    data = manifest(env)
    apply(env, data)
    conn.execute("UPDATE funding_event SET amount_status='金额待核' WHERE id=?", (eid,))
    cid = conn.execute("SELECT company_id FROM funding_event WHERE id=?", (eid,)).fetchone()[0]
    class Forbidden:
        calls = 0
        def search(self, *args):
            self.calls += 1
            pytest.fail("更正事件不因金额重搜")
    service = Forbidden()
    assert verify.run_verification(conn, cfg, Clock("2026-11-03"), company_ids=[cid], services=service) == {}
    assert service.calls == 0


def test_39_whole_company_skip_precedes_amount_row_comparison(env, capsys):
    conn, _ = env
    eid = add_event(conn, "Changed", old=1, amount="$5M")
    data = manifest(env)
    conn.execute("UPDATE funding_event SET push_date='2026-11-01',amount_usd=9 WHERE id=?", (eid,))
    conn.commit()
    apply(env, data)
    output = capsys.readouterr().out
    assert output.count("跳过：") == 1 and "共跳过 1 家" in output
    assert "清单生成后库里已变化" not in output
    assert conn.execute("SELECT amount_usd FROM funding_event WHERE id=?", (eid,)).fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM verify_log").fetchone()[0] == 0


def test_39_a_mapped_country_without_search_shows_omitted_for_other_pending_fields(env):
    conn, cfg = env
    eid = add_event(conn, "Mapped", pending=True, filtered=())
    row = conn.execute("SELECT extraction_json FROM event_source WHERE event_id=?", (eid,)).fetchone()
    extracted = json.loads(row[0]); extracted["ai"] = {"verdict": None, "quote": ""}
    conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=?", (json.dumps(extracted), eid))
    data = manifest(env)
    assert not data["searches"]
    apply(env, data)
    company = site._snapshot(conn, cfg, NOW)["companies"][0]
    assert any(v["text"] == "没有做入库查证" for v in company["verification"])
    assert conn.execute("SELECT COUNT(*) FROM verify_log").fetchone()[0] == 0
