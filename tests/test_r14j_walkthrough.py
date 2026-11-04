"""R14j：设计走查中的来源契约、北京日期与前端组件，全部使用合成数据。"""
from html.parser import HTMLParser
import json
import re

import pytest

from vcradar import site, wording
from vcradar.sources.registry import DISPLAY_ORDER, P0_SOURCES
from test_site_build import seeded, snapshot, node, render_page, summary_titles  # noqa: F401
from test_r14c_rulings import page_html


class Text(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.parts = []
        self.feed(html)

    def handle_data(self, data):
        self.parts.append(data)

    @property
    def text(self):
        return "".join(self.parts)


def add_source(conn, source_id, *, published="2026-10-28T01:00:00Z", seen="2026-10-30 08:42:00"):
    rid = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM raw_item").fetchone()[0]
    url = f"https://original.example/handwritten-{rid}"
    conn.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,published_at,first_seen_at) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, source_id, str(rid), url, "手写来源标题", published, seen))
    conn.execute("INSERT INTO event_source VALUES (3,?,?,?,'{}',1,NULL)", (rid, url, "misleading.example"))
    return url


def test_w14_data_json_card_sources_use_only_p0_names_and_original_links(seeded):
    conn, _ = seeded
    conn.execute("UPDATE event_source SET source_name='misleading.example'")
    urls = [add_source(conn, source.id) for source in P0_SOURCES]
    path, data = snapshot(seeded)
    names = {source["name"] for event in data["events"] for source in event.get("sources", [])}
    assert names <= set(DISPLAY_ORDER) and names == set(DISPLAY_ORDER)
    event = next(event for event in data["events"] if event["id"] == 3)
    assert set(urls) <= {source["url"] for source in event["sources"]}
    html = render_page(path, "weekHtml")
    assert "misleading.example" not in html and all(url in html for url in urls)


def test_w14_frontend_uses_name_without_title_domain_or_origin_fallback(seeded):
    path, data = snapshot(seeded)
    event = next(event for event in data["events"] if event["id"] == 3)
    event["sources"] = [{"name": "StrictlyVC", "url": "https://original.example/funding", "published_at": None,
                          "origin_name": "misleading.example", "title": "别的报道标题"}]
    (path / "data.json").write_text(json.dumps(data))
    html = render_page(path, "weekHtml")
    assert "StrictlyVC" in Text(html).text and "别的报道标题" not in Text(html).text
    assert "misleading.example" not in html and 'href="https://original.example/funding"' in html


def test_w15_backend_orders_sources_by_registry_then_ingestion(seeded):
    conn, _ = seeded
    later = add_source(conn, "tc_startups", seen="2026-10-30 08:44:00")
    earlier = add_source(conn, "tc_venture", seen="2026-10-30 08:43:00")
    for sid in reversed(["cbn_seed", "yc_launches", "yc_directory", "launch_hn", "strictlyvc", "alleywatch", "ts_web"]):
        add_source(conn, sid)
    _, data = snapshot(seeded)
    sources = next(event for event in data["events"] if event["id"] == 3)["sources"]
    ranks = [DISPLAY_ORDER.index(source["name"]) for source in sources]
    assert ranks == sorted(ranks)
    urls = [source["url"] for source in sources]
    assert urls.index(earlier) < urls.index(later)


def test_w15_frontend_preserves_frozen_order_group_date_and_amount(seeded):
    conn, _ = seeded
    frozen = [{"name": "StrictlyVC", "url": "https://original.example/first", "published_at": "2026-10-27 19:56:00"},
              {"name": "TechCrunch", "url": "https://original.example/second", "published_at": "2026-10-30 08:00:00"}]
    conn.execute("UPDATE digest SET card_sources_json=? WHERE date='2026-10-30'", (json.dumps({"3": frozen}),))
    path, data = snapshot(seeded)
    event = next(event for event in data["events"] if event["id"] == 3)
    assert event["sources"] == frozen
    result = node("const fs=require('fs'),ui=require(process.argv[1]),d=JSON.parse(fs.readFileSync(process.argv[2]));"
                  "d.week.cards=[3];console.log(JSON.stringify({html:ui.weekHtml(d),sources:d.events.find(e=>e.id===3).sources}));",
                  path / "data.json")
    assert result["sources"] == frozen
    assert result["html"].index("StrictlyVC") < result["html"].index("TechCrunch")
    assert 'data-date="2026-10-27"' in result["html"] and 'data-date="2026-10-30"' not in result["html"]
    assert event["amount"] in Text(result["html"]).text


