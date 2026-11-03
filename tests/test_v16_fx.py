"""V16e/S 对齐后的金额与汇率契约；全部返回均为手写合成数据。"""
from datetime import date, datetime, timedelta
import json
from pathlib import Path

import pytest

from vcradar import cli, config, db, extract, fx, http, llm, outbox, pipeline, render, scan, site
from vcradar.config import BJ, UA, Config
from vcradar.models import Extracted, HttpResponse, RawItem
from vcradar.sources.registry import P0_SOURCES


NOW = datetime(2026, 10, 31, 8, 40, tzinfo=BJ)
FIXTURES = Path(__file__).parent / "fixtures" / "v16_fx"
DAILY = (FIXTURES / "daily.xml").read_text()
HISTORY = (FIXTURES / "hist-90d.xml").read_text()
OLD = '<Envelope><Cube><Cube time="2026-06-19"><Cube currency="USD" rate="1.3"/></Cube></Cube></Envelope>'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("测试不访问网络、密钥或真实模型")
    monkeypatch.setattr(config, "load_api_key", forbidden)
    monkeypatch.setattr(http.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(llm, "urlopen", forbidden)


class Fetcher:
    def __init__(self, responses=None):
        self.responses = responses or {fx.DAILY_URL: DAILY, fx.HISTORY_90D_URL: HISTORY, fx.HISTORY_URL: OLD}
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append(url)
        assert headers == {"User-Agent": UA} and timeout == 30
        value = self.responses.get(url, HttpResponse(url, 404))
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, HttpResponse) else HttpResponse(url, 200, body=value)


def rates(tmp_path, fetcher=None, now=NOW):
    return fx.FxRates(Config(state_dir=tmp_path / "test"), fetcher or Fetcher(), now=now)


def connection(tmp_path):
    conn = db.connect(tmp_path / "data.db")
    db.sync_sources(conn, P0_SOURCES)
    return conn


def add(conn, rate_session=None, *, amount="€11M", source="tc_venture", name="手写公司", round_text="Seed", day=NOW):
    serial = str(conn.execute("SELECT count(*) FROM raw_item").fetchone()[0])
    item = RawItem(source, serial, f"https://synthetic.example/{serial}", f"{name} raises {amount or 'funding'}",
                   day, structured={"hq_text": "London"})
    ext = Extracted(company_name=name, round_text=round_text, amount_text=amount, hq_text="London",
                    summary="合成产品公司。")
    raw_id, _ = scan.upsert_raw_item(conn, item, day, False)
    eid = scan.ingest(conn, raw_id, item, ext, now=day, is_baseline=False, is_backfill=False,
                      latest_yc_batches=[], model=None, fx_rates=rate_session)
    return eid


@pytest.mark.parametrize("value,expected", [
    (8000, "8000 美元"), (2.5e6, "250 万美元"), (27e6, "2700 万美元"),
    (120e6, "1.2 亿美元"), (1e9, "10 亿美元"), (6e9, "60 亿美元"),
    (9999.6e4, "1 亿美元"), (14999, "1 万美元"), (15000, "2 万美元"), (0, "0 美元"),
])
def test_usd_writing(value, expected):
    assert fx.format_usd(value) == expected


@pytest.mark.parametrize("text,currency,value", [
    ("$2.5M", "USD", 2.5e6), ("US$5 million", "USD", 5e6), ("USD 5000", "USD", 5000),
    ("5 million dollars", "USD", 5e6), ("€11M", "EUR", 11e6), ("11 million euros", "EUR", 11e6),
    ("£8M", "GBP", 8e6), ("8 million pounds", "GBP", 8e6), ("₹50M", "INR", 50e6),
    ("5 million rupees", "INR", 5e6), ("C$5M", "CAD", 5e6), ("A$5M", "AUD", 5e6),
    ("CHF 500 万", "CHF", 5e6), ("SEK 2M", "SEK", 2e6), ("₪3M", "ILS", 3e6),
    ("3 million shekels", "ILS", 3e6), ("JPY 5000 万", "JPY", 50e6), ("5 million yen", "JPY", 5e6),
    ("CNY 5M", "CNY", 5e6), ("RMB 5M", "CNY", 5e6), ("5 million yuan", "CNY", 5e6),
    ("SGD 5M", "SGD", 5e6), ("S$5M", "SGD", 5e6), ("¥5M", None, 5e6),
    ("raised 5 million", None, 5e6), ("未披露", None, None),
])
def test_currency_is_explicit_only(text, currency, value):
    assert scan.parse_amount(text) == (currency, value)
    assert scan.parse_amount_usd(text) == (value if currency == "USD" else None)


