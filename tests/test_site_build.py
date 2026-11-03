"""合成库验证站点快照、SQL 口径和失败回滚；不读取真实业务数据。"""
from datetime import datetime, timedelta
from itertools import product
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from vcradar import cli, db, site
from vcradar.config import BJ, Config
from vcradar.sources.registry import P0_SOURCES


NOW = datetime(2026, 10, 31, 11, 0, tzinfo=BJ)


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def add_company(conn, cid, name, *, status="已纳入", ai="是", social="否", manual=None, country="美国"):
    conn.execute("INSERT INTO company(id,name_key,name,website,summary,hq,hq_basis,first_seen,"
                 "ai_label,social_label,manual_label,status,pending_reason,country) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, cid, name, f"https://{cid}.ai/", "这是手写的合成简介。公司为团队提供软件。融资用于产品研发。",
                  "美国 · 旧金山", "来源写总部在旧金山", "2026-10-19", ai, social, manual,
                  status, "总部来源都没写" if status == "总部待核" else "来源没写轮次" if status == "轮次待核" else "",
                  "国家待确认" if status == "总部待核" else country))


def add_event(conn, eid, cid, *, pushed="2026-10-30", amount="$20M", amount_status="披露", marks=(),
              round_norm="种子轮", filtered=(), seen="2026-10-30 08:41:00", source="tc_venture"):
    conn.execute("INSERT INTO funding_event(id,company_id,round_norm,round_text,amount_text,amount_status,"
                 "leads_json,follows_json,event_date,first_seen,first_ingested_at,marks_json,push_date,filter_reasons_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (eid, cid, round_norm, "seed funding" if round_norm == "种子轮" else "Series A", amount,
                  amount_status, dump(["First Round Capital"]), dump(["Bessemer Venture Partners"]),
                  "2026-10-30", seen, seen, dump(list(marks)), pushed, dump(list(filtered))))
    url = f"https://techcrunch.com/synthetic-funding-{eid}/"
    conn.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,published_at,first_seen_at,"
                 "original_date_et,structured_json) VALUES (?,?,?,?,?,?,?,?,?)",
                 (eid, source, str(eid), url, "手写融资信息", "2026-10-29T23:15:00+00:00", seen,
                  "2026-10-29", dump({"website": f"https://{cid}.ai/"})))
    conn.execute("INSERT INTO event_source VALUES (?,?,?,?,?,?,?)",
                 (eid, eid, url, "TechCrunch", dump({"website": f"https://{cid}.ai/", "amount_text": amount}), 1, None))


