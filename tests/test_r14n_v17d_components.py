"""R14n：V17d 组件同步、详情结构及路由回归；数据均为手写合成样例。"""
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re

import pytest

from vcradar import site, wording
from test_r14m_walkthrough_round3 import detail_data, detail, ui, source, set_sources  # noqa: F401
from test_site_build import node


DESIGN = Path("design-pack/20261103-u-round3/v17d/system")
W = wording.SITE_TEXT


@dataclass
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    @property
    def text(self):
        return "".join(child if isinstance(child, str) else child.text for child in self.children)

    def find(self, tag=None, cls=None):
        result = []
        for child in self.children:
            if isinstance(child, str):
                continue
            if (tag is None or child.tag == tag) and (cls is None or cls in child.attrs.get("class", "").split()):
                result.append(child)
            result.extend(child.find(tag, cls))
        return result


class DOM(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.root = Element("root")
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = Element(tag, dict(attrs))
        self.stack[-1].children.append(element)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        assert self.stack[-1].tag == tag, (self.stack[-1].tag, tag)
        self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(html):
    dom = DOM(html)
    assert len(dom.stack) == 1
    return dom.root


def test_1_components_match_design_bytes_and_tokens_icons_stay_identical():
    for name in ("components.css", "tokens.css", "icons.svg"):
        assert sha256((site.ASSETS / name).read_bytes()).digest() == sha256((DESIGN / name).read_bytes()).digest()


def test_1_style_defers_toolbar_heading_and_new_parts_to_components():
    style = (site.ASSETS / "style.css").read_text()
    assert not re.search(r"\.tb\s*\{|\.co-head h1\s*\{font:|\.history(?:>|\s+p)", style)
    assert not re.search(r"\.(?:evidence|ev-[\w-]+|ftl[\w-]*|diffnote|facts-note|cpop[\w-]*)\b[^{}]*\{", style)
    assert ".fpanel.drawer{position:static;transform:none;max-height:none;width:auto;overflow:visible}" in style


def test_2_detail_block_order_and_accessible_headings(detail_data):
    company = detail_data["companies"][0]
    company["verification"] = [{"field": "国家", "text": W["verified"], "urls": [], "quotes": []}]
    company["conflicts"] = [{"field": "金额", "values": [{"name": "合成媒体", "url": "https://media.example/", "value": "另一金额"}]}]
    html = detail(detail_data)
    tree = parse(html)
    positions = [html.index(marker) for marker in ('class="crumb"', 'class="co-head"', 'class="evidence"', '>简介</h2>',
                 '>关键字段</h2>', 'id="deep-1"', 'id="deep-8"', 'id="fund-hist"', 'class="diffnote"', 'id="all-sources"')]
    assert positions == sorted(positions)
    assert html.index(">入库查证</h2>") < html.index(">简介</h2>")
    history = next(n for n in tree.find("section") if n.attrs.get("id") == "fund-hist")
    assert history.attrs["aria-labelledby"] == history.find("h2")[0].attrs["id"] == "h-fund-hist"
    assert history.find("h2")[0].text == "融资历史" and not history.find(cls="no")
    assert history.find("use")[0].attrs["href"] == "./icons.svg#i-funding"
    assert re.findall(r'data-detail-section="([^"]+)"', html) == [f"deep-{i}" for i in range(1, 9)] + ["fund-hist", "all-sources"]
    assert not any(tree.find(cls=cls) for cls in ("ev-lead", "ev-foot", "ftl-total", "cpop", "cite", "pblock-intro", "pfeat", "shots"))


def test_3_evidence_has_final_source_labels_original_quotes_dates_and_no_explanations(detail_data):
    sources = [source("https://www.synthetic.example/about/", "www.synthetic.example", None),
               source("https://synthetic.example/", "synthetic.example", None)]
    set_sources(detail_data, sources)
    company = detail_data["companies"][0]
    evidence = company["deepdive"]["new_evidence"]
    evidence[0]["evidence"][0]["quote"] = 'Synthetic quote with <script> and "literal text".'
    evidence[0]["evidence"][0]["url"] = "http://www.synthetic.example/about#evidence"
    evidence.append({"field": "国家", "evidence": [deepcopy(evidence[0]["evidence"][1])]})
    html = detail(detail_data)
    tree = parse(html)
    block = tree.find("section", "evidence")[0]
    heading = block.find("div", "ev-h")[0]
    assert block.attrs["aria-labelledby"] == heading.attrs["id"]
    assert heading.find("b")[0].text == "资料有出入：金额、国家"
    assert heading.find("use")[0].attrs["href"] == "./icons.svg#i-info"
    items = block.find("li")
    assert len(items) == 3
    assert [item.find("span", "ev-src")[0].text for item in items] == ["synthetic.example/about", "synthetic.example", "synthetic.example"]
    assert items[0].find("q")[0].text == 'Synthetic quote with <script> and "literal text".'
    for item in items:
        assert [n.tag for n in item.children] == ["span", "q", "span"]
        assert item.find("a")[0].attrs["target"] == "_blank"
        assert item.find("span", "muted")[0].text == "北京时间 11-02 19:51 · 原文日期（美国东部时间）11-02"
    assert not block.find("script") and not block.find("blockquote") and not block.find(cls="source")
    assert not any(block.find(cls=cls) for cls in ("ev-zh", "ev-lead", "ev-foot", "alert"))


def test_3_funding_timeline_counts_current_round_missing_values_and_newest_first(detail_data):
    current = detail_data["events"][0]
    detail_data["events"] += [{**current, "id": 2, "date": "2025-12-30", "amount": "", "round": "", "follows": []},
                              {**current, "id": 3, "date": "2026-10-01", "amount": W["amount_missing"], "yc_batch": "Fall 2026", "follows": []}]
    company = detail_data["companies"][0]
    company["history"] = [2, 3, 1]
    company["deepdive"]["entities"] = [{"name": "Synthetic Capital", "url": "https://investor.example/"}]
    tree = parse(detail(detail_data))
    items = tree.find("li", "ftl-item")
    assert len(items) == len(company["history"])
    assert [item.attrs["data-event-id"] for item in items] == ["1", "3", "2"]
    assert [item.attrs["data-event-id"] for item in items if "is-current" in item.attrs["class"].split()] == [str(company["event_id"])]
    assert [item.find("span", "ftl-when")[0].text for item in items] == ["11-02", "Fall 2026（YC）", "2025-12-30"]
    assert items[0].find("span", "ftl-amt")[0].text == "1000 万美元"
    assert items[0].find("p", "ftl-who")[0].text == "Synthetic Capital 领投；Synthetic Ventures 跟投"
    assert items[0].find("p", "ftl-who")[0].find("a", "ext-link")[0].attrs["href"] == "https://investor.example/"
    assert not any("跟投" in item.find("p", "ftl-who")[0].text for item in items[1:])
    assert [item.find("span", "ftl-amt")[0].text for item in items[1:]] == [W["amount_missing"]] * 2
    assert all(item.find("span", "is-none") for item in items[1:])
    assert "is-unknown" in items[-1].attrs["class"].split() and items[-1].find("span", "ftl-round")[0].text == "轮次未披露"
    assert all(item.find("span", "ftl-dot")[0].attrs["aria-hidden"] == "true" for item in items)


def test_3_funding_does_not_invent_dates_or_treat_pending_amount_as_missing(detail_data):
    event = detail_data["events"][0]
    event.update(date=None, round=W["round_pending"], amount="金额待确认", leads=["Y Combinator"], follows=[])
    item = parse(detail(detail_data)).find("li", "ftl-item")[0]
    assert item.find("span", "ftl-when")[0].text == "以前"
    assert item.find("span", "ftl-amt")[0].text == "金额待确认" and not item.find("span", "is-none")
    assert item.find("span", "ftl-round")[0].text == W["round_missing"] and "is-unknown" in item.attrs["class"]
    assert item.find("p", "ftl-who")[0].text == "Y Combinator"


def test_3_skipped_yc_keeps_funding_history_hidden(detail_data):
    detail_data["companies"][0]["deepdive"]["status"] = W["skipped"]
    tree = parse(detail(detail_data))
    assert not tree.find(cls="ftl") and not tree.find(cls="ftl-item")


def test_3_conflicts_use_diffnote_with_one_linked_list_per_field(detail_data):
    detail_data["companies"][0]["conflicts"] = [
        {"field": "金额", "values": [{"name": "合成媒体甲", "url": "https://one.example/", "value": "100 万美元"},
                                   {"name": "合成媒体乙", "url": "https://two.example/", "value": "120 万美元"}]},
        {"field": "跟投", "values": [{"name": "合成官网", "url": "https://company.example/", "value": "<Synthetic Ventures>"}]}]
    blocks = parse(detail(detail_data)).find("div", "diffnote")
    assert len(blocks) == 2
    assert [block.find("b")[0].text for block in blocks] == ["金额", "跟投"]
    assert [len(block.find("li")) for block in blocks] == [2, 1]
    assert blocks[0].find("li")[0].text == "合成媒体甲：100 万美元"
    assert blocks[1].find("li")[0].text == "合成官网：<Synthetic Ventures>"
    for block in blocks:
        assert block.find("use")[0].attrs["href"] == "./icons.svg#i-warn"
        assert not block.find(cls="alert")
        for item in block.find("li"):
            assert len(item.find("span", "muted")[0].find("a")) == 1


@pytest.mark.parametrize("baseline", [False, True])
def test_3_facts_notes_follow_grid_and_keep_field_reasons_links_and_verified_status(detail_data, baseline):
    company = detail_data["companies"][0]
    company["baseline_unpushed"] = baseline
    company["verification"] = [
        {"field": "金额", "text": "金额查证说明", "reason": "来源未确认", "urls": ["https://verify.example/"], "quotes": ["Synthetic amount quote."]},
        {"field": "轮次", "text": "轮次查证说明", "urls": [], "quotes": []},
        {"field": "国家", "text": W["verified"], "urls": ["https://country.example/"], "quotes": ["Synthetic country quote."]},
        {"field": "城市", "text": "城市查证说明", "reason": "地点不同", "urls": [], "quotes": []}]
    html = detail(detail_data)
    tree = parse(html)
    grid = tree.find("dl", "facts")[0]
    notes = tree.find("p", "facts-note")
    assert len(notes) == 4
    assert not grid.find("p") and all(term not in grid.text for term in ("查证说明", "来源未确认", "地点不同", W["verified"]))
    assert notes[0].text == "待确认：金额 · 金额查证说明 · 来源未确认 Synthetic amount quote."
    assert notes[1].text == "待确认：金额 · 轮次查证说明"
    assert notes[2].text == "国家 · 入库时已查证 Synthetic country quote." and not notes[2].find("b")
    assert notes[3].text == "待确认：城市 · 城市查证说明 · 地点不同"
    assert all(note.find("b")[0].text == "待确认：" for note in (notes[0], notes[1], notes[3]))
    assert notes[0].find("a")[0].attrs["href"] == "https://verify.example/"
    assert html.index("</dl>") < html.index('class="facts-note"')


def test_3_empty_optional_parts_stay_absent(detail_data):
    company = detail_data["companies"][0]
    company["deepdive"]["new_evidence"] = []
    company["conflicts"] = []
    tree = parse(detail(detail_data))
    assert not any(tree.find(cls=cls) for cls in ("evidence", "diffnote", "facts-note", "ev-foot", "ftl-total", "cpop"))


@pytest.mark.parametrize("kind", ["both", "original", "published", "read"])
def test_4_evidence_keeps_all_four_source_date_states(detail_data, kind):
    day = "2025-12-30"
    set_sources(detail_data, [source(published_at=f"{day} 19:51:59" if kind in {"both", "published"} else None,
                                     original_date_et=day if kind in {"both", "original"} else None, beijing_at=f"{day} 22:30:59")])
    expected = {"both": f"北京时间 {day} 19:51 · 原文日期（美国东部时间）{day}", "original": f"原文日期（美国东部时间）{day}",
                "published": f"北京时间 {day} 19:51", "read": f"页面没写日期 · {day} 读取"}[kind]
    block = parse(detail(detail_data)).find("section", "evidence")[0]
    assert block.find("span", "muted")[0].text == expected


def test_1_main_list_class_switches_for_all_list_and_detail_routes(detail_data):
    result = node(r"""
      const fs=require('fs'),vm=require('vm'),d=JSON.parse(process.argv[2]),listeners={},classes=new Set(['main']);
      const main={innerHTML:'',classList:{toggle(k,on){on?classes.add(k):classes.delete(k)}}};
      const ctx={URL,location:{hash:'#week'},document:{body:{style:{}},querySelectorAll:()=>[],getElementById:()=>main,addEventListener:()=>{}},
        window:{addEventListener:(name,fn)=>listeners[name]=fn,scrollTo:()=>{}},fetch:async()=>({ok:true,json:async()=>d})};
      vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
      (async()=>{await tick();const results=[];
        for(const hash of ['#week','#c/adsilico','#list','#c/missing','#yc','#c/adsilico','#backfill','#list']) {
          ctx.location.hash=hash;listeners.hashchange();await tick();
          results.push({hash,list:classes.has('list'),main:classes.has('main'),html:main.innerHTML});
        }
        console.log(JSON.stringify(results));
      })().catch(error=>{console.error(error);process.exitCode=1});
    """, json.dumps(detail_data, ensure_ascii=False))
    assert len(result) == 8
    for item in result:
        assert item["list"] == (not item["hash"].startswith("#c/")) and item["main"]
        assert "正在读取" not in item["html"] and "暂时无法读取" not in item["html"]
