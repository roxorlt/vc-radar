"""R14l：U 走查复核第 1 轮的第二轮修改（金额未披露不加粗、桌面筛选卡、分段文案、空结果间距、
详情来源写法、零卡片折叠名单与此前漏掉补充卡、轮次组选项），全部使用合成数据。"""
import json
import re
from pathlib import Path

from vcradar import site, wording
from vcradar.sources.registry import name_for_url
from test_site_build import seeded, snapshot, node, render_page  # noqa: F401
from test_r14c_rulings import page_html
from test_r14j_walkthrough import Text

W = wording.SITE_TEXT
STYLE = (site.ASSETS / "style.css").read_text()
ALL = {"period": "all", "tag": "all", "round": "all", "status": "all"}


def media_block(css, query):
    start = css.index("@media(" + query + "){")
    depth, i = 0, css.index("{", start)
    for j in range(i, len(css)):
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        if depth == 0:
            return css[i + 1:j]
    raise AssertionError(query)


def amount_class(html, name):
    article = next(a for a in re.findall(r"<article\b.*?</article>", html, re.S) if name in a)
    return re.search(r'<span class="(co-amt-i[^"]*)">([^<]*)</span>', article).groups()


# 1 金额未披露：is-none，具体金额不带修饰类
def test_1_amount_undisclosed_is_none_and_concrete_amount_plain(seeded):
    path, data = snapshot(seeded)
    firsts = {}
    for e in data["events"]:
        if e["id"] in data["listing"]:
            firsts.setdefault(e["company_id"], e)
    events = list(firsts.values())[:4]
    for e, amount in zip(events, [W["amount_missing"], "1,000 万美元", "", "金额待确认"]):
        for same in data["events"]:
            if same["company_id"] == e["company_id"]:
                same["amount"] = amount
    (path / "data.json").write_text(json.dumps(data, ensure_ascii=False))
    html = page_html(path, filters=ALL)
    assert amount_class(html, events[0]["name"]) == ("co-amt-i is-none", "金额未披露")
    assert amount_class(html, events[1]["name"]) == ("co-amt-i", "1,000 万美元")
    assert amount_class(html, events[2]["name"]) == ("co-amt-i is-none", "金额未披露")
    assert amount_class(html, events[3]["name"]) == ("co-amt-i is-pend", "金额待确认")
    week = render_page(path, "weekHtml")
    assert 'class="co-amt-i is-none">金额未披露' in week
    assert not re.search(r'class="co-amt-i">[^<]*未披露', week + html)


# 2 桌面筛选卡：组名 40 宽在左、与选项同一行；手机抽屉仍为上下排
def test_2_desktop_filter_line_flex_and_mobile_drawer_unchanged():
    desktop = media_block(STYLE, "min-width:1024px")
    assert ".fpanel.drawer .filter-line{display:flex;align-items:center;gap:var(--s-2)}" in desktop
    assert ".fpanel.drawer .filter-line>.lab{width:40px}" in desktop
    mobile = media_block(STYLE, "max-width:1023px")
    assert ".fpanel .filter-line{display:grid;gap:var(--s-3)}" in mobile


# 3 分段文案
def test_3_period_segment_reads_pick_one_issue(seeded):
    path, data = snapshot(seeded)
    html = page_html(path, filters=ALL)
    segments = re.findall(r'<button type="button" class="segment[^"]*"[^>]*data-filter="period"[^>]*>([^<]*)</button>', html)
    assert segments == ["本周", "指定某一期", "近 30 天", "全部"]
    assert "选择推送日期" not in html and "<select" not in html
    day = data["digests"][0]["date"]
    picked = page_html(path, filters={**ALL, "period": f"date:{day}"})
    assert re.search(r"<option[^>]*>[^<]*周[日一二三四五六]那期</option>", picked)