def add_deep(conn, cid, status, *, found=None, run_date="2026-10-31"):
    points = {f"item{i}": {"points": [{"zh": f"第 {i} 项手写要点：Mach9 为团队提供软件。",
               "evidence": [{"page": 1, "quote": "Mach9 builds software for teams."}],
               "entities": [{"name": "Mach9", "url": "https://docs.mach9.ai/product?utm_source=test&doc=1#start"},
                            {"name": "First Round Capital", "url": "https://firstround.com/?utm_source=test"},
                            {"name": "资料", "url": "https://crunchbase.com/organization/mach9"},
                            {"name": "未知对象", "url": "https://not-in-material.ai/"}]}]} for i in range(1, 9)}
    points["item3"] = {"points": [], "missing": ["单价"]}
    conn.execute("INSERT INTO deepdive(company_id,run_date,status,result_json,push_judgement_json,"
                 "new_evidence_json,new_evidence_found_at,started_at,ended_at,online_at,estimated_cost,quality_pending) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, run_date, status, dump({"items": points}),
                  dump({"name": "Mach9" if cid == "mach9" else cid.title(), "marks": ["新一轮"], "ai_label": "是", "social_label": "否"}),
                  dump([{"field": "hq", "page": 1, "quote": "Mach9 builds software for teams."}]) if found else "[]",
                  found, f"{run_date} 09:00:00", f"{run_date} 09:01:00", f"{run_date} 09:02:00" if status == "完成" else None,
                  0.02, 1 if cid == "mach9" else 0))
    conn.execute("INSERT INTO deepdive_pages VALUES (?,?,?,?,?,?,?,?)",
                 (cid, run_date, 1, "https://mach9.ai/news/funding", f"{run_date} 09:00:05", 200,
                  "Mach9 builds software for teams.", dump([{"url": "https://mach9.ai/about", "text": "公司"}])))


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, P0_SOURCES)
    for cid, name, kwargs in [
        ("terse", "Terse", {}), ("mach9", "Mach9", {}), ("flapping", "Flapping Airplanes", {}),
        ("dream", "Dream", {"status": "总部待核", "social": "待核"}),
        ("viraj", "Viraj Aero", {"status": "轮次待核"}), ("quippy", "Quippy", {"ai": "否"}),
        ("wingman", "Wingman Law", {}), ("mochi", "Mochi", {"social": "待核"}),
        ("capveon", "Capveon", {}), ("talos", "Talos", {}), ("hue", "Hue", {}),
        ("aviern", "Aviern", {"ai": "否"}), ("confetti", "Confetti", {}), ("faraday", "Faraday", {}),
        ("nearby", "TryNearby", {"ai": "待核", "social": "待核", "manual": "社交/社群相关"}),
    ]:
        add_company(conn, cid, name, **kwargs)
    configs = [
        (1, "terse", {"pushed": "2026-10-23", "amount": None, "amount_status": "未披露"}),
        (2, "mach9", {"amount": "$34M", "marks": ["新一轮"]}),
        (3, "flapping", {"amount": "$1B", "marks": ["大额"], "round_norm": "A 轮"}),
        (4, "dream", {}), (5, "viraj", {"round_norm": "待核"}),
        (6, "quippy", {"filtered": ["总部非美国", "轮次超范围"]}),
        (7, "wingman", {"pushed": "2026-09-30"}), (8, "mochi", {}),
        (9, "capveon", {"amount": "TechCrunch 写 $10M，Crunchbase News 写 $12M", "amount_status": "金额待核"}),
        (10, "talos", {}), (11, "hue", {}), (12, "aviern", {"amount": None, "amount_status": "未披露"}),
        (13, "confetti", {"pushed": "2026-10-01"}), (14, "faraday", {"pushed": "2026-10-02"}),
        (15, "mach9", {"amount": "$5M", "round_norm": "天使轮"}),
        (16, "nearby", {"pushed": None, "seen": "2026-10-31 08:42:00"}),
    ]
    for eid, cid, kwargs in configs:
        add_event(conn, eid, cid, **kwargs)
    conn.execute("UPDATE raw_item SET is_baseline=1,source_id='yc_directory',first_seen_at='2026-10-19 07:42:00',"
                 "published_at='2026-09-30 07:00:00',original_date_et='2026-09-29' WHERE id=1")
    conn.execute("UPDATE funding_event SET yc_batch='Fall 2026' WHERE id=1")
    # 故意打乱摘要事件的顺序，页面须按 PRD 排序。
    ids = [6, 4, 5, 2, 3, 1, 8, 9, 10, 11, 12, 15]
    for day, push, event_ids, window in [
        ("2026-10-16", 1, [7], None), ("2026-10-23", 1, [1, 13, 14], "2026-10-16 08:55:00"),
        ("2026-10-30", 1, ids, "2026-10-16 08:55:00"), ("2026-10-31", 0, [16], None),
    ]:
        conn.execute("INSERT INTO digest(date,push,event_ids_json,generated_at,window_start,note) VALUES (?,?,?,?,?,?)",
                     (day, push, dump(event_ids), f"{day} 08:55:00", window, "首期" if day == "2026-10-16" else ""))
    for cid, status in [("mach9", "完成"), ("capveon", "生成中"), ("talos", "失败"), ("hue", "暂时没有"), ("aviern", "不做")]:
        add_deep(conn, cid, status, found="2026-10-31 09:01:00" if cid == "mach9" else None)
    add_deep(conn, "terse", "完成", run_date="2026-10-20", found="2026-10-20 09:01:00")
    for sid in [s.id for s in P0_SOURCES]:
        conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,http_code,count,result,latest_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", ("synthetic", sid, "2026-10-30 08:40:00", "2026-10-30 08:41:00", 200, 10,
                                                "成功", "2026-10-29 23:15:00+00:00"))
    conn.execute("UPDATE source SET crawl_status='已停抓',enabled=0,stop_reason='封禁',stopped_at='2026-10-30 08:43:00' WHERE id='ts_web'")
    conn.execute("UPDATE fetch_run SET http_code=403,result='已停抓',note='HTTP 403' WHERE source_id='ts_web'")
    conn.execute("UPDATE fetch_run SET result='失败',error_type='rate_limited',http_code=429,missed_range='10-29 08:40 至 10-30 08:40' WHERE source_id='yc_launches'")
    conn.commit()
    for name, cost in [("deep", 2.75), ("extract", 1.25)]:
        (tmp_path / f"{name}-usage.json").write_text(dump({"months": {"2026-10": {"estimated_cost": cost}, "2026-09": {"estimated_cost": 99}}}))
    records = tmp_path / "links.json"
    records.write_text(dump([
        {"name": "First Round Capital", "company": "Mach9", "url": "https://firstround.com/", "verified": True, "verified_at": "2026-10-30 10:00"},
    ]))
    monkeypatch.setattr(site, "LINK_RECORDS", records)
    yield conn, cfg
    conn.close()


