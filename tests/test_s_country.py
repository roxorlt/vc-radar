"""S：配置、首期 81 条总部写法及迁移的离线回归。"""
import json
from pathlib import Path

import pytest

from vcradar import db, extract, scan
from vcradar.models import Extracted, RawItem


# 总负责导出的原值只含地名短语；期望国家独立逐条写定。
HQ_CASES = [
    ("10-month-old London", "英国"),
    ("Addison, TX", "美国"),
    ("Amsterdam, NH, Netherlands", "荷兰"),
    ("Ankara, Ankara, Turkey", "土耳其"),
    ("Ann Arbor, MI, USA", "美国"),
    ("Austin, TX, USA", "美国"),
    ("BY, Germany", "德国"),
    ("Baltimore, MD, USA", "美国"),
    ("Beijing", "中国"),
    ("Bengaluru", "印度"),
    ("Bengaluru, KA, India", "印度"),
    ("Bhubaneswar, India", "印度"),
    ("Boston", "美国"),
    ("Boston, MA, USA", "美国"),
    ("Brno, South Moravian Region, Czechia", "捷克"),
    ("Brooklyn’s Sunset Park", "美国"),
    ("Calgary", "加拿大"),
    ("Chicago startup that is developing a gel", "美国"),
    ("Chicago, IL, USA", "美国"),
    ("Cologne, NRW, Germany", "德国"),
    ("Dallas", "美国"),
    ("Dallas, Texas", "美国"),
    ("Denver, CO, USA", "美国"),
    ("Dresden, SN, Germany", "德国"),
    ("Dublin", "爱尔兰"),
    ("Durham, NC, USA", "美国"),
    ("El Segundo, CA, USA", "美国"),
    ("Emeryville, CA, USA", "美国"),
    ("Emeryville, Calif.", "美国"),
    ("George Town, George Town, Cayman Islands", "开曼群岛"),
    ("Golden, CO", "美国"),
    ("Grass Lake, MI", "美国"),
    ("Helsinki", "芬兰"),
    ("Helsinki, Finland", "芬兰"),
    ("London", "英国"),
    ("London, England, United Kingdom", "英国"),
    ("Los Angeles", "美国"),
    ("Los Angeles, CA, USA", "美国"),
    ("Lund, Skåne County, Sweden", "瑞典"),
    ("Massachusetts", "美国"),
    ("Milan, Lombardy, Italy", "意大利"),
    ("Mountain View, CA", "美国"),
    ("Munich, BY, Germany", "德国"),
    ("Myrtle Beach, SC, USA", "美国"),
    ("New York", "美国"),
    ("New York City", "美国"),
    ("New York City, NY, USA", "美国"),
    ("New York- and Tel Aviv", "国家待确认"),
    ("Oslo, Oslo, Norway", "挪威"),
    ("Ottawa", "加拿大"),
    ("Oxford, England, United Kingdom", "英国"),
    ("Paris", "法国"),
    ("Paris, Île-de-France, France", "法国"),
    ("Phoenix", "美国"),
    ("Phoenix, AZ, USA", "美国"),
    ("Pittsburgh, PA, USA", "美国"),
    ("Providence, RI, USA", "美国"),
    ("Pune, India", "印度"),
    ("Riverside, CA", "美国"),
    ("Rome", "意大利"),
    ("Salt Lake City, UT, USA", "美国"),
    ("San Francisco", "美国"),
    ("San Francisco, CA, USA", "美国"),
    ("Santa Barbara, CA", "美国"),
    ("Santiago, Santiago Metropolitan Region, Chile", "智利"),
    ("Seattle", "美国"),
    ("Seattle, WA, USA", "美国"),
    ("Seoul", "韩国"),
    ("Shanghai", "中国"),
    ("Stockholm, Stockholm County, Sweden", "瑞典"),
    ("Sydney, NSW, Australia", "澳大利亚"),
    ("Tel Aviv-Yafo, Tel Aviv District, Israel", "以色列"),
    ("Texas.↩", "美国"),
    ("Toronto, ON, Canada", "加拿大"),
    ("Vancouver, BC, Canada", "加拿大"),
    ("Virginia", "美国"),
    ("Washington, D.C.", "美国"),
    ("Washington, DC", "美国"),
    ("Waterloo, ON, Canada", "加拿大"),
    ("ZH, Switzerland", "瑞士"),
    ("Zürich, ZH, Switzerland", "瑞士"),
]


@pytest.mark.parametrize("location,expected", HQ_CASES)
def test_first_issue_hq_values(location, expected):
    item = RawItem("yc_directory", "hq", "https://www.ycombinator.com/companies/synthetic", "手写公司", None,
                   structured={"location": location})
    assert scan.judge_country(item, Extracted())[0] == expected
    assert scan.country_from_hq(location, "") == expected


def test_v7_migration_reuses_configuration_and_keeps_legacy_states(tmp_path):
    assert len(HQ_CASES) == 81
    path = tmp_path / "v7.db"
    with db.connect(path) as conn:
        for number, (location, _) in enumerate(HQ_CASES):
            conn.execute("INSERT INTO company(id, name_key, name, first_seen, hq, country, status) "
                         "VALUES (?, ?, '合成公司', '2026-10-30', ?, '国家待确认', '总部待核')",
                         (str(number), str(number), location))
        conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    with db.connect(path) as conn:
        before = [dict(row) for row in conn.execute("SELECT * FROM company ORDER BY CAST(id AS INTEGER)")]
        assert [row["country"] for row in before] == [country for _, country in HQ_CASES]
        assert all(row["status"] == "总部待核" for row in before)
    with db.connect(path) as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM company ORDER BY CAST(id AS INTEGER)")] == before