@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Asia/Tokyo"])
def test_w16_shared_date_formatter_uses_meta_year_and_beijing_weekday(tz):
    result = node("const ui=require(process.argv[1]);process.env.TZ=process.argv[2];"
                  "console.log(JSON.stringify([ui.formatDate('2026-10-31','2026-11-03'),"
                  "ui.formatDate('2025-12-30','2026-11-03'),ui.formatDate('2025-12-31T17:00:00Z','2026-11-03'),"
                  "ui.formatDate('2026-10-31','2026-11-03',false),ui.formatDate('2025-12-30','2026-11-03',false)]));", tz)
    assert result == ["10-31 周六", "2025-12-30 周二", "01-01 周四", "10-31", "2025-12-30"]


def test_w16_week_archive_issue_select_and_detail_share_year_rule(seeded):
    path, data = snapshot(seeded)
    data["meta"]["today"] = "2027-01-03"
    data["digests"] = [{"date": "2026-10-31"}, {"date": "2027-01-01"}]
    (path / "data.json").write_text(json.dumps(data))
    result = node("const fs=require('fs'),ui=require(process.argv[1]),d=JSON.parse(fs.readFileSync(process.argv[2]));"
                  "console.log(JSON.stringify({week:ui.weekHtml(d),list:ui.listHtml(d,{...ui.defaultFilters(),period:'date:2026-10-31'})}));",
                  path / "data.json")
    assert "2026-10-30 周五" in Text(result["week"]).text
    assert "2026-10-31 周六那期" in Text(result["list"]).text
    assert "01-01 周五那期" in Text(result["list"]).text
    detail = json.loads((path / "c/mach9.json").read_text())
    detail["meta"] = data["meta"]
    (path / "c/mach9.json").write_text(json.dumps(detail))
    html = render_page(path, "detailHtml", "mach9")
    fields = dict(re.findall(r'<dt>([^<]+)</dt><dd>(.*?)</dd>', html, re.S))
    assert fields[wording.SITE_TEXT["date"]] == "2026-10-30"
    data["meta"]["today"] = "2026-11-03"
    (path / "data.json").write_text(json.dumps(data))
    fields = dict(re.findall(r'<dt>([^<]+)</dt><dd>(.*?)</dd>', render_page(path, "detailHtml", "mach9"), re.S))
    assert fields[wording.SITE_TEXT["date"]] == "10-30"


def test_h5_header_tail_order_and_unsent_four_characters(seeded):
    path, data = snapshot(seeded)
    event = next(event for event in data["events"] if event["id"] == 3)
    labels = {e["next_push_label"] for e in data["events"] if e.get("next_push_label")}
    assert labels and all(re.fullmatch(r"下次推送：\d{2}-\d{2} 09:00", label) for label in labels)
    event.update(backfill="此前漏掉", status="轮次待确认", pending_reason="来源没写轮次",
                 next_push_label="下次推送：11-06 09:00")
    (path / "data.json").write_text(json.dumps(data))
    html = page_html(path)
    assert "下次推送：11-06 09:00" in Text(html).text and "周五 09:00" not in Text(html).text
    article = next(part for part in re.findall(r"<article\b[^>]*>.*?</article>", html, re.S) if "Flapping Airplanes" in part)
    assert article.index("此前漏掉") < article.index("轮次待确认 · 来源没写轮次") < article.index("下次推送：")
    event.update(supplement_unsent=True)
    (path / "data.json").write_text(json.dumps(data))
    html = page_html(path)
    article = next(part for part in re.findall(r"<article\b[^>]*>.*?</article>", html, re.S) if "Flapping Airplanes" in part)
    assert "补充未推送" in Text(article).text
    assert not any(word in Text(article).text for word in ("此前漏掉", "下次推送", "来源没写轮次"))


def test_w13_h7_empty_sentences_and_no_search_or_sort(seeded):
    path, data = snapshot(seeded)
    data["week"]["cards"] = []
    (path / "data.json").write_text(json.dumps(data))
    result = node("const fs=require('fs'),ui=require(process.argv[1]),d=JSON.parse(fs.readFileSync(process.argv[2]));"
                  "console.log(JSON.stringify({week:ui.weekHtml(d),list:ui.listHtml(d,{...ui.defaultFilters(),tag:'没有这个标签'})}));",
                  path / "data.json")
    assert Text(result["week"]).text.startswith(wording.NO_NEW)
    assert wording.SITE_TEXT["no_matches"] in Text(result["list"]).text
    assert 'type="search"' not in result["list"] and "data-sort" not in result["list"]