def snapshot(seeded, **kwargs):
    conn, cfg = seeded
    path = site.build_site(conn, cfg, now=NOW, **kwargs)
    return path, json.loads((path / "data.json").read_text())


def node(script, *args):
    executable = shutil.which("node")
    if not executable:
        pytest.skip("未安装用于离线页面验证的 node")
    run = subprocess.run([executable, "-e", script, str(site.ASSETS / "app.js"), *map(str, args)],
                         text=True, capture_output=True, check=True)
    return json.loads(run.stdout)


def render_page(path, route, cid=None):
    return node("const ui=require(process.argv[1]),fs=require('fs'),path=require('path');"
                "const d=JSON.parse(fs.readFileSync(process.argv[2]));"
                "const load=ui.createDetailLoader(d,async url=>({ok:true,json:async()=>JSON.parse("
                "fs.readFileSync(path.join(path.dirname(process.argv[2]),decodeURIComponent(url))))}));"
                "(async()=>{const view=process.argv[3]==='detailHtml'?await load(process.argv[4]):d;"
                "console.log(JSON.stringify(ui[process.argv[3]](view||d,process.argv[4])));})();",
                path / "data.json", route, cid or "")


def company_detail(path, cid):
    """读取快照中自足的公司详情，供离线断言使用。"""
    return json.loads((path / "c" / f"{cid}.json").read_bytes())["companies"][0]


def test_snapshot_week_and_ratio_match_independent_sql(seeded):
    conn, _ = seeded
    path, data = snapshot(seeded)
    latest = conn.execute("SELECT * FROM digest WHERE push=1 ORDER BY date DESC LIMIT 1").fetchone()
    ids = json.loads(latest["event_ids_json"])
    assert data["week"]["date"] == "2026-10-30"
    assert set(data["week"]["event_ids"]) == set(ids)
    assert set(data["week"]["filtered"]) == {6}
    assert "hq_pending" not in data["week"]
    assert 4 in data["week"]["cards"]
    assert data["week"]["round_pending"] == [5]
    assert data["week"]["cards"][0] == 3
    assert 1 in data["week"]["cards"]  # 已推过的事件再次入期也按摘要显示。
    rows = conn.execute("SELECT DISTINCT c.id,c.status,c.country,c.ai_label,c.social_label,c.manual_label "
                        "FROM funding_event e JOIN company c ON c.id=e.company_id "
                        "WHERE e.id IN (SELECT value FROM json_each(?)) AND json_array_length(e.filter_reasons_json)=0", (dump(ids),)).fetchall()
    ratio = data["status"]["pending_ratios"]
    assert ratio["denominator"] == len(rows)
    assert ratio["country"]["count"] == sum(r["country"] == "国家待确认" for r in rows)
    assert ratio["round"]["count"] == sum(r["status"] == "轮次待核" for r in rows)
    assert ratio["tag"]["count"] == sum(not r["manual_label"] and "待核" in (r["ai_label"], r["social_label"]) for r in rows)
    assert {p.name for p in path.iterdir()} == {"index.html", "style.css", "app.js", "data.json", "c"}
    assert '<meta name="robots" content="noindex, nofollow">' in (path / "index.html").read_text()
    assert '<meta name="referrer" content="no-referrer">' in (path / "index.html").read_text()
    html = render_page(path, "weekHtml")
    assert html.index("Flapping Airplanes") < html.index("Dream") < html.index("轮次待确认</h2>") < html.index("<summary>已过滤")


