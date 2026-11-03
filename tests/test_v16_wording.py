"""V16e 合成数据验证推送、页面用语和首期遗留值；全程离线。"""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from vcradar import config, db, deep, outbox, pipeline, render, scan, site, wording
from vcradar.config import BJ, Config, load_config
from vcradar.models import Card, CardSource, DigestData, ErrorType, PendingLine, PushSection, SourceStatusLine
from vcradar.sources.registry import P0_SOURCES


NOW = datetime(2026, 10, 31, 8, 45, tzinfo=BJ)
BASE = "https://radar.test/private"
RULES = json.loads((Path(__file__).parent / "fixtures/wording/banned-20261031.json").read_text())


def clean(text):
    """只扫描产品文案，原文内容由调用方排除。"""
    for rule in RULES:
        assert not (re.search(rule["旧词"], text) if rule.get("正则") else rule["旧词"] in text), rule
    assert not re.search(r"美国早期|非美国|美国公司|美国天使轮", text)


def company(name="Terse", *, amount=27_000_000, marks=(), **kwargs):
    return Card(name.lower().replace(" ", "-"), name, list(marks), "种子轮", "$27M", "领投未披露",
                "AI相关 + 标签待核", "AI", "公司提供软件。团队开发产品。",
                [CardSource("TechCrunch", NOW.date(), f"https://news.test/{name.replace(' ', '-')}")],
                amount_usd=amount, **kwargs)


@pytest.fixture
def links(tmp_path):
    path = tmp_path / "links.json"
    path.write_text(json.dumps([
        {"type":"产品", "company":"Terse", "verified":True, "url":"https://product.test/"},
        {"type":"公司", "company":"Terse", "verified":False, "url":"https://unverified.test/"},
        {"type":"公司", "company":"Terse", "verified":True, "url":""},
        {"type":"公司", "company":" terse ", "verified":True, "url":"https://terse.test/"},
        {"type":"公司", "company":"Terse", "verified":True, "url":"https://later.test/"},
        {"type":"公司", "company":"Flapping Airplanes", "verified":True, "url":"https://flapping.test/"},
    ]))
    return path


def digest(**kwargs):
    kwargs.setdefault("review_site_passed", True)
    return DigestData(NOW.date(), None, NOW, "有新增", **kwargs)


def test_banned_fixture_is_covered():
    assert {r["旧词"] for r in RULES} <= wording.BANNED_OLD_WORDS
    assert len(RULES) >= 37


def test_exact_push_format_and_verified_official_names(links):
    cards = [company(), company("Terse AI"), company("Numeracle"), company("Slingshot Insurance"),
             company("Flapping Airplanes", marks=wording.MARK_ORDER)]
    data = digest(cards=cards, base_url=BASE, round_pending=[PendingLine("p", "待确认公司", "", "")],
                  filtered={"轮次不在范围":2})
    text = render.render_push(data, links_file=links)
    lines = text.splitlines()
    assert lines[0] == "10-31 周六 · 早期融资 · 本周"
    assert lines[1].startswith("1. ") and "本周共" not in text and "已过滤" not in text
    assert lines[5] == ("5. Flapping Airplanes【新一轮】【大额】【补录】【金额说法不一】｜公司提供软件。｜种子轮 · 2700 万美元｜"
                        "[官网](https://flapping.test/) [来源](https://news.test/Flapping-Airplanes)")
    assert "[官网](https://terse.test/)" in text
    for line in lines:
        if any(f". {name}｜" in line for name in ["Terse AI", "Numeracle", "Slingshot Insurance"]):
            assert "[官网]" not in line and "[来源]" in line
    assert "later.test" not in text
    assert lines[-1] == f"[看本周全部]({BASE}/#week)"
    clean(text)


@pytest.mark.parametrize("content", [None, "{", "null", '"bad"', '[null, 1, {}]', '{"links": "bad"}'])
def test_bad_or_missing_links_are_empty(tmp_path, content):
    path = tmp_path / "links.json"
    if content is not None:
        path.write_text(content)
    assert render.official_links(path) == {}
    assert "[官网]" not in render.render_push(digest(cards=[company()]), links_file=path)
    loaded = [{k:v for k,v in r.items() if k != "_record_file"} for r in deep.load_link_records(path)]
    assert loaded == render.read_link_records(path)