def test_non_dollar_writing():
    assert fx.format_amount(fx.Amount("EUR", 11e6, 12e6)) == "约 1200 万美元（原 €1100 万）"
    assert fx.format_amount(fx.Amount("GBP", 8e6, 10e6)) == "约 1000 万美元（原 £800 万）"
    assert fx.format_original("EUR", 8000) == "€8000"
    assert fx.format_original("EUR", 120e6) == "€1.2 亿"
    assert fx.format_original("CHF", 5e6) == "瑞士法郎 500 万"
    assert fx.format_amount_text("TechCrunch 写 $10M，Crunchbase News 写 $12M") == "TechCrunch 写 1000 万美元，Crunchbase News 写 1200 万美元"
    assert fx.format_amount_text("融资$27M，估值$6B。") == "融资2700 万美元，估值60 亿美元。"
    assert fx.format_amount_text("Funding was $8000 in cash.") == "Funding was 8000 美元 in cash."


def test_recent_publication_day_and_daily_once(tmp_path):
    fetcher = Fetcher()
    session = rates(tmp_path, fetcher)
    for day in (date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)):
        snapshot = session.snapshot(day)
        assert snapshot["date"] == "2026-10-30"
        assert fx.convert_amount("£8M", snapshot).amount_usd == pytest.approx(12e6)
    session.refresh()
    assert fetcher.calls == [fx.DAILY_URL, fx.HISTORY_90D_URL]
    cache = json.loads(session.path.read_text())
    assert cache["base"] == "USD"
    assert cache["days"]["2026-10-30"]["rates"]["EUR"] == pytest.approx(1 / 1.2)
    assert cache["days"]["2026-10-30"]["fetched_at"] == "2026-10-31 08:40:00"
    assert not (tmp_path / "rates.json").exists()


def test_history_escalation_weekend_and_holiday(tmp_path):
    fetcher = Fetcher()
    session = rates(tmp_path, fetcher)
    assert session.snapshot(date(2026, 10, 26))["date"] == "2026-10-30"
    assert session.snapshot(date(2026, 10, 29))["date"] == "2026-10-29"
    assert fetcher.calls == [fx.DAILY_URL, fx.HISTORY_90D_URL]
    assert session.snapshot(date(2026, 6, 19))["date"] == "2026-06-19"
    assert fetcher.calls == [fx.DAILY_URL, fx.HISTORY_90D_URL, fx.HISTORY_URL]
    assert session.snapshot(date(2026, 7, 30))["date"] == "2026-10-30"
    assert len(fetcher.calls) == 3


@pytest.mark.parametrize("response", [HttpResponse(fx.DAILY_URL, None, error="timeout", elapsed_s=30),
                                      HttpResponse(fx.DAILY_URL, 503), "<invalid>",
                                      '<Cube time="2026-10-30"><Cube currency="USD" rate="0"/></Cube>'])