@pytest.mark.parametrize("period", ["all", "week", "30", "date:2026-10-16", "date:2026-10-23", "date:2026-10-30"])
def test_all_filter_combinations_match_sql(seeded, period):
    conn, _ = seeded
    for eid, cid, options in [(101, "baseline-ai", {}),
                              (102, "baseline-hq", {"status": "总部待核", "social": "待核"}),
                              (103, "baseline-round", {"status": "轮次待核", "manual": "社交/社群相关"}),
                              (104, "baseline-filtered", {})]:
        add_company(conn, cid, cid.title(), **options)
        add_event(conn, eid, cid, pushed=None, round_norm="待核" if eid == 103 else "种子轮",
                  filtered=["总部非美国"] if eid == 104 else [])
        conn.execute("UPDATE funding_event SET baseline_unpushed=1 WHERE id=?", (eid,))
        conn.execute("UPDATE raw_item SET is_baseline=1 WHERE id=?", (eid,))
    path, data = snapshot(seeded)
    options = [{"period": period, "tag": tag, "round": round_, "status": status}
               for tag, round_, status in product(["all", "AI相关", "社交/社群相关", "都不是", "标签待确认"],
                                                  ["all", "天使轮", "种子轮", "A 轮", "轮次待确认"],
                                                  ["all", "已纳入", "轮次待确认"])]
    actual = node("const ui=require(process.argv[1]),fs=require('fs'); const d=JSON.parse(fs.readFileSync(process.argv[2]));"
                  "console.log(JSON.stringify(JSON.parse(process.argv[3]).map(f=>ui.selectEvents(d,f).map(e=>e.id))));", path / "data.json", dump(options))
    for filters, selected in zip(options, actual):
        sql = "SELECT e.id FROM funding_event e JOIN company c ON c.id=e.company_id WHERE json_array_length(e.filter_reasons_json)=0"
        sql += " AND (e.baseline_unpushed=0 OR e.push_date IS NOT NULL)"
        params = []
        if period == "week":
            sql += " AND e.id IN (SELECT value FROM json_each((SELECT event_ids_json FROM digest WHERE push=1 ORDER BY date DESC LIMIT 1)))"
        elif period == "30":
            sql += " AND e.push_date BETWEEN ? AND ?"
            params.extend(["2026-10-02", "2026-10-31"])
        elif period.startswith("date:"):
            sql += " AND e.push_date=?"
            params.append(period[5:])
        if filters["round"] != "all":
            sql += " AND e.round_norm=?"
            params.append("待核" if filters["round"] == "轮次待确认" else filters["round"])
        if filters["status"] != "all":
            sql += " AND (CASE WHEN c.status='总部待核' THEN '已纳入' ELSE c.status END)=?"
            params.append("轮次待核" if filters["status"] == "轮次待确认" else filters["status"])
        if filters["tag"] != "all":
            tag = filters["tag"]
            clauses = {"AI相关": "c.ai_label='是'", "社交/社群相关": "c.social_label='是'",
                       "都不是": "c.ai_label='否' AND c.social_label='否'", "标签待确认": "(c.ai_label='待核' OR c.social_label='待核')"}
            sql += " AND ((c.manual_label IS NOT NULL AND instr(c.manual_label,?)>0) OR (c.manual_label IS NULL AND (" + clauses[tag] + ")))"
            params.append("标签待核" if tag == "标签待确认" else tag)
        expected = {r[0] for r in conn.execute(sql, params)}
        assert set(selected) == expected, filters
        assert 6 not in selected
        assert 104 not in selected
        assert not {101, 102, 103}.intersection(selected)
        if period != "all":
            assert 16 not in selected


@pytest.mark.parametrize("cid,message", [("capveon", "解读生成中"), ("nearby", "解读生成中"),
                                         ("talos", "深度解读生成失败"), ("hue", "暂时没有深度解读")])