@pytest.mark.parametrize("place,country", [
    ("Colombia", "哥伦比亚"), ("Cayman Islands", "开曼群岛"), ("Cambridge, MA", "美国"),
    ("Cambridge, UK", "英国"), ("Cambridge", "国家待确认"), ("Waterloo", "国家待确认"),
    ("Taiwan", "中国台湾"), ("Hong Kong", "中国香港"), ("Macao", "中国澳门"),
    ("Czech Republic", "捷克"), ("Türkiye", "土耳其"), ("UAE", "阿联酋"), ("Britain", "英国"),
    ("New Mexico", "美国"), ("Georgia", "国家待确认"), ("Georgia, USA", "美国"),
])
def test_country_aliases_and_ambiguous_cities(place, country):
    item = RawItem("news", "place", "https://synthetic.example/place", "公司消息", None)
    assert scan.judge_country(item, Extracted(company_name="合成公司", hq_text=place))[0] == country


def test_colombia_based_finmaq_and_unknown_city_log(caplog):
    item = RawItem("news", "finmaq", "https://synthetic.example/finmaq", "FinMaq", None,
                   body="FinMaq, a Colombia-based fintech, raised seed funding.")
    assert scan.judge_country(item, extract.rule_extract(item))[0] == "哥伦比亚"
    records = []
    with scan.country_log(records.append):
        assert scan.judge_country(item, Extracted(company_name="FinMaq", hq_text="Unlistedville"))[0] == "哥伦比亚"
        unknown = RawItem("news", "unknown", "https://synthetic.example/unknown", "公司消息", None)
        assert scan.judge_country(unknown, Extracted(company_name="手写公司", hq_text="Unlistedville"))[0] == "国家待确认"
    assert records == [
        {"event": "country_city_unknown", "city": "Unlistedville", "company_name": "FinMaq"},
        {"event": "country_city_unknown", "city": "Unlistedville", "company_name": "手写公司"},
    ]
    assert "Unlistedville" in caplog.text and "手写公司" in caplog.text


@pytest.mark.parametrize("bad", [None, "{", '{}', '{"countries": []}'])
def test_bad_or_missing_location_configuration_is_startup_error(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(scan, "_CONFIG_DIR", tmp_path)
    if bad is not None:
        (tmp_path / "country-names.json").write_text(bad)
    with pytest.raises(RuntimeError, match="无法启动"):
        scan._load_locations()


def test_iso_territories_and_initial_city_configuration():
    names = scan._LOCATION_CONFIG["countries"]
    assert len(names) == 249
    for iso, expected in {"CO": "哥伦比亚", "KY": "开曼群岛", "TW": "中国台湾", "HK": "中国香港", "MO": "中国澳门"}.items():
        assert names[iso]["name"] == expected
    cities = json.loads((Path(scan.__file__).resolve().parent.parent / "config/city-country.json").read_text())
    assert cities["Cambridge"] == cities["Waterloo"] == "有歧义"
    for city in ["Addison", "Ann Arbor", "Brno", "Bhubaneswar", "Emeryville", "Lund", "Santiago"]:
        assert city in cities


def test_legacy_source_country_cannot_undo_table_migration_on_next_merge(tmp_path):
    from test_core_scan import NOW
    from vcradar.sources.registry import P0_SOURCES
    path = tmp_path / "legacy.db"
    with db.connect(path) as conn:
        db.sync_sources(conn, P0_SOURCES)
        item = RawItem("tc_venture", "old", "https://synthetic.example/old", "FinMaq", NOW,
                       body="FinMaq, a Colombia-based fintech, raised seed funding.")
        ext = Extracted(company_name="FinMaq", round_text="Seed", hq_text="Colombia")
        raw_id, _ = scan.upsert_raw_item(conn, item, NOW, False)
        eid = scan.ingest(conn, raw_id, item, ext, now=NOW, is_baseline=False, is_backfill=False,
                          latest_yc_batches=[], model=None)
        source = conn.execute("SELECT * FROM event_source WHERE event_id=?", (eid,)).fetchone()
        data = json.loads(source["extraction_json"])
        data["_country"] = "国家待确认"
        conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=?", (json.dumps(data), eid))
        conn.execute("UPDATE company SET hq='Colombia', country='国家待确认'")
        conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    with db.connect(path) as conn:
        assert conn.execute("SELECT country FROM company").fetchone()[0] == "哥伦比亚"
        item = RawItem("cbn_main", "new", "https://synthetic.example/new", "FinMaq", NOW)
        raw_id, _ = scan.upsert_raw_item(conn, item, NOW, False)
        scan.ingest(conn, raw_id, item, Extracted(company_name="FinMaq", round_text="Seed"), now=NOW,
                    is_baseline=False, is_backfill=False, latest_yc_batches=[], model=None)
        assert conn.execute("SELECT country FROM company").fetchone()[0] == "哥伦比亚"