def test_failed_fetch_uses_cache_and_logs(tmp_path, response):
    rates(tmp_path).refresh()
    records = []
    session = fx.FxRates(Config(state_dir=tmp_path, test_mode=True), Fetcher({fx.DAILY_URL: response}), now=NOW, log=records.append)
    value = fx.convert_amount("€10M", session.snapshot(NOW.date()))
    assert value.amount_usd == pytest.approx(12e6) and value.fx_stale == 1
    assert fx.format_amount(value) == "约 1200 万美元（原 €1000 万，按 10-30 汇率）"
    assert session.status["ok"] is False and session.status["date"] == "2026-10-30"
    assert records[0]["source"] == fx.DAILY_URL and "elapsed_s" in records[0]


def test_no_cache_and_missing_currency_degrade_without_interrupting(tmp_path):
    session = rates(tmp_path, Fetcher({fx.DAILY_URL: HttpResponse(fx.DAILY_URL, 503)}))
    with connection(tmp_path) as conn:
        eid = add(conn, session)
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["amount_usd"] is None and row["amount_status"] == "暂未换算"
        card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
        assert card.amount_label == "€1100 万（暂未换算成美元）" and "金额待核" not in card.marks
        text = render.render_card(card, "")
        assert "金额说法不一" not in text and "暂未换算成美元" in text and "$" not in text


def test_numeric_sort_big_threshold_and_unknown_bucket(tmp_path):
    session = rates(tmp_path)
    with connection(tmp_path) as conn:
        for name, text in (("Dollar", "$11M"), ("Euro", "€10M"), ("Unknown", "5 million"),
                           ("YenUnknown", "¥1B"), ("Undisclosed", None)):
            add(conn, session, name=name, amount=text)
        add(conn, session, name="Disputed", amount="$5M")
        add(conn, session, name="Disputed", amount="$10M", source="cbn_main")
        listing = scan.collect_listing(conn, NOW.date(), weekly=True)
        names = [card.name for card in render.sort_cards(listing["cards"], first_seen=listing["first_seen"])]
        assert names == ["Euro", "Dollar", "Disputed", "Unknown", "YenUnknown", "Undisclosed"]
        for text, round_text, big in (("€41.666666M", "Seed", False), ("€41.666667M", "Seed", True),
                                      ("€83.333334M", "Series A", True), ("500 million", "Seed", False)):
            name = text + round_text
            eid = add(conn, session, name=name, amount=text, round_text=round_text)
            row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
            assert ("大额" in json.loads(row["marks_json"])) is big


def test_multisource_comparison_and_frozen_exchange_rate(tmp_path):
    session = rates(tmp_path)
    with connection(tmp_path) as conn:
        eid = add(conn, session, amount="€10M")
        original = dict(conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone())
        changed = rates(tmp_path, Fetcher({fx.DAILY_URL: DAILY.replace('rate="1.2"', 'rate="2.0"')}))
        add(conn, changed, amount="€10M", source="cbn_main", day=NOW + timedelta(days=2))
        frozen = dict(conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone())
        assert (frozen["fx_rate"], frozen["amount_usd"]) == (original["fx_rate"], original["amount_usd"])
        assert changed.requested == set()
        add(conn, session, amount="$13.2M", source="cbn_venture")
        assert conn.execute("SELECT amount_status FROM funding_event WHERE id=?", (eid,)).fetchone()[0] == "披露"
        add(conn, session, amount="$13.20001M", source="cbn_seed")
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["amount_status"] == "金额待核"
        assert scan.amount_label(conn, row).startswith("TechCrunch 写 约 1200 万美元（原 €1000 万，按 10-30 汇率）")


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_currency_dispute_uses_same_day_independent_of_ingestion_order(tmp_path, reverse):
    session = rates(tmp_path)
    inputs = [("$10M", "tc_venture"), ("€10M", "cbn_main")]
    with connection(tmp_path) as conn:
        for amount, source in reversed(inputs) if reverse else inputs:
            eid = add(conn, session, amount=amount, source=source)
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["amount_status"] == "金额待核"
        assert row["amount_usd"] == 10e6
        assert scan.amount_label(conn, row) == "TechCrunch 写 1000 万美元，Crunchbase News 写 约 1200 万美元（原 €1000 万，按 10-30 汇率）"