def test_deep_states_render_same_company_route(seeded, cid, message):
    path, _ = snapshot(seeded)
    html = render_page(path, "detailHtml", cid)
    assert message in html
    assert "关键字段" in html and "简介" in html and "全部来源" in html
    conn, _ = seeded
    conn.execute("UPDATE deepdive SET status='完成' WHERE company_id=?", (cid,))
    if cid == "nearby":
        add_deep(conn, cid, "完成")
    path, _ = snapshot(seeded)
    html = render_page(path, "detailHtml", cid)
    assert "产品是什么" in html and "融资历史" in html


def test_eight_items_frozen_header_sources_and_entity_links(seeded):
    path, data = snapshot(seeded)
    company = company_detail(path, "mach9")
    assert company["header"] == {"name": "Mach9", "marks": ["新一轮"], "tags": ["AI相关"], "country": "美国"}
    assert [i["name"] for i in company["deepdive"]["items"]] == list(site.ITEM_NAMES)
    entities = company["deepdive"]["items"][0]["points"][0]["entities"]
    assert entities[0]["url"] == "https://docs.mach9.ai/product?doc=1"
    assert entities[1]["url"] == "https://firstround.com/"
    assert not entities[2]["url"] and not entities[3]["url"]
    html = render_page(path, "detailHtml", "mach9")
    assert 'target="_blank" rel="noopener nofollow"' in html
    assert html.index("资料有出入：国家") < html.index("简介") < html.index("关键字段") < html.index("产品是什么") < html.index("融资历史") < html.index("全部来源")
    assert "来源里没写" in html and "原文日期（美国东部时间）2026-10-29" in html
    assert "北京时间 2026-10-30 07:15:00" in html
    assert "官网未知" not in html and "utm_" not in html
    terse = next(e for e in data["events"] if e["id"] == 1)
    assert terse["sources"][0]["beijing_at"] == "2026-10-19 07:42:00"
    assert "Fall 2026（YC）" in render_page(path, "detailHtml", "terse")


def test_yc_skipped_only_summary_and_sources(seeded):
    path, _ = snapshot(seeded)
    html = render_page(path, "detailHtml", "aviern")
    assert "简介" in html and "全部来源" in html
    for hidden in ["关键字段", "融资历史", "产品是什么", "解读生成中", "深度解读"]:
        assert hidden not in html


def test_source_status_progress_usage_and_long_evidence_window(seeded):
    path, data = snapshot(seeded)
    status = data["status"]
    assert len(status["sources"]) == 8
    tc = next(s for s in status["sources"] if s["name"] == "TechCrunch")
    assert len(tc["addresses"]) == 2 and tc["count"] == 20
    assert "HTTP 200" == tc["technical"]
    ts = next(s for s in status["sources"] if s["name"] == "Term Sheet 网页版")
    assert ts["result"] == "Term Sheet 网页版已停止抓取：对方封禁，2026-10-30"
    assert ts["state"] == "已停止抓取"
    assert status["missed"] == [{"name": "YC Launches", "range": "10-29 08:40 至 10-30 08:40"}]
    assert status["deepdive_progress"] == {"date": "2026-10-31", "total": 5, "done": 1, "running": 1, "failed": 1, "timeout": 1, "skipped": 1}
    assert status["deepdive_budget"]["month_used"] == 2.75
    assert status["extract_budget"]["month_used"] == 1.25
    assert [e["company_id"] for e in status["new_evidence"]] == ["mach9", "terse"]
    html = render_page(path, "sourcesHtml")
    assert "可能有遗漏" in html and "本月已花费" in html
    assert "内容待复查" in html  # 内容待复查只在来源网站状态页显示。


def test_no_push_no_ratio_and_first_issue_is_latest(seeded):
    conn, _ = seeded
    conn.execute("DELETE FROM digest")
    path, data = snapshot(seeded)
    assert data["status"]["pending_ratios"] is None and data["week"]["date"] is None
    assert "还没有推送" in render_page(path, "weekHtml")
    conn.execute("INSERT INTO digest(date,push,event_ids_json,note) VALUES ('2026-10-31',1,'[16]','首期')")
    conn.execute("UPDATE funding_event SET push_date='2026-10-31' WHERE id=16")
    _, data = snapshot(seeded)
    assert data["week"]["cards"] == [16] and data["status"]["pending_ratios"]["denominator"] == 1


