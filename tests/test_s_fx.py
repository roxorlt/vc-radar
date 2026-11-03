"""S：V16e 金额边界、币种上下文、缓存迁移与补算冻结。"""
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from test_v16_fx import NOW, DAILY, Fetcher, add, connection, offline, rates
from vcradar import config, db, fx, render, scan, site
from vcradar.config import Config
from vcradar.models import DigestData, HttpResponse


@pytest.mark.parametrize("text,label", [
    ("$8,000.5", "8001 美元"), ("$9,999.4", "9999 美元"), ("$9,999.6", "1 万美元"),
    ("$12,345.6", "1 万美元"), ("$2.5M", "250 万美元"), ("$27M", "2700 万美元"),
    ("$99,994,999", "9999 万美元"), ("$99,996,000", "1 亿美元"),
    ("$120M", "1.2 亿美元"), ("$125M", "1.3 亿美元"), ("$1B", "10 亿美元"),
    ("$1.25B", "12.5 亿美元"), ("$0.49999999999999999999", "0 美元"),
    ("$9,999.49999999999999999", "9999 美元"),
])
def test_exact_decimal_writing_boundaries(text, label):
    assert fx.format_amount(fx.convert_amount(text), text) == label
    assert fx.format_amount_text(text) == label


@pytest.mark.parametrize("currency,prefix", [
    ("EUR", "€"), ("GBP", "£"), ("INR", "₹"), ("CAD", "C$"), ("AUD", "A$"),
    ("SGD", "S$"), ("HKD", "HK$"), ("KRW", "₩"), ("ILS", "₪"),
    ("CNY", "人民币 "), ("JPY", "日元 "), ("CHF", "瑞士法郎 "),
])
def test_original_prefixes_use_same_rounding_algorithm(currency, prefix):
    assert fx.format_original(currency, Decimal("8000.5")) == prefix + "8001"
    assert fx.format_original(currency, Decimal("9999.6")) == prefix + "1 万"
    assert fx.format_original(currency, Decimal("99996000")) == prefix + "1 亿"
    assert "¥" not in fx.format_original(currency, 125e6)


_CURRENCY_ROWS = json.loads((Path(__file__).resolve().parent.parent / "config/currencies.json").read_text())


@pytest.mark.parametrize("alias,entry", list(_CURRENCY_ROWS.items()))
def test_every_configured_currency_alias(alias, entry):
    assert scan.parse_amount(alias + " 3 million") == (entry["code"], 3e6)


@pytest.mark.parametrize("text,currency,amount", [
    ("Rs5 crore", "INR", 50e6), ("Rs. 3 lakh", "INR", 300000),
    ("5 crore rupees", "INR", 50e6), ("5 crore dollars", None, 50e6), ("5 lakh euros", None, 500000),
    ("5mn dollars", "USD", 5e6), ("USD5MM", "USD", 5e6), ("CA$3M", "CAD", 3e6),
    ("AU$8mn", "AUD", 8e6), ("NIS2MM", "ILS", 2e6), ("8M won", "KRW", 8e6),
    ("3000万美元", "USD", 30e6), ("2亿元", "CNY", 200e6), ("500万元", "CNY", 5e6),
    ("1.5 亿日元", "JPY", 150e6), ("2千万欧元", "EUR", 20e6), ("2十万元", "CNY", 200000),
    ("3百万元", "CNY", 3e6), ("5万港币", "HKD", 50000), ("5万元人民币", "CNY", 50000),
    ("¥10 million", None, 10e6), ("￥10 million", None, 10e6),
    ("¥10 million yuan", "CNY", 10e6), ("RMB financing of ¥10 million", None, 10e6),
    ("¥10 million yen", "JPY", 10e6), ("￥500 万円", "JPY", 5e6),
    ("¥10 million yuan and yen", "CNY", 10e6), ("¥10 million in RMB or JPY", None, 10e6),
    ("¥10 million with 日元 context", None, 10e6), ("￥500万元", "CNY", 5e6),
    ("raised 10 million", None, 10e6), ("₿5M", None, 5e6),
])
def test_currency_and_quantity_context(text, currency, amount):
    assert scan.parse_amount(text) == (currency, amount)