def test_unconfirmed_currency_does_not_enter_ten_percent_comparison(tmp_path):
    session = rates(tmp_path)
    with connection(tmp_path) as conn:
        eid = add(conn, session, amount="$12M", source="cbn_main")
        add(conn, session, amount="999 million", source="tc_venture")
        add(conn, session, amount="€10M", source="cbn_venture")
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["amount_status"] == "披露" and row["amount_usd"] == 12e6
        assert "金额待核" not in json.loads(row["marks_json"])


def test_malformed_cache_and_atomic_write_failure_do_not_block(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "test")
    cfg.rates_file.parent.mkdir()
    cfg.rates_file.write_text('{"rates": {"2026-10-30": {"USD": 1.2}}, "coverage": null}')
    session = fx.FxRates(cfg, Fetcher(), now=NOW)
    errors = []
    session.log = errors.append
    def refused(*args):
        raise OSError("合成的磁盘写入失败")
    monkeypatch.setattr(fx.os, "replace", refused)
    snapshot = session.snapshot(NOW.date())
    assert snapshot["rates"]["USD"] == 1
    assert errors[0]["event"] == "fx_cache_error"
    assert set(cfg.rates_file.parent.iterdir()) == {cfg.rates_file}


def test_old_database_migration_and_idempotence(tmp_path):
    path = tmp_path / "legacy.db"
    columns = {"amount_currency", "amount_original", "amount_usd", "fx_rate", "fx_date", "fx_source", "fx_stale"}
    old = db.connect(path)
    for column in columns:
        old.execute(f"ALTER TABLE funding_event DROP COLUMN {column}")
    old.execute("INSERT INTO company(id,name_key,name,first_seen) VALUES ('old','old','Old','2026-10-30')")
    for text in ("$17M", "€11M"):
        old.execute("INSERT INTO funding_event(company_id,round_norm,amount_text,amount_status,event_date,first_seen) "
                    "VALUES ('old','种子轮',?,'披露','2026-10-30','2026-10-30')", (text,))
    old.commit()
    old.close()
    migrated = db.connect(path)
    assert columns <= {row[1] for row in migrated.execute("PRAGMA table_info(funding_event)")}
    usd, eur = migrated.execute("SELECT * FROM funding_event ORDER BY id").fetchall()
    assert (usd["amount_currency"], usd["amount_original"], usd["amount_usd"], usd["fx_rate"], usd["fx_source"]) == ("USD", 17e6, 17e6, 1, "原文美元")
    assert (eur["amount_currency"], eur["amount_original"], eur["amount_status"]) == ("EUR", 11e6, "暂未换算")
    assert all(eur[key] is None for key in columns - {"fx_stale", "amount_currency", "amount_original"})
    before = [dict(usd), dict(eur)]
    migrated.close()
    with db.connect(path) as reopened:
        assert [dict(row) for row in reopened.execute("SELECT * FROM funding_event ORDER BY id")] == before


def test_rule_extracts_foreign_and_unknown_currency_verbatim():
    for text in ("€11M", "£900,000", "C$5 million", "₹5 million", "5 million euros", "5 million"):
        item = RawItem("tc_venture", "1", "https://synthetic.example/", f"Aster raised {text} in seed funding", NOW)
        assert extract.rule_extract(item).amount_text == text


def test_failure_cache_uses_latest_run_day_cache_for_old_announcement(tmp_path):
    rates(tmp_path).refresh()
    session = rates(tmp_path, Fetcher({fx.DAILY_URL: TimeoutError()}))
    assert session.snapshot(date(2026, 10, 29))["date"] == "2026-10-30"
    # 失败缓存没有另设最大年龄，但显示的日期必须是真实的旧汇率日。
    stale = session.snapshot(date(2026, 12, 1))
    assert stale["date"] == "2026-10-30" and stale["stale"] == 1