def test_atomic_replace_keeps_exactly_one_previous_version(seeded):
    path, _ = snapshot(seeded)
    original = (path / "data.json").read_bytes()
    conn, _ = seeded
    conn.execute("UPDATE company SET summary='第二版合成简介' WHERE id='terse'")
    path, _ = snapshot(seeded)
    previous = path.with_name("site.prev")
    assert (previous / "data.json").read_bytes() == original
    second = (path / "data.json").read_bytes()
    conn.execute("UPDATE company SET summary='第三版合成简介' WHERE id='terse'")
    snapshot(seeded)
    assert (previous / "data.json").read_bytes() == second
    assert not list(path.parent.glob(".site-*"))


@pytest.mark.parametrize("text", ["Quorvia", "tEsSaLiNe", "https://vc.example.invalid/", "REPLACE_RANDOM_PATH"])
@pytest.mark.parametrize("field", ["name", "summary", "website"])
def test_guard_failure_preserves_both_versions(seeded, text, field):
    path, _ = snapshot(seeded)
    snapshot(seeded)
    before = {p.relative_to(path.parent): p.read_bytes() for root in [path, path.with_name("site.prev")]
              for p in root.rglob("*") if p.is_file()}
    conn, _ = seeded
    if field == "website":
        conn.execute("UPDATE event_source SET url=? WHERE event_id=2", (text,))
    else:
        conn.execute(f"UPDATE company SET {field}=? WHERE id='mach9'", (text,))
    with pytest.raises(ValueError, match="禁用名|占位地址"):
        snapshot(seeded)
    assert all((path.parent / name).read_bytes() == value for name, value in before.items())
    assert not list(path.parent.glob(".site-*"))


def test_rename_failure_restores_site_and_previous(seeded, monkeypatch):
    path, _ = snapshot(seeded)
    snapshot(seeded)
    before = (path / "data.json").read_bytes()
    old_previous = (path.with_name("site.prev") / "data.json").read_bytes()
    original_replace = site.os.replace

    def fail_final(source, target):
        if Path(source).name.startswith(".site-") and Path(target) == path:
            raise OSError("合成写入失败")
        return original_replace(source, target)

    monkeypatch.setattr(site.os, "replace", fail_final)
    with pytest.raises(OSError):
        snapshot(seeded)
    assert (path / "data.json").read_bytes() == before
    assert (path.with_name("site.prev") / "data.json").read_bytes() == old_previous
    assert not list(path.parent.glob(".site-*"))


def test_test_mode_isolated_and_explicit_snapshot_dir(seeded):
    conn, cfg = seeded
    prod, _ = snapshot(seeded)
    cfg.test_mode = True
    (cfg.state_dir / "test").mkdir()
    (cfg.state_dir / "test/deep-usage.json").write_text(dump({"months": {"2026-10": {"estimated_cost": .12}}}))
    path, data = snapshot(seeded)
    assert path == cfg.state_dir / "test/site"
    assert data["meta"]["simulated"] is True
    assert data["status"]["deepdive_budget"]["month_used"] == .12
    assert data["status"]["extract_budget"]["month_used"] is None
    assert not prod.with_name("site.prev").exists()
    out = cfg.state_dir / "test-site/2026-10-31/0930"
    assert site.build_site(conn, cfg, out_dir=out, now=NOW) == out
    assert (out / "index.html").exists()


@pytest.mark.parametrize("now,expected", [("2026-10-29 08:00", "2026-10-30"), ("2026-10-30 09:00", "2026-11-06"),
                                          ("2026-09-30 08:00", "2026-09-30"), ("2026-09-30 09:00", "2026-10-09")])