def test_g1_g4_exported_shell_and_fonts_are_self_contained(seeded):
    path, _ = snapshot(seeded)
    for name in ("tokens.css", "components.css", "icons.svg", "fonts/LICENSE.txt"):
        assert (path / name).read_bytes() == (site.ASSETS / name).read_bytes()
    css = (path / "components.css").read_text()
    for url in re.findall(r'url\("([^"]+)"\)', css):
        assert not url.startswith(("http:", "https:", "/"))
        assert (path / url).read_bytes() == (site.ASSETS / url).read_bytes()
    html = (path / "index.html").read_text()
    assert html.index("tokens.css") < html.index("components.css") < html.index("style.css")
    assert 'class="appbar"' in html and 'class="nav-side"' in html and 'class="tabbar"' in html
    assert html.count('data-nav="week"') == html.count('data-nav="list"') == 2
    assert 'data-theme-cycle' in html and 'data-theme-set="system"' in html
    assert not re.search(r'(?:src|href)="(?:https?:|/srv/private)', html)
    custom = (path / "style.css").read_text()
    assert not re.search(r'--[\w-]+\s*:', custom)
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(', custom)


def test_w2_w12_cards_sources_and_fold_use_component_structure(seeded):
    path, _ = snapshot(seeded)
    html = render_page(path, "weekHtml")
    assert re.search(r'<section class="dgroup"[^>]+><h2 class="dgroup-h">.*?</h2><div class="co-list"><article', html, re.S)
    article = re.findall(r'<article\b[^>]*>.*?</article>', html, re.S)[0]
    assert re.findall(r'<(?:div|p) class="(co-r1|co-prod|co-fund|co-desc|co-srcs|co-more-l)"', article) == [
        "co-r1", "co-prod", "co-fund", "co-desc", "co-srcs", "co-more-l"]
    assert 'class="cmark ' in article and 'aria-label="Flapping Airplanes 详情"' in article
    assert 'class="co-amt-i' in article and 'class="co-country' in article
    assert 'class="co-src"' in article and 'target="_blank"' in article and 'icons.svg#i-ext' in article
    assert summary_titles(html) == ["轮次待确认", "不在收录范围"]
    assert 'class="week-tail"' in html and 'class="fold"' in html and '<details open' not in html
    assert 'data-label="展开"' in html and 'data-label="收起"' in html
    css = (path / "components.css").read_text()
    assert '.co-src-i { display: inline-block; white-space: nowrap;' in css
    assert '.co-more, .co-card .co-src { min-height: 24px;' in css


def test_h1_h3_issue_select_only_appears_for_selected_issue(seeded):
    path, _ = snapshot(seeded)
    result = node("const fs=require('fs'),ui=require(process.argv[1]),d=JSON.parse(fs.readFileSync(process.argv[2]));"
                  "console.log(JSON.stringify({all:ui.listHtml(d,ui.defaultFilters()),issue:ui.listHtml(d,{...ui.defaultFilters(),period:'date:2026-10-30'})}));",
                  path / "data.json")
    assert '<select id="issue"' not in result["all"] and '<select id="issue"' in result["issue"]
    assert 'aria-pressed="true" data-filter="period" data-value="issue"' in result["issue"]
    assert "10-30 周五那期" in Text(result["issue"]).text
    for html in result.values():
        assert 'id="open-filters"' in html and 'class="fpanel drawer"' in html
        assert 'id="filter-scrim"' in html and 'data-close-filters' in html
        assert 'type="search"' not in html and 'data-sort' not in html


def test_d1_d5_detail_header_fields_pending_skeleton_and_source_titles(seeded):
    path, data = snapshot(seeded)
    html = render_page(path, "detailHtml", "capveon")
    assert 'class="crumb"' in html and 'class="co-head"' in html
    assert 'class="facts"' in html and 'class="head-country' in html
    title = re.search(r'<h1>(.*?)</h1>', html, re.S)[1]
    assert "国家待确认" not in title and "美国" not in title
    assert 'class="sec-pending"' in html and 'role="status"' in html
    assert 'class="deep-skeleton" aria-hidden="true"' in html and html.count('class="skl-section"') == 8
    assert "解读生成中" in Text(html).text
    assert not any(word in Text(html).text for word in ("产品介绍", "核心功能", "产品截图"))
    # 详情全部来源继续保留报道标题，不套用列表来源名限制。
    assert "手写融资信息" in Text(html).text
    complete = render_page(path, "detailHtml", "mach9")
    assert 'class="detail v2"' in complete and 'class="toc"' in complete
    assert re.findall(r'data-detail-section="([^"]+)"', complete) == [f"deep-{i}" for i in range(1, 9)] + ["fund-hist", "all-sources"]
    assert all(f'id="deep-{i}" tabindex="-1"' in complete for i in range(1, 9))


def test_h1_drawer_focus_keyboard_rerender_and_theme_events(seeded):
    path, _ = snapshot(seeded)
    result = node(r"""
      const fs=require('fs'),vm=require('vm'),listeners={},windowListeners={},nodes={},themes=[];
      let doc,buttons=[],panel;
      class El {
        constructor(id,dataset={}) {this.id=id;this.dataset=dataset;this.attrs={};this.style={};this.inert=false;
          this.classList={toggle:(k,v)=>{this.attrs['class:'+k]=v}};}
        setAttribute(k,v){this.attrs[k]=v} removeAttribute(k){delete this.attrs[k]}
        getAttribute(k){return this.attrs[k] ?? null} focus(){doc.activeElement=this}
        querySelector(){return nodes.close}
        querySelectorAll(selector){return selector==='[data-filter]'?buttons:[nodes.close,...buttons,nodes.done]}
      }
      const main=new El('main'); let html='';
      Object.defineProperty(main,'innerHTML',{get:()=>html,set:value=>{
        html=value; panel=html.includes('id="filter-panel"')?new El('filter-panel'):null;
        nodes['filter-panel']=panel;nodes['filter-scrim']=new El('filter-scrim');
        nodes['open-filters']=new El('open-filters');nodes.close=new El('close');nodes.done=new El('done');
        buttons=[...html.matchAll(/data-filter="([^"]+)" data-value="([^"]+)"/g)].map(m=>new El('button',{filter:m[1],value:m[2]}));
      }});
      nodes.main=main; const root=new El('root'),background=[new El('side'),new El('tabs'),new El('bar'),new El('results')];
      for(const themeSet of ['light','dark','system'])themes.push(new El('theme',{themeSet}));
      doc={documentElement:root,body:new El('body'),activeElement:null,getElementById:id=>nodes[id],
        addEventListener:(k,fn)=>listeners[k]=fn,querySelectorAll:selector=>selector==='[data-theme-set]'?themes:selector==='[data-nav]'?[]:background};
      const storage=new Map(); const context={document:doc,location:{hash:'#list'},URL,
        window:{addEventListener:(k,fn)=>windowListeners[k]=fn,scrollTo:()=>{}},
        localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},matchMedia:()=>({matches:false}),
        fetch:async()=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(process.argv[2]))})};
      vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
      const flush=()=>new Promise(resolve=>setImmediate(resolve));
      const click=async(node,selector)=>{listeners.click({target:{closest:s=>s===selector?node:null},preventDefault:()=>{}});await flush();await flush()};
      (async()=>{
        await flush();await flush();
        await click(nodes['open-filters'],'#open-filters');
        const opened=panel.attrs.role==='dialog'&&panel.attrs['aria-modal']==='true'&&doc.body.style.overflow==='hidden'&&background.every(e=>e.inert);
        const issue=buttons.find(b=>b.dataset.value==='issue');await click(issue,'[data-filter]');
        const rerender=html.includes('<select id="issue"')&&doc.activeElement===buttons.find(b=>b.dataset.value==='issue')&&panel.attrs.role==='dialog';
        doc.activeElement=nodes.close;listeners.keydown({key:'Tab',shiftKey:true,preventDefault:()=>{}});
        const backwards=doc.activeElement===nodes.done;
        listeners.keydown({key:'Tab',shiftKey:false,preventDefault:()=>{}});
        const forwards=doc.activeElement===nodes.close;
        listeners.keydown({key:'Escape',preventDefault:()=>{}});
        const closed=!panel.attrs.role&&doc.activeElement===nodes['open-filters']&&doc.body.style.overflow===''&&background.every(e=>!e.inert);
        await click(themes[1],'[data-theme-set]');
        const theme=root.attrs['data-theme']==='dark'&&storage.get('vcr-theme')==='dark'&&themes[1].attrs['aria-pressed']==='true';
        console.log(JSON.stringify({opened,rerender,backwards,forwards,closed,theme}));
      })().catch(error=>{console.error(error);process.exitCode=1});
    """, path / "data.json")
    assert result == dict.fromkeys(("opened", "rerender", "backwards", "forwards", "closed", "theme"), True)
