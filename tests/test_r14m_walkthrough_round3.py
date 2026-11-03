"""R14m 第三轮走查：合成数据验证来源、日期、字段、导航及本轮融资显示。"""
from copy import deepcopy
import json
import re

import pytest

from vcradar import site, wording
from test_r14j_walkthrough import Text
from test_site_build import node, seeded, snapshot, render_page  # noqa: F401


W = wording.SITE_TEXT
TODAY = "2026-11-03"


def source(url="https://fortune.com/synthetic-funding/", name="Term Sheet", title=" adsilico ", **values):
    return {"url": url, "name": name, "title": title, "published_at": "2026-11-02 19:51:59",
            "original_date_et": "2026-11-02", "beijing_at": "2026-11-03 08:49:55", **values}


@pytest.fixture
def detail_data():
    src = source()
    evidence = {**src, "quote": "Synthetic company raised its current round."}
    event = {"id": 1, "company_id": "adsilico", "name": "adsilico", "round": "种子轮", "round_text": "Seed",
             "amount": "1000 万美元", "leads": ["Synthetic Capital"], "follows": ["Synthetic Ventures"],
             "country": "英国", "city": "伦敦", "country_basis": "来源写公司在伦敦", "date": "2026-11-02",
             "first_seen": "2026-10-31", "push_date": None, "sources": [src], "summary": "合成的产品介绍。合成的其余简介。",
             "status": W["admitted"], "pending_reason": "", "marks": [], "tags": [W["ai"]], "filter_reasons": []}
    items = [{"name": name, "points": [], "missing": []} for name in site.ITEM_NAMES]
    items[0]["points"] = [{"text": "合成的本轮事实", "evidence": [evidence], "entities": []}]
    company = {"id": "adsilico", "event_id": 1, "header": {"name": "adsilico", "country": "英国", "marks": [], "tags": [W["ai"]]},
               "summary": event["summary"], "website": "https://adsilico.example/", "verification": [],
               "deepdive": {"status": "完成", "items": items, "entities": [],
                            "new_evidence": [{"field": "金额", "evidence": [deepcopy(evidence)]}]},
               "sources": [src], "history": [1], "conflicts": []}
    return {"wording": W, "meta": {"today": TODAY, "recent_cutoff": "2026-10-04"}, "events": [event],
            "companies": [company], "listing": [1], "digests": [{"date": "2026-10-30"}],
            "week": {"date": "2026-10-30", "event_ids": [1], "cards": [1], "round_pending": [], "filtered": [], "yc": []}}


def ui(data, expression):
    return node("const ui=require(process.argv[1]),d=JSON.parse(process.argv[2]);"
                f"console.log(JSON.stringify({expression}));", json.dumps(data, ensure_ascii=False))


def detail(data):
    return ui(data, "ui.detailHtml(d,'adsilico')")


def source_rows(html, class_name):
    rows = [(Text(label).text, Text(date).text) for label, date in re.findall(
        rf'<div class="{class_name}">(.*?)<div class="muted">(.*?)</div></div>', html, re.S)]
    if class_name == "source":
        evidence = [(Text(label).text, Text(date).text) for label, date in re.findall(
            r'<span class="ev-src">(.*?)</span><q>.*?</q><span class="muted">(.*?)</span>', html, re.S)]
        return evidence + rows
    return rows