@pytest.mark.parametrize("text,label", [
    ("¥10 million", "¥1000 万（币种待确认）"), ("￥10 million", "￥1000 万（币种待确认）"),
    ("raised 10 million", "1000 万（币种待确认）"), ("5 million", "500 万（币种待确认）"),
    ("₿8000.5", "₿8001（币种待确认）"),
])
def test_unknown_currency_amount_uses_chinese_quantity(text, label):
    assert fx.format_amount(fx.convert_amount(text), text) == label


@pytest.mark.parametrize("label", [
    "约 1200 万美元（原 €1100 万）", "约 1200 万美元（原 €1100 万，按 10-30 汇率）",
    "€1100 万（暂未换算成美元）", "¥1000 万（币种待确认）", "1000 万（币种待确认）",
    "人民币 500 万（暂未换算成美元）", "日元 3 亿（暂未换算成美元）", "8001 美元", "1 万美元",
])
def test_repeated_formatting_preserves_display_labels(label):
    assert fx.format_amount_text(label) == label


def write_cache(cfg, days):
    cfg.rates_file.parent.mkdir(parents=True, exist_ok=True)
    cache = {"base": "USD", "source": fx.SOURCE, "days": {
        day: {"fetched_at": day + " 08:41:05", "rates": table} for day, table in days.items()}}
    cfg.rates_file.write_text(json.dumps(cache))
    return cache


def test_ecb_usd_base_file_schema_and_existing_dates_are_immutable(tmp_path):
    cfg = Config(state_dir=tmp_path, test_mode=True)
    expected = write_cache(cfg, {"2026-10-30": {"EUR": 0.9, "GBP": 0.75}})
    session = fx.FxRates(cfg, Fetcher(), now=NOW)
    session.refresh()
    cache = json.loads(cfg.rates_file.read_text())
    assert set(cache) == {"base", "source", "days"}
    assert cache["days"] == expected["days"]
    amount = fx.convert_amount("£9M", session.snapshot(date(2026, 10, 30), currencies=("GBP",)))
    assert amount.amount_usd == 12e6
    assert amount.fx_rate == pytest.approx(1 / 0.75)
    fresh = rates(tmp_path / "fresh")
    fresh.refresh()
    table = json.loads(fresh.path.read_text())["days"]["2026-10-30"]
    assert table["fetched_at"] == "2026-10-31 08:40:00"
    assert table["rates"]["EUR"] == pytest.approx(1 / 1.2)
    assert table["rates"]["GBP"] == pytest.approx(0.8 / 1.2)


def test_legacy_cache_converts_once_and_old_event_snapshot_still_works(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "test")
    cfg.rates_file.parent.mkdir()
    old = {"source": fx.DAILY_URL, "fetched_at": "2026-10-30T08:41:05+08:00", "last_ok_date": "2026-10-30",
           "rates": {"2026-10-30": {"EUR": 1, "USD": 1.25, "GBP": 0.75}}, "coverage": {}}
    cfg.rates_file.write_text(json.dumps(old))
    session = fx.FxRates(cfg, Fetcher({fx.DAILY_URL: TimeoutError()}), now=NOW)
    converted = json.loads(cfg.rates_file.read_text())
    assert converted == {"base": "USD", "source": fx.SOURCE, "days": {"2026-10-30": {
        "fetched_at": "2026-10-30 08:41:05", "rates": {"EUR": 0.8, "USD": 1, "GBP": 0.6}}}}
    assert fx.convert_amount("€10M", session.snapshot(NOW.date())).amount_usd == 12.5e6
    def refuse(*args):
        raise AssertionError("已转换的缓存不应再次写入")
    monkeypatch.setattr(fx.os, "replace", refuse)
    fx.FxRates(cfg, Fetcher(), now=NOW)
    frozen = {"rates": {"EUR": 1, "USD": 1.25, "GBP": 0.75}, "date": "2026-10-30"}
    assert fx.convert_amount("£6M", frozen).amount_usd == 10e6


@pytest.mark.parametrize("payload", [None, "", "{", '{"base":"USD","source":"合成汇率","days":{}}',
    '{"base":"USD","days":{"2026-10-30":{"fetched_at":"bad","rates":{"EUR":1}}}}'])