@pytest.mark.parametrize("has_cards", [False, True])
@pytest.mark.parametrize("base", ["", BASE])
def test_sections_have_separate_slots_counts_order_and_overflow(links, has_cards, base):
    sections = [PushSection("backfill", "之前漏掉的公司（一次性补充）", "另补充此前漏掉的 12 家",
                            [company(f"Extra {i:02}", amount=i) for i in range(12)]),
                PushSection("other", "另一组公司", "另有一组公司", [company("Another")]),
                PushSection("empty", "空段", "空段提示")]
    data = digest(cards=[company(f"Weekly {i:02}", amount=i) for i in range(11)] if has_cards else [],
                  extra_sections=sections, base_url=base, yc_new_batch=("Fall 2026", 31) if has_cards else None,
                  sources=[SourceStatusLine("TechCrunch", True, False, "31 条")],
                  last_push_summary=(date(2026,10,30), 12))
    text = render.render_push(data, links_file=links)
    lines = text.splitlines()
    clean(text)
    assert "空段" not in text and "空段提示" not in text
    assert "另补充此前漏掉的" not in text and "另有一组公司" not in text
    start = lines.index(sections[0].title)
    assert lines[start+1].startswith("1. Extra 11｜") and lines[start+10].startswith("10. Extra 02｜")
    overflow = f"还有一些没有列出，[在往期页看]({BASE}/#backfill)" if base else "还有一些没有列出"
    assert lines[start+11] == overflow
    assert lines[start+13].startswith("1. Another｜")
    if has_cards:
        assert lines[1].startswith("1. Weekly 10｜")
        assert lines[start-2].startswith("10. Weekly 01｜")
        assert lines[start-1] == "YC 新一批 31 家" + (f"，[名单见网页]({BASE}/#yc)" if base else "")
    else:
        assert lines[1] == wording.NO_NEW
        assert "已接入" not in text and "上一次有新公司" not in text
        assert start == 2
    if not base:
        assert "/#" not in text and "见网页" not in text and "在往期页看" not in text
    assert render.remove_page_links(render.render_push(replace(data, base_url=BASE), links_file=links)) == render.render_push(replace(data, base_url=""), links_file=links)


@pytest.mark.parametrize("error", list(ErrorType))
def test_error_and_all_hint_wording(links, error):
    data = digest(cards=[company(marks=wording.MARK_ORDER)], baseline_count=46,
                  new_evidence_count=2, ts_stopped_notice="封禁", resend=True,
                  sources=[SourceStatusLine("TechCrunch", False, False, "", error,
                                            date(2026,10,30), "10-28 至 10-30")])
    text = render.render_push(data, links_file=links, failed_days=2, early_push=True,
                              incomplete_until=date(2026,10,27))
    clean(text)
    hints = text.splitlines()[1]
    assert all(old not in text for old in ("不算新增", "没抓全", "提前推送", "重发", "本周有 2 天", "资料有出入"))
    stopped = "Term Sheet 没抓到：对方不再允许抓取" + wording.FETCH_WARNING_END
    if error == ErrorType.TS_STOPPED:
        assert hints == stopped and "已停止抓取" not in text
    else:
        assert hints == ("TechCrunch 没抓到：" + wording.PUSH_FAILURE_REASONS[error].format(date="10-30")
                         + wording.FETCH_WARNING_END + stopped)
    assert text.splitlines()[2].startswith("1. ")
    assert data.baseline_count == 46 and data.new_evidence_count == 2


@pytest.mark.parametrize("state", ["没跑完", "全部失败", "没有新增"])
def test_empty_and_run_failures_use_exact_copy(links, state):
    data = replace(digest(), status=state, sources=[SourceStatusLine("Term Sheet", False, True, "")])
    text = render.render_push(data, links_file=links)
    clean(text)
    assert wording.NO_NEW in text
    assert "已接入" not in text and "已停止抓取" not in text
    if state != "没有新增":
        assert "没抓到：" in text.splitlines()[1] and text.count(wording.FETCH_WARNING_END) == 1
    else:
        assert len(text.splitlines()) == 2


@pytest.mark.parametrize("amount", ["约 1200 万美元（原 €1100 万，按 10-30 汇率）", "1000 万（币种待确认）", "€1100 万（暂未换算成美元）"])
def test_push_keeps_original_currency_and_only_omits_fx_date(links, amount):
    c = replace(company(), amount_label=amount)
    text = render.render_push(digest(cards=[c]), links_file=links)
    assert amount.replace("，按 10-30 汇率", "") in text
    assert "按 10-30 汇率" not in text
    assert amount in render.render_card(c, BASE)
    clean(text)
    clean(render.render_card(c, BASE))