def test_next_push_respects_holidays_and_time(tmp_path, monkeypatch, now, expected):
    cfg = Config(state_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    monkeypatch.setattr(site, "LINK_RECORDS", tmp_path / "missing.json")
    try:
        path = site.build_site(conn, cfg, now=datetime.fromisoformat(now).replace(tzinfo=BJ))
        assert json.loads((path / "data.json").read_text())["meta"]["next_push_date"] == expected
    finally:
        conn.close()


def test_cli_build_only_opens_read_only_db(seeded, monkeypatch):
    conn, cfg = seeded
    config_file = cfg.state_dir / "config.toml"
    config_file.write_text(f'state_dir = "{cfg.state_dir}"\n')
    conn.commit()
    before = cfg.db_path.read_bytes()
    calls = []
    original_build = site.build_site

    def verify_readonly(connection, config, **kwargs):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE company SET name='不应该写入'")
        calls.append(True)
        return original_build(connection, config, now=NOW)

    monkeypatch.setattr(site, "build_site", verify_readonly)
    assert cli.main(["--build-site", "--config", str(config_file)]) == 0
    assert calls == [True] and cfg.db_path.read_bytes() == before
    assert not cfg.outbox_dir.exists()


@pytest.mark.parametrize("other", ["--deep-only", "--resend", "--sync-sent", "--reset-test-db", "--first-issue"])
def test_cli_build_rejects_other_actions(other):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["--build-site", other])
    assert exc.value.code == 2


def test_cli_missing_database_fails_without_creating_it(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f'state_dir = "{tmp_path}"\n')
    assert cli.main(["--build-site", "--config", str(config)]) == 1
    assert not (tmp_path / "vc-radar.db").exists()


def test_render_escapes_untrusted_content(seeded):
    conn, _ = seeded
    conn.execute("UPDATE company SET summary='<img src=x onerror=alert(1)>' WHERE id='mach9'")
    conn.execute("UPDATE event_source SET url='javascript:alert(1)' WHERE event_id=2")
    path, _ = snapshot(seeded)
    html = render_page(path, "detailHtml", "mach9")
    assert "&lt;img" in html and "<img" not in html
    assert 'href="javascript:' not in html


def test_sort_all_tie_breakers_and_amount_buckets(seeded):
    conn, _ = seeded
    cases = [
        (20, "sorta", "Zeta", "$20M", "披露", [], "2026-10-31 08:42:00"),
        (21, "sortb", "beta", "$20M", "披露", [], "2026-10-31 08:42:00"),
        (22, "sortc", "Gamma", "$20M", "披露", [], "2026-10-31 08:41:00"),
        (23, "sortd", "Delta", "$20M", "披露", ["大额"], "2026-10-31 08:42:00"),
        (24, "sorte", "Epsilon", "$20M", "披露", ["新一轮"], "2026-10-31 08:42:00"),
        (25, "sortf", "Alpha", "$30M", "披露", [], "2026-10-31 08:42:00"),
        (26, "sortg", "Eta", "TechCrunch 写 $100M，Crunchbase News 写 $120M", "金额待核", [], "2026-10-31 08:42:00"),
        (27, "sorth", "Theta", None, "未披露", [], "2026-10-31 08:42:00"),
    ]
    for eid, cid, name, amount, state, marks, seen in cases:
        add_company(conn, cid, name)
        add_event(conn, eid, cid, amount=amount, amount_status=state, marks=marks, seen=seen)
    conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=26", (dump({"amount_text": "$100M"}),))
    conn.execute("UPDATE digest SET event_ids_json=? WHERE date='2026-10-30'", (dump([row[0] for row in cases]),))
    _, data = snapshot(seeded)
    assert data["week"]["cards"] == [25, 24, 23, 22, 21, 20, 26, 27]


@pytest.mark.parametrize("verified,verified_at,linked", [(False, "2026-10-30 10:00", False),
                                                       (True, "2026-11-01 10:00", False),
                                                       (True, "2026-10-31 10:00", True)])
def test_verified_links_never_use_false_or_future_records(seeded, verified, verified_at, linked):
    site.LINK_RECORDS.write_text(dump([{"name": "First Round Capital", "company": "Mach9", "url": "https://firstround.com/team/",
                                       "verified": verified, "verified_at": verified_at}]))
    path, _ = snapshot(seeded)
    company = company_detail(path, "mach9")
    assert bool(company["deepdive"]["items"][0]["points"][0]["entities"][1]["url"]) == linked