def test_empty_or_corrupt_cache_displays_clear_status_without_blocking(tmp_path, payload):
    cfg = Config(state_dir=tmp_path, test_mode=True)
    if payload is not None:
        cfg.rates_file.parent.mkdir()
        cfg.rates_file.write_text(payload)
    assert fx.status_from_files(cfg)["message"] == "汇率没有取到"
    session = fx.FxRates(cfg, Fetcher({fx.DAILY_URL: TimeoutError()}), now=NOW)
    with connection(tmp_path) as conn:
        eid = add(conn, session, amount="€11M")
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["amount_status"] == "暂未换算" and row["amount_currency"] == "EUR"
        assert row["amount_original"] == 11e6 and row["amount_usd"] is None
        assert "金额待核" not in json.loads(row["marks_json"])
        assert scan.amount_label(conn, row) == "€1100 万（暂未换算成美元）"
        assert session.status["message"] == "汇率没有取到"


def test_no_rate_and_unknown_currency_share_sort_bucket_and_never_compare(tmp_path, monkeypatch):
    session = rates(tmp_path, Fetcher({fx.DAILY_URL: HttpResponse(fx.DAILY_URL, 503)}))
    cfg = Config(state_dir=tmp_path, test_mode=True)
    monkeypatch.setattr(site, "LINK_RECORDS", tmp_path / "no-links.json")
    with connection(tmp_path) as conn:
        add(conn, session, name="Unknown", amount="10 billion")
        add(conn, session, name="Unconverted", amount="€8B")
        add(conn, session, name="Undisclosed", amount=None)
        add(conn, session, name="Disputed", amount="$5M")
        add(conn, session, name="Disputed", amount="$10M", source="cbn_main")
        eid = add(conn, session, name="Comparable", amount="$6M", source="cbn_main")
        add(conn, session, name="Comparable", amount="€100B", source="tc_venture")
        listing = scan.collect_listing(conn, NOW.date(), weekly=True)
        cards = render.sort_cards(listing["cards"], first_seen=listing["first_seen"])
        assert [c.name for c in cards] == ["Comparable", "Disputed", "Unconverted", "Unknown", "Undisclosed"]
        assert cards[2].amount_status == "暂未换算" and not cards[2].marks
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["amount_status"] == "披露" and row["amount_usd"] == 6e6
        path = site.build_site(conn, cfg, now=NOW)
        data = json.loads((path / "data.json").read_text())
        assert [next(e["name"] for e in data["events"] if e["id"] == eid) for eid in data["listing"]] == [c.name for c in cards]
        unconverted = next(e for e in data["events"] if e["name"] == "Unconverted")
        assert unconverted["amount"] == "€80 亿（暂未换算成美元）"
        assert site._snapshot(conn, cfg, NOW)["status"]["fx"]["message"] == "汇率没有取到"
        assert "status" not in data


def test_unconverted_rows_recover_without_new_source_and_then_freeze(tmp_path):
    failed = rates(tmp_path, Fetcher({fx.DAILY_URL: TimeoutError()}))
    good = rates(tmp_path)
    with connection(tmp_path) as conn:
        eid = add(conn, failed, amount="€50M")
        scan.mark_listed(conn, [eid], NOW.date())
        before = dict(conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone())
        scan.retry_unconverted_amounts(conn, good)
        after = dict(conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone())
        assert after["amount_status"] == "披露" and after["amount_usd"] == pytest.approx(60e6)
        assert "大额" in json.loads(after["marks_json"])
        for key in ["pending", "push_date", "last_pushed_date", "first_ingested_at", "first_seen"]:
            assert before[key] == after[key]
        assert scan.collect_listing(conn, (NOW + timedelta(days=7)).date(), weekly=True)["cards"] == []
        changed = rates(tmp_path, Fetcher({fx.DAILY_URL: DAILY.replace('rate="1.2"', 'rate="2.0"')}))
        scan.retry_unconverted_amounts(conn, changed)
        assert dict(conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()) == after
        assert not changed.requested
        scan.revalue_amounts(conn, eid, changed)
        revalued = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        # 同一天的缓存不可被新返回覆盖，显式重判仍使用已缓存的日期。
        assert revalued["amount_usd"] == pytest.approx(after["amount_usd"])


def test_missing_currency_uses_earlier_available_currency_rate(tmp_path):
    cfg = Config(state_dir=tmp_path, test_mode=True)
    write_cache(cfg, {"2026-10-29": {"EUR": 0.8, "CAD": 1.25}, "2026-10-30": {"EUR": 0.9}})
    session = fx.FxRates(cfg, Fetcher({fx.DAILY_URL: TimeoutError()}), now=NOW)
    snapshot = session.snapshot(date(2026, 10, 30), currencies=("CAD",))
    assert snapshot["date"] == "2026-10-29" and snapshot["stale"] == 1
    assert fx.convert_amount("CAD 5M", snapshot).amount_usd == 4e6