def test_legacy_adapter_and_resend_preserve_source_links():
    old = ("10-31 周六 · 美国早期融资 · 本周\n本周共 2 家。另有 3 家总部待核 · 1 家轮次待核 · 已过滤 2 家。\n"
           "已收录 46 家作为基线 · 本周一至 10-27 日部分未能补全 · 提前推送 · 有漏抓：X，10-27 至 10-30 · "
           "[本周 2 家有新证据](https://radar.test/#sources)\n1. Terse【补抓】｜软件。｜种子轮 · $27M｜[来源](https://news.test/x)\n"
           "YC 新批次 31 家，见回看页\n[看本周全部 2 家](https://radar.test/#week)｜[信源状态](https://radar.test/#sources)")
    text = wording.legacy_to_display(old)
    clean(text)
    assert "3 家" not in text
    assert "可能有遗漏：X，10-27 至 10-30的内容没抓全" in text
    resend = render.add_resend_notice(render.remove_page_links(text))
    assert "X 没抓到：此前遗漏的内容还没有补齐" in resend and "重发" not in resend and "提前推送" not in resend
    assert "[来源](https://news.test/x)" in resend and "/#" not in resend
    assert "见网页" not in resend and "在往期页看" not in resend
    assert render.add_resend_notice(resend) == resend
    clean(resend)


@pytest.mark.parametrize("reason,ext,round_text,expected", [
    ("总部非美国", {}, "", ""), ("批次超过 12 个月", {}, "", "YC 批次超过一年"),
    ("轮次不在范围", {"is_debt_only":True}, "", "只有债务融资"),
    ("轮次不在范围", {"is_fund_raise":True}, "", "基金募资"),
    ("轮次不在范围", {}, "Series B", "B 轮及以后"),
    ("轮次不在范围", {}, "", "B 轮及以后"),
])
def test_filter_reason_mapping(reason, ext, round_text, expected):
    assert wording.filter_reason(reason, extractions=[ext], round_text=round_text) == expected


def test_links_file_config_can_be_overridden(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'links_file = "{tmp_path / "custom.json"}"')
    assert load_config(config_file).links_file == tmp_path / "custom.json"


def node(path, script):
    if not shutil.which("node"):
        pytest.skip("离线前端验证需要 node")
    return json.loads(subprocess.run(["node", "-e", script, str(path)], check=True, capture_output=True, text=True).stdout)