def test_missing_currency_has_no_usd_and_does_not_block_other_events(tmp_path):
    session = rates(tmp_path, Fetcher({fx.DAILY_URL: '<Cube time="2026-10-30"><Cube currency="USD" rate="1.2"/></Cube>'}))
    with connection(tmp_path) as conn:
        add(conn, session, amount="CAD 5M", name="MissingRate")
        add(conn, session, amount="$6M", name="Dollar")
        cards = render.sort_cards(scan.collect_listing(conn, NOW.date(), weekly=True)["cards"])
        assert cards[0].name == "Dollar" and cards[1].amount_label == "C$500 万（暂未换算成美元）"
        assert cards[1].amount_status == "暂未换算" and cards[1].amount_usd is None


def test_failed_conversion_recovers_on_next_ingestion(tmp_path):
    failed = rates(tmp_path, Fetcher({fx.DAILY_URL: HttpResponse(fx.DAILY_URL, 503)}))
    good = rates(tmp_path)
    with connection(tmp_path) as conn:
        eid = add(conn, failed)
        add(conn, good, source="cbn_main")
        assert good.requested == {fx.DAILY_URL, fx.HISTORY_90D_URL}
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["amount_usd"] == pytest.approx(13.2e6) and row["fx_rate"] == pytest.approx(1.2)
        assert row["fx_stale"] == 1 and row["amount_status"] == "披露"


def test_stale_conversion_is_frozen_after_cache_recovers(tmp_path):
    rates(tmp_path).refresh()
    stale = rates(tmp_path, Fetcher({fx.DAILY_URL: HttpResponse(fx.DAILY_URL, 503)}))
    recovered = rates(tmp_path, Fetcher({fx.DAILY_URL: DAILY.replace('rate="1.2"', 'rate="2"')}))
    with connection(tmp_path) as conn:
        eid = add(conn, stale)
        add(conn, recovered, source="cbn_main")
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["fx_stale"] == 1 and row["amount_usd"] == pytest.approx(13.2e6)
        assert "按 10-30 汇率" in scan.amount_label(conn, row)


def test_site_week_history_conflicts_and_status_use_frozen_amounts(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path, test_mode=True)
    monkeypatch.setattr(site, "LINK_RECORDS", tmp_path / "missing-links.json")
    rates(tmp_path).refresh()
    session = rates(tmp_path, Fetcher({fx.DAILY_URL: HttpResponse(fx.DAILY_URL, 503)}))
    with connection(tmp_path) as conn:
        euro = add(conn, session, name="Euro", amount="€10M")
        gbp = add(conn, session, name="Pound", amount="£8M")
        add(conn, session, name="Dollar", amount="$11M")
        add(conn, session, name="Unknown", amount="5 million")
        add(conn, session, name="Undisclosed", amount=None)
        scan.upsert_digest(conn, {"date": date(2026, 10, 30), "push": True, "event_ids": [euro, gbp],
                                  "generated_at": NOW, "status": "有新增", "text": ""})
        add(conn, session, name="Euro", amount="$20M", source="cbn_main")
        conn.execute("UPDATE company SET summary='融资 €10M，原句留存。' WHERE id=(SELECT company_id FROM funding_event WHERE id=?)", (euro,))
        outbox.write_atomic(tmp_path / "test" / "status.json", {"fx": session.status})
        path = site.build_site(conn, cfg, now=NOW)
        data = json.loads((path / "data.json").read_text())
        by_id = {e["id"]: e for e in data["events"]}
        assert site._snapshot(conn, cfg, NOW)["status"]["fx"]["ok"] is False
        assert site._snapshot(conn, cfg, NOW)["status"]["fx"]["date"] == "2026-10-30"
        assert "status" not in data
        assert by_id[euro]["amount"] == "TechCrunch 写 约 1200 万美元（原 €1000 万，按 10-30 汇率），Crunchbase News 写 2000 万美元"
        assert by_id[gbp]["amount"] == "约 1200 万美元（原 £800 万，按 10-30 汇率）"
        assert "融资 约 1200 万美元（原 €1000 万，按 10-30 汇率）" in by_id[euro]["summary"]
        detail_path = next(c["detail_url"] for c in data["companies"] if c["id"] == by_id[euro]["company_id"])
        detail = json.loads((path / detail_path).read_text())
        assert detail["events"][0]["amount"] == by_id[euro]["amount"]
        assert detail["companies"][0]["conflicts"][0]["values"][0]["value"] == "约 1200 万美元（原 €1000 万，按 10-30 汇率）"
        positions = {eid: index for index, eid in enumerate(data["listing"])}
        assert positions[gbp] < positions[euro]
        assert "status.fx.message" not in (path / "app.js").read_text()