def test_large_yc_batch_has_no_spurious_generating_message(seeded):
    conn, _ = seeded
    for i in range(31):
        cid = f"yc-seed-{i}"
        add_company(conn, cid, f"Terse Seed {i}")
        add_event(conn, 100 + i, cid, pushed=None, source="yc_directory", seen="2026-10-31 08:40:00")
        conn.execute("UPDATE raw_item SET structured_json=? WHERE id=?", (dump({"yc_batch": "Fall 2026"}), 100 + i))
    path, data = snapshot(seeded)
    folded = [company_detail(path, c["id"]) for c in data["companies"] if c["id"].startswith("yc-seed-")]
    assert len(folded) == 31 and all(c["deepdive"]["status"] == "不做" for c in folded)
    assert "解读生成中" not in render_page(path, "detailHtml", "yc-seed-0")


@pytest.mark.parametrize("error,message", list(site.ERROR_TEXT.items()))
def test_source_error_copy_and_latest_address_result(seeded, error, message):
    conn, _ = seeded
    conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,http_code,result,error_type) "
                 "VALUES ('later','tc_venture','2026-10-31T00:40:00Z','2026-10-31T00:41:00Z',403,'失败',?)", (error,))
    _, data = snapshot(seeded)
    source = next(s for s in data["status"]["sources"] if s["name"] == "TechCrunch")
    assert source["result"] == message and "HTTP" not in source["result"]
    assert source["last_at"] == "2026-10-31 08:41:00"
    assert "HTTP 403" in source["technical"]


def test_guard_checks_deep_quotes_and_static_assets(seeded, monkeypatch, tmp_path):
    conn, _ = seeded
    path, _ = snapshot(seeded)
    before = (path / "data.json").read_bytes()
    conn.execute("UPDATE deepdive SET result_json=? WHERE company_id='mach9'", (dump({"items": {"item1": {"points": [
        {"zh": "手写要点", "evidence": [{"page": 1, "quote": "Quorvia"}]}]}}}),))
    with pytest.raises(ValueError):
        snapshot(seeded)
    assert (path / "data.json").read_bytes() == before
    conn.rollback()
    assets = tmp_path / "assets"
    shutil.copytree(site.ASSETS, assets)
    (assets / "style.css").write_text("/* REPLACE_RANDOM_PATH */")
    monkeypatch.setattr(site, "ASSETS", assets)
    with pytest.raises(ValueError):
        snapshot(seeded)
    assert (path / "data.json").read_bytes() == before


@pytest.mark.parametrize("state", ["没跑完", "全部失败", "部分失败"])
def test_failed_week_does_not_claim_no_new_companies(seeded, state):
    conn, _ = seeded
    conn.execute("UPDATE digest SET status=?,event_ids_json='[]' WHERE date='2026-10-30'", (state,))
    path, _ = snapshot(seeded)
    html = render_page(path, "weekHtml")
    assert "本周没有发现新的天使轮、种子轮、A 轮融资。" not in html and "来源网站状态" in html
    expected = {"没跑完": "今天没抓完，本周汇总按已有的数据展示。",
                "全部失败": "今天所有来源都没抓到，本周汇总按已有的数据展示。",
                "部分失败": "部分来源网站没抓到，本周汇总可能不全。"}[state]
    assert expected in html and "金额未披露" not in html


def test_stored_week_hints_are_plain_copy_with_relative_source_link(seeded):
    conn, _ = seeded
    text = "10-30 周五 · 美国早期融资 · 本周\n上次推送后共 9 家\n提前推送 · [本周 1 家有新证据](https://radar.example.com/private/#sources)\n1. Mach9"
    conn.execute("UPDATE digest SET text=? WHERE date='2026-10-30'", (text,))
    path, _ = snapshot(seeded)
    html = render_page(path, "weekHtml")
    assert "上次推送后共 9 家" in html and "提前推送" in html
    assert 'href="#sources"' in html and "private" not in html


def test_build_accepts_default_sqlite_connection_without_mutating_it(seeded):
    conn, cfg = seeded
    conn.commit()
    plain = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert plain.row_factory is None
        path = site.build_site(plain, cfg, now=NOW)
        assert json.loads((path / "data.json").read_text())["week"]["cards"][0] == 3
        assert plain.row_factory is None and not plain.in_transaction
    finally:
        plain.close()