# 4 手机筛选结果为 0：句子离「筛选」按钮 8px（工具条下边距在手机为 --s-2）
def test_4_mobile_empty_result_gap_is_s2(seeded):
    components = (site.ASSETS / "components.css").read_text()
    assert ".tb { justify-content: flex-start; margin: 0 0 var(--s-2); }" in components
    assert not re.search(r"\.tb\s*\{", STYLE)
    tokens = (site.ASSETS / "tokens.css").read_text()
    assert re.search(r"--s-2:\s*8px", tokens)
    path, _ = snapshot(seeded)
    html = page_html(path, filters={**ALL, "tag": "不存在的标签"})
    assert html.endswith(f'<p class="empty-line">{W["no_matches"]}</p></div>')


# 5 详情来源：信源名 · 标题；北京时间 MM-DD HH:mm；非今年写年；没日期写读取日
def test_5_detail_sources_name_title_and_short_times(seeded):
    path, data = snapshot(seeded)
    html = render_page(path, "detailHtml", "mach9")
    rows = re.findall(r'<div class="source-row">(.*?)</div></div>', html, re.S)
    texts = [Text(r).text for r in rows]
    assert "TechCrunch · 手写融资信息北京时间 10-30 07:15 · 原文日期（美国东部时间）10-29" in texts
    assert any(t.endswith("页面没写日期 · 10-31 读取") for t in texts)
    assert not re.search(r"\d{2}:\d{2}:\d{2}", html) and "2026-" not in Text(html).text
    data["meta"]["today"] = "2027-01-05"
    (path / "data.json").write_text(json.dumps(data, ensure_ascii=False))
    later = Text(render_page(path, "detailHtml", "mach9")).text
    assert "北京时间 2026-10-30 07:15 · 原文日期（美国东部时间）2026-10-29" in later
    assert "页面没写日期 · 2026-10-31 读取" in later


def test_5_deep_pages_take_source_name_registry_name_or_domain():
    sources = [{"name": "StrictlyVC", "url": "https://finsmes.com/2026/10/marc.html", "title": "Marc",
                "published_at": "2026-10-24 13:45:00", "original_date_et": "2026-10-23"}]
    pages = {str(i): {"url": url, "name": name, "beijing_at": "2026-11-02 08:49:55", "original_date_et": None}
             for i, (url, name) in enumerate([
                 ("https://www.finsmes.com/2026/10/marc.html/", "www.finsmes.com"),
                 ("https://content.fortune.com/newsletter/termsheet?post_id=1", "content.fortune.com"),
                 ("https://www.ycombinator.com/companies/kanopy", "www.ycombinator.com"),
                 ("https://www.ycombinator.com/launches/abc", "www.ycombinator.com"),
                 ("https://www.ycombinator.com/blog", "www.ycombinator.com"),
                 ("https://techfundingnews.com/a", "techfundingnews.com"),
                 ("https://dealroom.co/just-founded/x/", "来源")], 1)}
    site._name_pages(pages, sources)
    assert pages["1"] | {} == {**pages["1"], "name": "StrictlyVC", "title": "Marc",
                               "published_at": "2026-10-24 13:45:00", "original_date_et": "2026-10-23"}
    assert [pages[k]["name"] for k in "234567"] == ["Term Sheet", "YC 公司目录", "YC Launches",
                                                     "ycombinator.com", "techfundingnews.com", "dealroom.co"]
    assert all(pages[k]["published_at"] is None for k in "23456")
    assert name_for_url("not a url") is None
    assert site._same_page("https://www.a.com/x/") == site._same_page("http://a.com/x")