def test_stale_date_only_in_cards_and_details_not_push(tmp_path):
    session = rates(tmp_path)
    with connection(tmp_path) as conn:
        add(conn, session, amount="€10M")
        card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
        assert "按 10-30 汇率" in render.render_card(card, "")
        digest = DigestData(NOW.date(), None, NOW, "有新增", cards=[card])
        push = render.render_push(digest)
        assert "约 1200 万美元（原 €1000 万）" in push
        assert "汇率" not in push


def test_v7_missing_rate_migration_does_not_change_real_conflicts(tmp_path):
    path = tmp_path / "data.db"
    with connection(tmp_path) as conn:
        missing = add(conn, None, amount="€11M", name="Missing")
        conflict = add(conn, None, amount="$5M", name="Conflict")
        add(conn, None, amount="$10M", name="Conflict", source="cbn_main")
        conn.execute("UPDATE funding_event SET amount_status='金额待核', amount_currency=NULL, amount_original=NULL, "
                     "marks_json='[\"金额待核\"]' WHERE id=?", (missing,))
        conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    with db.connect(path) as conn:
        migrated = conn.execute("SELECT * FROM funding_event WHERE id=?", (missing,)).fetchone()
        assert migrated["amount_status"] == "暂未换算" and migrated["amount_currency"] == "EUR"
        assert "金额待核" not in json.loads(migrated["marks_json"])
        assert conn.execute("SELECT amount_status FROM funding_event WHERE id=?", (conflict,)).fetchone()[0] == "金额待核"
        scan.retry_unconverted_amounts(conn, rates(tmp_path))
        assert conn.execute("SELECT amount_status FROM funding_event WHERE id=?", (missing,)).fetchone()[0] == "披露"


@pytest.mark.parametrize("payload", [None, "{", "{}", '{"x":{"code":"bad","name":"名称","prefix":"x"}}'])
def test_bad_currency_configuration_fails_startup(tmp_path, payload):
    path = tmp_path / "currencies.json"
    if payload is not None:
        path.write_text(payload)
    with pytest.raises(RuntimeError, match="无法启动"):
        fx._load_currencies(path)


@pytest.mark.parametrize("context,excerpt,currency,value", [
    ("The company reports financing in RMB. It raised ¥10 million.", "", None, None),
    ("The company raised ¥10 million, denominated in yen.", "", None, None),
    ("The company raised ¥10 million; yuan and yen are both mentioned.", "", None, None),
    ("The company raised ¥10 million; yuan and yen are both mentioned.", "yuan financing", None, None),
])
def test_yen_symbol_does_not_borrow_distant_currency_words(tmp_path, context, excerpt, currency, value):
    from vcradar.models import Extracted, RawItem
    xml = '<Cube time="2026-10-30"><Cube currency="USD" rate="1"/><Cube currency="CNY" rate="10"/><Cube currency="JPY" rate="100"/></Cube>'
    session = rates(tmp_path, Fetcher({fx.DAILY_URL: xml}))
    with connection(tmp_path) as conn:
        item = RawItem("tc_venture", "symbol", "https://synthetic.example/symbol", "合成公司融资", NOW, excerpt=excerpt, body=context)
        ext = Extracted(company_name="合成公司", round_text="Seed", amount_text="¥10 million", hq_text="London")
        raw_id, _ = scan.upsert_raw_item(conn, item, NOW, False)
        eid = scan.ingest(conn, raw_id, item, ext, now=NOW, is_baseline=False, is_backfill=False,
                          latest_yc_batches=[], model=None, fx_rates=session)
        row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
        assert row["amount_currency"] == currency and row["amount_usd"] == value
        assert row["amount_text"] == "¥10 million"
        assert fx.parse_amount("¥10 million", context=context)[0] == currency
        if currency:
            assert "¥" not in scan.amount_label(conn, row)
        else:
            assert scan.amount_label(conn, row) == "¥1000 万（币种待确认）"


def test_unknown_currency_preserves_compound_symbol():
    assert fx.format_amount(fx.convert_amount("CA$5 crore"), "CA$5 crore") == "CA$5000 万（币种待确认）"