@pytest.fixture
def page(tmp_path, links):
    from test_site_build import add_company, add_event, add_deep
    cfg = Config(state_dir=tmp_path, links_file=links)
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, P0_SOURCES)
    for i, status in enumerate(["已纳入", "总部待核", "轮次待核", "已纳入", "已纳入", "已纳入"], 1):
        add_company(conn, f"word-{i}", f"Word {i}", status=status, social="待核")
        add_event(conn, i, f"word-{i}", round_norm="待核" if i == 3 else "种子轮",
                  pushed=None if i == 6 else "2026-10-30", marks=["补抓", "金额待核"] if i == 1 else (),
                  filtered=["总部非美国"] if i == 4 else ["总部非美国", "轮次不在范围"] if i == 5 else ())
    add_deep(conn, "word-1", "完成", found="2026-10-31 08:30:00")
    conn.execute("UPDATE funding_event SET baseline_unpushed=1 WHERE id=6")
    conn.execute("UPDATE raw_item SET is_baseline=1 WHERE id=6")
    conn.execute("INSERT INTO digest(date,push,status,event_ids_json,text,generated_at) VALUES(?,1,'基线',?,?,?)",
                 ("2026-10-30", json.dumps([1,2,3,4,5]), "10-30 周五 · 美国早期融资 · 本周\n本周共 1 家。另有 1 家总部待核 · 已过滤 2 家。\n已收录 46 家作为基线 · 有漏抓：TechCrunch，10-28 至 10-30", "2026-10-30 08:45:00"))
    conn.execute("UPDATE source SET enabled=0,crawl_status='已停抓',stop_reason='封禁',stopped_at='2026-10-30 08:41:00' WHERE id='ts_web'")
    conn.execute("INSERT INTO fetch_run(source_id,run_id,started_at,ended_at,result) VALUES('tc_venture','wording',?,?, '没轮到')", (NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    yield conn, cfg
    conn.close()


def copy_strings(value, key=""):
    # 公司/来源正文原样保留，不能按用语规则改写原句。
    if key in {"name", "summary", "one_liner", "quote", "url", "website", "title", "country_basis", "hq_basis", "text"}:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, item in value.items():
            yield from copy_strings(item, k)
    elif isinstance(value, list):
        for item in value:
            yield from copy_strings(item, key)


def test_site_outputs_use_display_words_and_legacy_visibility(page):
    conn, cfg = page
    path = site.build_site(conn, cfg, now=NOW)
    data = json.loads((path / "data.json").read_text())
    assert data["week"]["cards"] == [2,1] and data["week"]["round_pending"] == [3]
    assert data["week"]["filtered"] == [5]
    assert site._snapshot(conn, cfg, NOW)["week"]["filtered_counts"] == {"B 轮及以后":1}
    assert "filtered_counts" not in data["week"] and "status" not in data
    assert 4 not in data["listing"] and 6 not in data["listing"]
    e = next(e for e in data["events"] if e["id"] == 1)
    assert e["marks"] == ["补录", "金额说法不一"] and e["tags"] == ["AI相关", "标签待确认"]
    old_hq = next(e for e in data["events"] if e["id"] == 2)
    assert old_hq["status"] == "已纳入" and old_hq["country"] == "国家待确认"
    for filename in [path / "app.js", path / "index.html"]:
        text = filename.read_text()
        clean(text)
        assert "美国" not in text.replace("美国东部时间", "")
    assert "早期创投监控" in (path / "index.html").read_text()
    for filename in path.rglob("*.json"):
        clean("\n".join(copy_strings(json.loads(filename.read_text()))))
    script = """
      const fs=require('fs'),p=require('path'),root=process.argv[1],ui=require(p.join(root,'app.js'));
      const d=JSON.parse(fs.readFileSync(p.join(root,'data.json')));
      const detail=id=>JSON.parse(fs.readFileSync(p.join(root,'c',id+'.json')));
      console.log(JSON.stringify({week:ui.weekHtml(d),yc:ui.ycHtml(d),
        detail:ui.detailHtml(detail('word-1'),'word-1'),baseline:ui.detailHtml(detail('word-6'),'word-6'),
        selections:['all','week','30','date:2026-10-30'].map(period=>ui.selectEvents(d,{period,tag:'all',round:'all',status:'all'}).map(e=>e.id))}));
    """
    rendered = node(path, script)
    clean("\n".join(v for v in rendered.values() if isinstance(v, str)))
    assert all(6 not in ids for ids in rendered["selections"])
    assert "国家和依据" in rendered["detail"] and "旧金山" in rendered["detail"]
    assert "城市待确认" not in rendered["detail"]
    assert "原文日期（美国东部时间）" in rendered["detail"]
    assert "不算新增" not in rendered["week"] and "来源网站状态" not in rendered["week"]
    internal = site._snapshot(conn, cfg, NOW)
    assert internal["week"]["baseline_notice"] == "首次运行：收录了 46 家以前的公司，不算新增"
    assert any(s["state"] == "已停止抓取" for s in internal["status"]["sources"])
    assert "这次没来得及抓" not in rendered["week"]
    assert "首次运行时已有，未推送" not in rendered["baseline"] and "下次推送" not in rendered["baseline"]


def test_company_country_in_description_is_preserved(links):
    c = replace(company(), summary="总部在美国旧金山。公司开发软件。")
    text = render.render_push(digest(cards=[c]), links_file=links)
    assert "总部在美国旧金山。" in text
    clean(text)


@pytest.mark.parametrize("route", ["#yc", "#backfill"])
def test_new_routes_and_supplement_badges(page, route):
    conn, cfg = page
    conn.execute("UPDATE company SET backfill='首期漏掉' WHERE id='word-1'")
    conn.execute("UPDATE digest SET supplement_event_ids_json='[1]'")
    if route == "#backfill":
        conn.execute("UPDATE company SET backfill='补充未推送',backfill_rank=11 WHERE id='word-1'")
        conn.execute("UPDATE funding_event SET original_first_ingested_at=first_ingested_at,push_date=NULL WHERE id=1")
    path = site.build_site(conn, cfg, now=NOW)
    data_path = path / "data.json"
    data = json.loads(data_path.read_text())
    assert 1 not in data["week"]["cards"] and 1 not in data["week"]["event_ids"]
    assert site._snapshot(conn, cfg, NOW)["status"]["pending_ratios"]["denominator"] == 2
    # 合成一个已折叠 YC 名单，不借用真实抓取数据。
    data["week"]["yc"] = [2]
    data_path.write_text(json.dumps(data))
    script = r"""
      const fs=require('fs'),p=require('path'),vm=require('vm'),root=process.argv[1];
      const nodes={header:{innerHTML:'早期创投监控'},main:{innerHTML:''}},listeners={};
      const context={URL,location:{hash:ROUTE},
        document:{querySelectorAll:()=>[],getElementById:id=>nodes[id],addEventListener:(name,fn)=>listeners[name]=fn},
        window:{addEventListener:()=>{},scrollTo:()=>{}},
        fetch:async url=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(p.join(root,url)))})};
      vm.runInNewContext(fs.readFileSync(p.join(root,'app.js'),'utf8'),context);
      (async()=>{await new Promise(resolve=>setImmediate(resolve));
        const initial=nodes.main.innerHTML;
        listeners.click({target:{closest:selector=>selector==='[data-clear-backfill]'?{}:null}});
        await new Promise(resolve=>setImmediate(resolve));
        console.log(JSON.stringify({initial,after:nodes.main.innerHTML,title:context.document.title,header:nodes.header.innerHTML}));
      })();
    """.replace("ROUTE", json.dumps(route))
    rendered = node(path, script)
    clean(rendered["initial"])
    assert rendered["title"].endswith(" · 早期创投监控")
    if route == "#yc":
        assert "Word 2" in rendered["initial"] and "Word 1" not in rendered["initial"]
        assert rendered["title"].startswith("YC 新一批名单")
    else:
        assert "Word 1" in rendered["initial"] and "Word 2" not in rendered["initial"]
        assert "补充未推送 1 家" not in rendered["initial"] and "查看全部" in rendered["initial"]
        assert "Word 2" in rendered["after"]  # 可继续改筛选，不会被路由强制覆盖。
    detail = node(path, """
      const fs=require('fs'),p=require('path'),root=process.argv[1],ui=require(p.join(root,'app.js'));
      const d=JSON.parse(fs.readFileSync(p.join(root,'c/word-1.json')));
      console.log(JSON.stringify(ui.detailHtml(d,'word-1')));
    """)
    assert rendered["header"] == "早期创投监控"
    assert ('class="mark is-supp">此前漏掉' in detail) is (route != "#backfill")


def test_large_index_retains_details_but_hides_all_pure_baselines(tmp_path, links):
    from test_site_slim import size_database
    cfg = Config(state_dir=tmp_path, links_file=links)
    conn = db.connect(cfg.db_path)
    try:
        size_database(conn)
        path = site.build_site(conn, cfg, now=NOW)
        data = json.loads((path / "data.json").read_text())
        assert (path / "data.json").stat().st_size < 500_000
        assert len(data["listing"]) == 50 and len(data["events"]) == 50
        assert len(data["companies"]) == 550 and len(list((path / "c").glob("*.json"))) == 550
        assert not any(e.get("baseline_unpushed") for e in data["events"])
    finally:
        conn.close()


@pytest.mark.parametrize("no_links", [False, True])
def test_pipeline_uses_configured_official_links_and_keeps_sources(tmp_path, monkeypatch, links, no_links):
    from test_pipe_pipeline import run_at, write_fixture
    def forbidden(*args, **kwargs):
        raise AssertionError("本测试禁止读取密钥")
    monkeypatch.setattr(config, "load_api_key", forbidden)
    records = json.loads(links.read_text())
    records.append({"type":"公司", "company":"Anchor", "verified":True, "url":"https://anchor.test/"})
    links.write_text(json.dumps(records))
    cfg = Config(state_dir=tmp_path / "state", test_mode=True, links_file=links, base_url=BASE, review_site_passed=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30")
    handoff, _ = run_at(cfg, fixture, "2026-10-30", first_issue=True, no_deep=True, no_links=no_links)
    text = handoff["text"]
    clean(text)
    assert "[官网](https://anchor.test/) [来源](https://synthetic.example/anchor)" in text
    assert ("/#week" in text) is not no_links


def test_pipeline_resends_old_digest_with_new_copy_without_mutating_original(tmp_path, monkeypatch):
    from test_pipe_pipeline import arguments
    def forbidden(*args, **kwargs):
        raise AssertionError("本测试禁止读取密钥")
    monkeypatch.setattr(config, "load_api_key", forbidden)
    cfg = Config(state_dir=tmp_path, test_mode=True)
    original = ("10-31 周六 · 美国早期融资 · 本周\n本周没有新增的美国天使轮、种子轮、A 轮公司。\n"
                "已接入信源 8 个，全部正常\n上次有新增：10-30那期，12 家")
    conn = db.connect(cfg.db_path)
    scan.upsert_digest(conn, {"date":NOW.date(), "push":True, "status":"没有新增", "text":original, "generated_at":NOW})
    conn.commit()
    conn.close()
    main = outbox.write_main(cfg.outbox_dir, NOW.date(), push=True, alarm=False, generated_at=NOW,
                             text=original, test=True, real_now=NOW)
    before = main.read_bytes()
    assert pipeline.run(cfg, arguments(resend=True, no_deep=True), pipeline.Clock(NOW.date(), NOW.time())) == 0
    resend, = cfg.outbox_dir.glob("*-resend-*.json")
    text = json.loads(resend.read_text())["text"]
    clean(text)
    assert len(text.splitlines()) == 2 and "重发" not in text
    assert "上一次有新公司" not in text
    assert main.read_bytes() == before
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT text FROM digest").fetchone()[0] == original
    conn.close()


@pytest.mark.parametrize("ok,message,expected", [
    (True, "汇率日期 10-30", "汇率日期：10-30"),
    (False, "汇率没更新，用的是 10-29 的汇率", "汇率没更新，用的是 10-29 的汇率"),
])
def test_fx_status_uses_normal_date_or_degraded_message(page, ok, message, expected):
    conn, cfg = page
    (cfg.state_dir / "status.json").write_text(json.dumps({"fx":{
        "ok":ok, "date":"2026-10-30", "message":message, "source":"https://ecb.test/"}}))
    path = site.build_site(conn, cfg, now=NOW)
    html = node(path, """
      const fs=require('fs'),p=require('path'),root=process.argv[1],ui=require(p.join(root,'app.js'));
      console.log(JSON.stringify(ui.weekHtml(JSON.parse(fs.readFileSync(p.join(root,'data.json'))))));
    """)
    status = site._snapshot(conn, cfg, NOW)["status"]["fx"]
    assert ("汇率日期：" + status["date"][5:] if ok else status["message"]) == expected
    assert expected not in html


def test_empty_evidence_list_uses_full_company_phrase(page):
    conn, cfg = page
    conn.execute("DELETE FROM deepdive")
    path = site.build_site(conn, cfg, now=NOW)
    html = node(path, """
      const fs=require('fs'),p=require('path'),root=process.argv[1],ui=require(p.join(root,'app.js'));
      console.log(JSON.stringify(ui.weekHtml(JSON.parse(fs.readFileSync(p.join(root,'data.json'))))));
    """)
    assert "暂时没有资料有出入的公司" not in html
    assert site._snapshot(conn, cfg, NOW)["status"]["new_evidence"] == []


def test_legacy_hq_pending_header_uses_frozen_country_without_changing_event(page):
    from test_site_build import add_deep
    conn, cfg = page
    add_deep(conn, "word-2", "完成")
    conn.execute("UPDATE deepdive SET push_judgement_json=? WHERE company_id='word-2'",
                 (json.dumps({"country":"美国"}),))
    path = site.build_site(conn, cfg, now=NOW)
    detail = json.loads((path / "c/word-2.json").read_text())
    assert detail["companies"][0]["header"]["country"] == "美国"
    assert detail["events"][0]["country"] == "国家待确认"
    assert detail["events"][0]["status"] == "已纳入"


def test_custom_section_overflow_and_no_links(links):
    section = PushSection("backfill", "之前漏掉的公司（一次性补充）", "另补充此前漏掉的 11 家",
                          [company(f"Supplement {i}") for i in range(11)],
                          f"另有 1 家，[在往期页看]({BASE}/#backfill)")
    data = digest(extra_sections=[section])
    text = render.render_push(data, links_file=links)
    assert text.endswith("还有一些没有列出") and "/#" not in text
    clean(text)