def test_5_evidence_line_uses_same_wording(seeded):
    path, data = snapshot(seeded)
    company = json.loads((path / next(c for c in data["companies"] if c["id"] == "mach9")["detail_url"]).read_text())
    target = next(c for c in company["companies"] if c["id"] == "mach9")
    point = next(p for item in target["deepdive"]["items"] for p in item["points"] if p["evidence"])
    point["evidence"][0].update(name="TechCrunch", title="手写融资信息", published_at="2026-10-30 07:15:00",
                                original_date_et="2026-10-29")
    (path / next(c for c in data["companies"] if c["id"] == "mach9")["detail_url"]).write_text(json.dumps(company, ensure_ascii=False))
    html = render_page(path, "detailHtml", "mach9")
    lines = [Text(x).text for x in re.findall(r'<div class="source">(.*?)</div>', html, re.S)]
    assert "来源：mach9.ai北京时间 10-30 07:15 · 原文日期（美国东部时间）10-29" in lines
    assert all(re.search(r"(北京时间 \d{2}-\d{2} \d{2}:\d{2}|页面没写日期 · \d{2}-\d{2} 读取)", l) for l in lines)


# 6 零卡片 + 折叠名单有内容；名单为空时整块不出；此前漏掉补充卡尾标顺序
def test_6_zero_cards_with_folds_and_without(seeded):
    path, data = snapshot(seeded)
    ids = [e["id"] for e in data["events"]]
    data["week"].update(cards=[], round_pending=ids[:1], filtered=[])
    filtered = [e for e in data["events"] if e.get("filter_reasons")]
    if filtered:
        data["week"]["filtered"] = [filtered[0]["id"]]
    (path / "data.json").write_text(json.dumps(data, ensure_ascii=False))
    html = render_page(path, "weekHtml")
    assert html.startswith(f'<p class="empty-line">{wording.NO_NEW}</p><div class="week-tail">')
    assert html.count('<details class="fold">') == 1 + bool(filtered) and "co-card" not in html
    data["week"].update(round_pending=[], filtered=[])
    (path / "data.json").write_text(json.dumps(data, ensure_ascii=False))
    assert render_page(path, "weekHtml") == f'<p class="empty-line">{wording.NO_NEW}</p>'


def test_6_backfill_card_shows_mark_then_state_then_next_push(seeded):
    path, data = snapshot(seeded)
    event = next(e for e in data["events"] if e.get("next_push_label") and e["id"] in data["listing"])
    event.update(backfill="补充", status=W["round_pending"], pending_reason="来源没写轮次")
    (path / "data.json").write_text(json.dumps(data, ensure_ascii=False))
    article = next(a for a in re.findall(r"<article\b.*?</article>", page_html(path, filters=ALL), re.S) if event["name"] in a)
    r1 = Text(re.search(r'<div class="co-r1">.*?</div>', article, re.S).group(0)).text
    assert r1.index("此前漏掉") < r1.index("轮次待确认 · 来源没写轮次") < r1.index("下次推送：")
    assert re.search(r"下次推送：\d{2}-\d{2} 09:00$", r1)


# 7 「轮次待确认」只在「状态」组；筛选结果与状态一致
def test_7_round_pending_only_in_status_group(seeded):
    path, data = snapshot(seeded)
    html = page_html(path, filters=ALL)
    def group(label):
        line = re.search(r'<div class="filter-line"><span class="lab">' + label + r'</span>(.*?)</div></div>', html, re.S).group(1)
        return re.findall(r">([^<]+)</button>", line)
    assert group("轮次") == ["全部", "天使轮", "种子轮", "A 轮"]
    assert group("状态") == ["全部", "已纳入", "轮次待确认"]
    assert html.count('data-value="轮次待确认"') == 1
    pending = node("const fs=require('fs'),ui=require(process.argv[1]),d=JSON.parse(fs.readFileSync(process.argv[2]));"
                   "const f={period:'all',tag:'all',round:'all',status:'轮次待确认'};"
                   "console.log(JSON.stringify(ui.selectEvents(d,f).map(e=>[e.id,e.status,e.round])));", path / "data.json")
    assert pending and all(status == "轮次待确认" for _, status, _ in pending)
    cards = re.findall(r"<article\b", page_html(path, filters={**ALL, "status": "轮次待确认"}))
    companies = {e[0] for e in pending}
    assert 0 < len(cards) <= len(companies)