@pytest.mark.parametrize("cache_available", [False, True])
def test_first_issue_pipeline_fx_failure_isolated_and_not_in_first_lines(tmp_path, monkeypatch, cache_available):
    from test_pipe_pipeline import write_fixture
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    if cache_available:
        seeded = rates(cfg.state_dir)
        seeded.refresh()
    fixture = write_fixture(tmp_path / "fixtures", day="2026-10-30")
    manifest_path = fixture / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for source in P0_SOURCES:
        if source.kind == "rss":
            manifest[source.url]["body"] = manifest[source.url]["body"].replace("$2M", "€10M")
    manifest[fx.DAILY_URL] = {"status": 503}
    manifest_path.write_text(json.dumps(manifest))
    calls = []
    original_get = http.FixtureTransport.get
    def get(self, url, **kwargs):
        calls.append(url)
        return original_get(self, url, **kwargs)
    monkeypatch.setattr(http.FixtureTransport, "get", get)
    args = cli.parse_args(["--first-issue", "--fixture", str(fixture), "--dry-run", "--no-deep", "--rates-fail"])
    clock = pipeline.Clock(date(2026, 10, 30), datetime.min.time().replace(hour=8, minute=40))
    assert pipeline.run(cfg, args, clock) == 0
    handoff = json.loads(next(cfg.outbox_dir.glob("*-first.json")).read_text())
    assert "汇率" not in "\n".join(handoff["text"].splitlines()[:3])
    with db.connect(cfg.db_path) as conn:
        event = conn.execute("SELECT e.* FROM funding_event e JOIN company c ON c.id=e.company_id WHERE c.name='Anchor'").fetchone()
        assert event["fx_stale"] == 1
        assert event["amount_usd"] == (pytest.approx(12e6) if cache_available else None)
    status = json.loads((cfg.state_dir / "test" / "status.json").read_text())
    assert status["fx"]["ok"] is False
    assert calls.count(fx.DAILY_URL) == 0
    assert not (cfg.state_dir / "rates.json").exists()
    records = [json.loads(line) for line in (cfg.log_dir / "2026-10-30.log").read_text().splitlines()]
    assert sum(record["event"] == "fx_fetch_failed" for record in records) == 0


def test_replay_accepts_fixed_rates_without_network(tmp_path, monkeypatch, capsys):
    cfg = Config(state_dir=tmp_path / "state")
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: cfg)
    payload = {"date": "2026-10-30", "fixed_rates": {"base":"USD", "source":"手写固定汇率", "days":{"2026-10-30":{"fetched_at":"2026-10-30 08:40:00", "rates":{"EUR":1/1.2}}}},
               "items": [{"source_id": "tc_venture", "external_id": "euro", "url": "https://synthetic.example/euro",
                          "title": "Euro raises €10M in Seed funding", "published_at": "2026-10-30T07:00:00+08:00",
                          "excerpt": "Euro is a London-based software company."}]}
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(payload))
    assert cli.main(["--replay", str(path), "--no-links"]) == 0
    assert "约 1200 万美元（原 €1000 万）" in capsys.readouterr().out
    assert not (cfg.state_dir / "rates.json").exists()
    assert not (cfg.state_dir / "test" / "rates.json").exists()