def facts(html):
    grid = re.search(r'<dl class="facts">(.*?)</dl>', html, re.S).group(1)
    return dict(re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", grid, re.S))


def set_sources(data, sources):
    company = data["companies"][0]
    company["sources"] = sources
    company["deepdive"]["items"][0]["points"][0]["evidence"] = [
        {**src, "quote": "Handwritten synthetic evidence."} for src in sources]
    company["deepdive"]["new_evidence"][0]["evidence"] = deepcopy(company["deepdive"]["items"][0]["points"][0]["evidence"])
    data["events"][0]["sources"] = sources


def test_1_company_titles_hidden_and_cards_keep_source_name_date(detail_data):
    detail_data["events"][0]["name"] = "Adsilico Current"
    sources = [source(), source("https://techcrunch.com/synthetic/", "TechCrunch", " ADSILICO CURRENT "),
               source("https://news.crunchbase.com/synthetic/", "Crunchbase News", "一篇合成融资标题")]
    set_sources(detail_data, sources)
    html = detail(detail_data)
    expected = ["Term Sheet", "TechCrunch", "Crunchbase News · 一篇合成融资标题"]
    assert [label for label, _ in source_rows(html, "source-row")] == expected
    assert [label for label, _ in source_rows(html, "source")] == expected + ["来源：" + label for label in expected]
    cards = ui(detail_data, "ui.weekHtml(d)")
    names = [Text(label).text for label in re.findall(r'<a class="co-src"[^>]*>(.*?)</a>', cards)]
    assert names == ["Term Sheet · 11-02", "TechCrunch · 11-02", "Crunchbase News · 11-02"]


def test_1_duplicate_labels_share_same_page_in_all_sources_and_both_evidence_places(detail_data):
    urls = ["https://www.kanopy-ai.com/", "https://kanopy-ai.com/about/", "https://kanopy-ai.com/about?lang=en#part",
            "https://kanopy-ai.com/?entry=one", "https://finsmes.com/hex", "https://other.example/hex/",
            "https://other.example/funding/", "https://other.example/funding/?v=2"]
    sources = [source(url, "www.kanopy-ai.com", None) for url in urls[:4]] + [
        source(url, "StrictlyVC", "Hex Technologies") for url in urls[4:]]
    set_sources(detail_data, sources)
    # 引用中的协议、www、末尾斜杠和片段不同，仍与目录共用同一页面写法。
    point = detail_data["companies"][0]["deepdive"]["items"][0]["points"][0]
    point["evidence"][1]["url"] = "http://www.kanopy-ai.com/about#another"
    point["evidence"] += [{**source("https://standalone.example/page", "Term Sheet", " ADSILICO "), "quote": "Synthetic extra quote."}]
    html = detail(detail_data)
    labels = [label for label, _ in source_rows(html, "source-row")]
    assert labels == ["kanopy-ai.com", "kanopy-ai.com/about", "kanopy-ai.com/about?lang=en", "kanopy-ai.com?entry=one",
                      "StrictlyVC · Hex Technologies", "StrictlyVC · Hex Technologies · other.example",
                      "StrictlyVC · Hex Technologies · other.example/funding",
                      "StrictlyVC · Hex Technologies · other.example/funding?v=2"]
    assert len(labels) == len(set(labels))
    evidence_labels = [label.removeprefix("来源：") for label, _ in source_rows(html, "source")]
    assert evidence_labels == labels + labels + ["Term Sheet"]
    assert all("（" not in label and "(" not in label and "www." not in label for label in labels)
    keys = ui(detail_data, "[...ui.sourceLabels(d.companies[0].sources,['adsilico'])]")
    assert [key for key, _ in keys] == [site._same_page(url) for url in urls]


def test_1_same_page_sources_render_once_and_first_label_stays(detail_data):
    set_sources(detail_data, [source("http://www.carelumi.com/about/", "www.carelumi.com", None),
                              source("https://carelumi.com/about#intro", "carelumi.com", None),
                              source("https://carelumi.com/team/", "carelumi.com", None),
                              source("https://carelumi.com/contact/", "carelumi.com", None)])
    labels = [label for label, _ in source_rows(detail(detail_data), "source-row")]
    assert labels == ["carelumi.com", "carelumi.com/team", "carelumi.com/contact"]


def test_1_root_page_keeps_bare_domain_and_subpages_get_path(detail_data):
    """维护者x手改：同域名多行时首页写裸域名，其余（包括排在首页前面的子页）写域名加路径，不出现「域名 · 完整网址」。"""
    set_sources(detail_data, [source("https://carelumi.com/news", "carelumi.com", None),
                              source("https://carelumi.com/", "carelumi.com", None),
                              source("https://carelumi.com/about", "carelumi.com", None)])
    labels = [label for label, _ in source_rows(detail(detail_data), "source-row")]
    assert labels == ["carelumi.com/news", "carelumi.com", "carelumi.com/about"]
    assert len(set(labels)) == 3 and not any("https://" in label for label in labels)


def test_1_backend_and_frontend_domain_fallback_drops_www_without_inventing_title(detail_data):
    pages = {"1": {"url": "https://www.kanopy-ai.com/about/", "name": "www.kanopy-ai.com"},
             "2": {"url": "https://www.carelumi.com/", "name": "来源"}}
    site._name_pages(pages, [])
    assert [(p["name"], p["title"], p["published_at"]) for p in pages.values()] == [
        ("kanopy-ai.com", None, None), ("carelumi.com", None, None)]
    set_sources(detail_data, [source(pages["1"]["url"], "www.kanopy-ai.com", "不应猜网页标题")])
    assert source_rows(detail(detail_data), "source-row")[0][0] == "kanopy-ai.com"


def test_2_evidence_sources_are_two_lines_and_use_caption_tokens(detail_data):
    html = detail(detail_data)
    rows = source_rows(html, "source")
    assert rows == [(label, "北京时间 11-02 19:51 · 原文日期（美国东部时间）11-02") for label in ("Term Sheet", "来源：Term Sheet")]
    assert not re.search(r'</a> · 北京时间', html)
    style = (site.ASSETS / "style.css").read_text()
    assert ".source .muted" in style
    assert re.search(r"\.source \.muted[^{}]*\{font:var\(--t-caption\);color:var\(--c-muted\)\}", style)


@pytest.mark.parametrize("year", [2025, 2026])
@pytest.mark.parametrize("kind", ["both", "published", "original", "read"])
def test_3_three_date_states_and_published_without_original(detail_data, year, kind):
    day = f"{year}-12-30"
    short = day if year != 2026 else "12-30"
    src = source(published_at=f"{day} 19:51:59" if kind in {"both", "published"} else None,
                 original_date_et=day if kind in {"both", "original"} else None, beijing_at=f"{day} 20:11:50")
    set_sources(detail_data, [src])
    expected = {"both": f"北京时间 {short} 19:51 · 原文日期（美国东部时间）{short}",
                "published": f"北京时间 {short} 19:51", "original": f"原文日期（美国东部时间）{short}",
                "read": f"页面没写日期 · {short} 读取"}[kind]
    html = detail(detail_data)
    assert source_rows(html, "source-row") == [("Term Sheet", expected)]
    assert source_rows(html, "source") == [("Term Sheet", expected), ("来源：Term Sheet", expected)]
    assert "未知" not in html and not re.search(r"\d{2}:\d{2}:\d{2}", html)


@pytest.mark.parametrize("year", [2025, 2026])
def test_3_dates_share_year_rule_across_cards_sources_evidence_and_history(detail_data, year):
    day = f"{year}-12-30"
    short = day if year != 2026 else "12-30"
    set_sources(detail_data, [source(published_at=f"{day} 19:51:00", original_date_et=day)])
    detail_data["events"][0].update(date=day, first_seen=day, push_date=day)
    html = detail(detail_data)
    for render in ("ui.weekHtml(d)", "ui.listHtml(d,ui.defaultFilters())", "ui.ycHtml(d)"):
        detail_data["week"]["yc"] = [1]
        assert f"Term Sheet · {short}" in Text(ui(detail_data, render)).text
    assert all(short in time for _, time in source_rows(html, "source-row") + source_rows(html, "source"))
    assert f'<span class="ftl-when tnum">{short}</span>' in html
    assert [Text(facts(html)[key]).text for key in ("日期", "首次发现", "推送日期")] == [short] * 3
    assert ("2026-" not in Text(html).text) if year == 2026 else ("2025-12-30" in Text(html).text)


def test_3_source_timestamp_converts_to_beijing_before_formatting(detail_data):
    set_sources(detail_data, [source(published_at="2025-12-31T17:51:59Z", original_date_et=None)])
    assert source_rows(detail(detail_data), "source-row")[0][1] == "北京时间 01-01 01:51"
    assert "Term Sheet · 01-01" in Text(ui(detail_data, "ui.weekHtml(d)")).text


def test_3_backend_derives_original_date_from_published_at_when_missing(seeded):
    """维护者x手改：库里原文日期为空但有发布时间时，仍按发布时间换算美东日期（PRD 第 9 节「每个来源另写原文日期」，V17l 原有规则不变）。"""
    conn, _ = seeded
    conn.execute("UPDATE raw_item SET original_date_et=NULL WHERE id IN (2,15)")
    conn.execute("UPDATE deepdive_pages SET url='https://techcrunch.com/synthetic-funding-15/' WHERE company_id='mach9'")
    path, _ = snapshot(seeded)
    payload = json.loads((path / "c/mach9.json").read_text())
    sources = [s for s in payload["companies"][0]["sources"] if s.get("published_at")]
    assert sources and all(src["original_date_et"] for src in sources)
    html = render_page(path, "detailHtml", "mach9")
    rows = source_rows(html, "source-row")
    assert rows and all(time.startswith("北京时间 10-30 07:15 · 原文日期（美国东部时间）") for _, time in rows)


@pytest.mark.parametrize("baseline", [False, True])
def test_4_key_fields_order_and_verification_basis_for_regular_and_baseline(detail_data, baseline):
    company = detail_data["companies"][0]
    company["baseline_unpushed"] = baseline
    company["verification"] = [{"field": "金额", "text": "金额查证依据", "urls": [], "quotes": []},
                               {"field": "国家", "text": "国家查证依据", "urls": [], "quotes": []},
                               {"field": "城市", "text": "城市查证依据", "urls": [], "quotes": []}]
    html = detail(detail_data)
    fields = facts(html)
    assert list(fields) == ["本轮", "国家和依据", "官网", "日期", "首次发现"]
    assert "1000 万美元" in fields["本轮"] and '<small>种子轮（原文 Seed）</small>' in fields["本轮"]
    assert "领投：Synthetic Capital" in Text(fields["本轮"]).text
    assert "跟投：Synthetic Ventures" in Text(fields["本轮"]).text
    # R14n：说明移到字段表下面，仍逐条注明对应字段。
    notes = [Text(note).text for note in re.findall(r'<p class="facts-note">(.*?)</p>', html, re.S)]
    assert notes == ["待确认：金额 · 金额查证依据", "待确认：国家 · 国家查证依据", "待确认：城市 · 城市查证依据"]
    assert "金额查证依据" not in fields["本轮"] and "国家查证依据" not in fields["本轮"]
    assert "英国 · 伦敦 · 来源写公司在伦敦" in fields["国家和依据"]
    assert "国家查证依据" not in fields["国家和依据"] and "城市查证依据" not in fields["国家和依据"]
    assert "金额查证依据" not in fields["国家和依据"]
    assert "推送日期" not in fields and all("未披露" not in value for value in fields.values()) and "成立" not in html


@pytest.mark.parametrize("amount", ["1000 万美元", "金额待确认", "金额说法不一", "€1100 万（暂未换算成美元）", "", "金额未披露"])
def test_4_current_round_omits_missing_amount_and_absent_investors(detail_data, amount):
    event = detail_data["events"][0]
    event.update(amount=amount, round_text="种子轮", leads=["Y Combinator"], follows=[], city="城市待确认", country_basis="")
    fields = facts(detail(detail_data))
    funding = Text(fields["本轮"]).text
    assert "领投：Y Combinator" in funding and "跟投" not in funding and "未披露" not in funding
    assert funding.count("种子轮") == 1 and "种子轮 · 种子轮" not in funding
    if amount and "未披露" not in amount:
        assert amount in funding
    assert Text(fields["国家和依据"]).text == "英国"


def test_4_empty_country_website_dates_and_leads_omit_whole_cells(detail_data):
    detail_data["events"][0].update(country="", city="伦敦", country_basis="地点依据", amount="", leads=[], follows=[],
                                     date=None, first_seen=None, round_text="", founded="2020")
    detail_data["companies"][0]["website"] = ""
    fields = facts(detail(detail_data))
    assert list(fields) == ["本轮"]
    assert Text(fields["本轮"]).text == "种子轮"


@pytest.mark.parametrize("membership", ["cards", "round_pending", "filtered", "old", "baseline", "supplement", "next", "no_week"])
def test_5_main_week_controls_breadcrumb_sidebar_and_mobile_nav(detail_data, membership):
    company = detail_data["companies"][0]
    company["detail_url"] = "./c/adsilico.json"
    detail_data["events"].append({**detail_data["events"][0], "id": 2})
    detail_data["week"].update(cards=[], round_pending=[], filtered=[])
    if membership in {"cards", "round_pending", "filtered"}:
        # 详情当前事件是 1，本周另一条事件 2 也应让这家公司归本周。
        detail_data["week"][membership] = [2]
    elif membership == "baseline":
        company["baseline_unpushed"] = True
    elif membership == "supplement":
        detail_data["events"][0]["supplement_unsent"] = True
    elif membership == "next":
        detail_data["events"][0]["next_push_label"] = "下次推送：11-06 09:00"
    elif membership == "no_week":
        detail_data.pop("week")
    result = node(r"""
      const fs=require('fs'),vm=require('vm'),d=JSON.parse(process.argv[2]);
      const navs=['side','mobile'].flatMap(place=>['week','list'].map(nav=>({place,dataset:{nav},attrs:{},
        classList:{toggle(k,v){this[k]=v}},setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]}})));
      const main={innerHTML:''},ctx={URL,location:{hash:'#c/adsilico'},
        document:{body:{style:{}},getElementById:()=>main,addEventListener:()=>{},
          querySelectorAll:s=>s==='[data-nav]'?navs:[]},window:{addEventListener:()=>{}},
        fetch:async url=>({ok:true,json:async()=>url==='./data.json'?d:
          {companies:d.companies,events:d.events,wording:d.wording}})};
      vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
      (async()=>{await new Promise(resolve=>setImmediate(resolve));
        console.log(JSON.stringify({html:main.innerHTML,nav:navs.map(n=>[n.place,n.dataset.nav,n.classList.on,n.attrs['aria-current']||null])}));})();
    """, json.dumps(detail_data, ensure_ascii=False))
    nav = "week" if membership in {"cards", "round_pending"} else "list"
    label = "本周" if nav == "week" else "往期"
    crumb = re.search(r'<nav class="crumb".*?</nav>', result["html"], re.S).group(0)
    assert f'href="#{nav}"' in crumb and Text(crumb).text == f"{label}›adsilico"
    for _, item, active, current in result["nav"]:
        assert active == (item == nav)
        assert current == ("page" if item == nav else None)


def test_5_detail_without_week_defaults_to_archive(detail_data):
    detail_data.pop("week")
    html = detail(detail_data)
    assert re.search(r'<nav class="crumb"[^>]*><a href="#list">', html)


@pytest.mark.parametrize("period", ["all", "date:2026-10-30"])
def test_5_archive_has_only_issue_select_and_prescribed_drawer_actions(detail_data, period):
    html = ui(detail_data, f"ui.listHtml(d,{{...ui.defaultFilters(),period:{json.dumps(period)}}})")
    assert 'type="search"' not in html and "排序" not in html
    assert re.findall(r'<select[^>]*id="([^"]+)"', html) == (["issue"] if period.startswith("date:") else [])
    assert '<h2 id="filter-title">筛选</h2>' in html and 'aria-label="关闭筛选"' in html
    assert re.findall(r'data-(?:reset|close)-filters>([^<]+)</button>', html) == ["重置", "完成"]
    assert re.search(r'id="open-filters"[^>]*>.*?筛选</button>', html)
    status = html.index('data-filter="status"')
    backfill = html.index("data-backfill-mark")
    assert status < backfill and 'data-value="backfill"' not in html


def test_6_detail_order_keeps_only_facts_verification_and_history_in_toc(detail_data):
    """维护者x手改：按 V17d usage「小标题栏加融资历史」，目录在第 8 项后、全部来源前加融资历史。"""
    company = detail_data["companies"][0]
    company["verification"] = [{"field": "国家", "text": "入库时已查证", "urls": [], "quotes": []}]
    company["conflicts"] = [{"field": "金额", "values": [{"url": "https://synthetic.example/", "name": "合成来源", "value": "金额不同"}]}]
    html = detail(detail_data)
    headings = [Text(h).text for h in re.findall(r'<h2 class="sec-t"[^>]*>(.*?)</h2>', html, re.S)]
    assert headings == ["简介", "关键字段"] + [f"{i:02d}{name}" for i, name in enumerate(site.ITEM_NAMES, 1)] + [
        "融资历史", "各来源说法不一", "全部来源"]
    assert html.index('class="crumb"') < html.index('class="co-head"') < html.index("资料有出入：") < html.index('>简介</h2>')
    assert '>入库查证</h2>' not in html and html.count("入库时已查证") == 1
    assert re.findall(r'data-detail-section="([^"]+)"', html) == [f"deep-{i}" for i in range(1, 9)] + ["fund-hist", "all-sources"]


@pytest.mark.parametrize("point,keep", [
    ({"field": "round", "value": "Seed", "text": "种子轮"}, True),
    ({"field": "round", "value": "pre-seed", "text": "种子轮"}, True),
    ({"field": "round", "value": "Series A", "text": "A 轮"}, False),
    ({"field": "amount", "text": "本轮金额", "quote": "Previously raised funding."}, False),
    ({"field": "leads", "text": "Earlier investors led the round."}, False),
    ({"field": "follows", "text": "本轮跟投", "evidence": [{"quote": "A prior round included investors."}]}, False),
    ({"field": "date", "text": "日期", "evidence": {"quote": "Raised funds last year."}}, False),
    ({"text": "由一群合成投资方出资"}, True),
    ({"text": "本轮以外", "evidence": [{"quote": "Previous funding supported the team."}]}, False),
    ({"field": "valuation", "text": "估值"}, False),
    ({"field": "amount", "text": "此前的金额"}, False),
    ({"field": "leads", "text": "之前的投资方"}, False),
    ({"field": "follows", "text": "上一轮跟投"}, False),
    ({"field": "amount", "text": "公司以前的融资"}, False),
    ({"field": "amount", "text": "本轮金额", "quote": "The current round is backed by Last Year Capital."}, False),
    ({"field": "amount", "text": "本轮金额", "quote": "The current round uses a priori analysis."}, True),
])
def test_7_current_funding_point_rules(point, keep):
    assert site._funding_point_is_current(point, {"round": "种子轮"}) is keep


@pytest.mark.parametrize("shape", ["named", "numbered", "list"])
def test_7_backend_filters_only_funding_and_fills_each_missing_subfield_without_writing(seeded, shape):
    conn, _ = seeded
    # 合成库另有同公司的天使轮，将本次要核对的种子轮设为详情当前事件。
    conn.execute("UPDATE funding_event SET first_ingested_at='2026-10-31 08:42:00' WHERE id=2")
    old = {"field": "amount", "text": "此前完成合成的旧轮次", "quote": "Previously raised seed funding.", "page": 1}
    current = {"field": "round", "value": "seed funding", "text": "本轮为种子轮", "quote": "Synthetic seed funding.", "page": 1}
    generic = {"text": "由一群合成投资方出资", "quote": "Synthetic investors backed the company.", "page": 1}
    funding = [old, current, generic, {"field": "round", "value": "Series A", "text": "另一个轮次"}]
    product = [{"text": "此前的产品事实仍保留", "quote": "Synthetic product fact.", "page": 1}]
    raw = {"产品是什么": product, "融资": funding}
    if shape == "numbered":
        raw = {"items": {"item1": {"points": product}, "item5": {"points": funding}}}
    elif shape == "list":
        raw = {"items": [{"name": name, "points": points} for name, points in raw.items()]}
    encoded = json.dumps(raw, ensure_ascii=False)
    conn.execute("UPDATE deepdive SET result_json=? WHERE company_id='mach9'", (encoded,))
    path, data = snapshot(seeded)
    company = json.loads((path / "c/mach9.json").read_text())["companies"][0]
    item = company["deepdive"]["items"][4]
    assert [p["text"] for p in item["points"]] == ["本轮为种子轮", "由一群合成投资方出资"]
    assert item["missing"] == ["金额", "领投", "跟投", "日期"]
    assert company["deepdive"]["items"][0]["points"][0]["text"] == "此前的产品事实仍保留"
    assert conn.execute("SELECT result_json FROM deepdive WHERE company_id='mach9'").fetchone()[0] == encoded
    html = render_page(path, "detailHtml", "mach9")
    section = html.split('id="deep-5"', 1)[1].split("</section>", 1)[0]
    assert "此前完成合成的旧轮次" not in section and "另一个轮次" not in section
    assert '<dl class="kv">' in section and 'class="point-label"' not in section
    assert "<dd>种子轮 · 3400 万美元</dd>" in section
    assert "<dd>First Round Capital</dd>" in section and "<dd>Bessemer Venture Partners</dd>" in section
    assert "<dd>10-30</dd>" in section and "来源里没写" not in section


def test_7_filter_preserves_input_and_fills_all_fields_when_every_point_removed():
    points = [{"field": "round", "value": "Series A", "text": "旧轮次"}, {"text": "此前的融资"}]
    before = deepcopy(points)
    filtered = site._current_funding_points(points, {"round": "种子轮"})
    assert points == before
    assert filtered == [{"field": field, "text": "来源里没写"} for field in ("round", "amount", "leads", "follows", "date")]


def test_7_prompt_requests_only_current_round():
    prompt = (site.ASSETS.parent / "prompts/deep.txt").read_text()
    assert "融资只写本轮（轮次、金额、领投、跟投、日期）；以前轮次不写进这一项，页面另有融资历史。金额和轮次尽量引用包含该轮次、日期的完整原句。" in prompt
    assert "本轮和历史融资分别列要点" not in prompt
