"""用手写合成数据验证首屏体积、基线边界和按需详情。"""
import json
from pathlib import Path

import pytest

from vcradar import db, site
from vcradar.config import Config
from vcradar.sources.registry import P0_SOURCES

from test_site_build import NOW, add_company, add_deep, add_event, dump, node, render_page, seeded, snapshot


def json_keys(value):
    """递归枚举 JSON 中的键，用于检查内部字段泄漏。"""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from json_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_keys(item)


def size_database(conn):
    """构造 500 家纯基线和 50 家带完整深度解读的已纳入公司。"""
    db.sync_sources(conn, P0_SOURCES)
    for i in range(550):
        cid, eid = f"size-{i}", i + 1
        baseline = i < 500
        add_company(conn, cid, f"Terse Size {i}")
        add_event(conn, eid, cid, pushed=None if baseline else "2026-10-30")
        conn.execute("UPDATE funding_event SET baseline_unpushed=? WHERE id=?", (int(baseline), eid))
        conn.execute("UPDATE raw_item SET is_baseline=?,excerpt=?,structured_json=? WHERE id=?",
                     (int(baseline), "手写合成摘录。" * 500,
                      dump({"website": f"https://{cid}.ai/", "excerpt": "Synthetic source excerpt. " * 100}), eid))
        conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=?",
                     (dump({"website": f"https://{cid}.ai/", "amount_text": "$20M",
                            "quotes": {"summary": "Synthetic extraction evidence. " * 80}}), eid))
        if baseline:
            continue
        add_deep(conn, cid, "完成")
        points = {f"item{n}": {"points": [
            {"zh": f"第 {n} 项要点 {j}：" + "公司为团队提供软件，融资用于产品研发。" * 6,
             "evidence": [{"page": 1, "quote": "Synthetic software for teams. " * 8}],
             "entities": [{"name": "Mach9", "url": "https://mach9.ai/"}]} for j in range(4)]}
            for n in range(1, 9)}
        conn.execute("UPDATE deepdive SET result_json=?,materials_json=? WHERE company_id=?",
                     (dump({"items": points, "debug": {"body": "Synthetic private model trace. " * 100}}),
                      dump([{"page_no": 1, "url": "https://mach9.ai/news/funding",
                             "body": "Synthetic private material. " * 100}]), cid))
        conn.execute("UPDATE deepdive_pages SET body=? WHERE company_id=?",
                     ("Synthetic software for teams. " * 250, cid))
    conn.execute("INSERT INTO digest(date,push,event_ids_json,generated_at) VALUES (?,?,?,?)",
                 ("2026-10-30", 1, dump(list(range(501, 551))), "2026-10-30 08:55:00"))
    conn.commit()


def test_large_snapshot_stays_below_mobile_budget(tmp_path, monkeypatch):
    """500 家纯基线仅保留详情，完整深度结果不增加首屏传输体积。"""
    cfg = Config(state_dir=tmp_path)
    monkeypatch.setattr(site, "LINK_RECORDS", tmp_path / "missing-links.json")
    conn = db.connect(cfg.db_path)
    try:
        size_database(conn)
        path = site.build_site(conn, cfg, now=NOW)
        data = json.loads((path / "data.json").read_bytes())
        assert (path / "data.json").stat().st_size < 500_000
        assert len(data["listing"]) == 50
        assert {c["id"] for c in data["companies"]} == {f"size-{i}" for i in range(550)}
        assert len(list((path / "c").glob("*.json"))) == 550
        assert len(data["events"]) == 50 and not any(e.get("baseline_unpushed") for e in data["events"])
        baselines = [json.loads((path / f"c/size-{i}.json").read_bytes())["events"][0] for i in range(500)]
        assert len(baselines) == 500
        for event in baselines:
            assert set(event) == {"id", "company_id", "round", "round_text", "amount", "leads", "follows",
                                  "hq", "hq_basis", "date", "first_seen", "push_date", "country", "country_basis", "city"}
            assert event["push_date"] is None
        assert site._snapshot(conn, cfg, NOW)["status"]["deepdive_progress"]["total"] == 50
        forbidden = {"excerpt", "body", "structured", "structured_json", "extraction", "extraction_json",
                     "result_json", "materials_json", "links_json", "judgement", "debug"}
        for output in path.rglob("*.json"):
            payload = json.loads(output.read_bytes())
            assert payload["wording"]["description"] == "简介"
            business = {key: value for key, value in payload.items() if key != "wording"}
            assert not forbidden.intersection(json_keys(business)), output
            assert "Synthetic private" not in output.read_text()
        detail = json.loads((path / "c/size-500.json").read_bytes())
        company = detail["companies"][0]
        assert len(company["deepdive"]["items"]) == 8
        point = company["deepdive"]["items"][0]["points"][0]
        assert point["evidence"][0]["quote"] == "Synthetic software for teams. " * 8
        assert point["evidence"][0]["page_no"] == "1"
        assert point["entities"][0]["url"] == "https://mach9.ai/"
        assert company["deepdive"]["online_at"] == "2026-10-31 09:02:00"
        assert not any("items" in c.get("deepdive", {}) for c in data["companies"])
        baseline = json.loads((path / "c/size-0.json").read_bytes())
        assert set(baseline["companies"][0]) == {"id", "event_id", "header", "sources", "baseline_unpushed", "country", "country_basis", "backfill"}
        assert not {"summary", "deepdive", "excerpt", "body"}.intersection(json_keys(baseline))
    finally:
        conn.close()


def test_baselines_reactivated_events_and_unpushed_list_match_sql(seeded):
    """纯基线在往期所有筛选中隐藏，新增和已推送事件按 P0-11 出现。"""
    conn, _ = seeded
    for i, cid in enumerate(["only-baseline", "baseline-pending", "baseline-pushed"], 101):
        add_company(conn, cid, cid.title())
        add_event(conn, i, cid, pushed="2026-10-23" if cid == "baseline-pushed" else None)
        conn.execute("UPDATE funding_event SET baseline_unpushed=1 WHERE id=?", (i,))
        conn.execute("UPDATE raw_item SET is_baseline=1 WHERE id=?", (i,))
    # 新轮次让基线公司重回名单；详情仍包含旧的融资历史与来源。
    add_event(conn, 104, "baseline-pending", pushed=None, round_norm="A 轮")
    path, data = snapshot(seeded)
    expected = {r[0] for r in conn.execute(
        "SELECT e.id FROM funding_event e WHERE json_array_length(e.filter_reasons_json)=0 "
        "AND e.baseline_unpushed=0")}
    actual = node("const ui=require(process.argv[1]),fs=require('fs');"
                  "const d=JSON.parse(fs.readFileSync(process.argv[2]));"
                  "console.log(JSON.stringify(['all','week','30','date:2026-10-23'].map(period=>"
                  "ui.selectEvents(d,{period,tag:'all',round:'all',status:'all'}).map(e=>e.id))));",
                  path / "data.json")
    assert set(actual[0]) == expected
    assert 101 not in actual[0] and 102 not in actual[0]
    assert 103 not in actual[0] and 104 in actual[0] and 16 in actual[0]
    assert all(not {101, 102, 103, 104, 16}.intersection(ids) for ids in actual[1:])
    assert (path / "c/only-baseline.json").exists()
    assert "only-baseline" in (path / "data.json").read_text()
    html = node("const ui=require(process.argv[1]),fs=require('fs');"
                "const d=JSON.parse(fs.readFileSync(process.argv[2]));"
                "console.log(JSON.stringify(ui.listHtml(d,{period:'all',tag:'all',round:'all',status:'all'})));",
                path / "data.json")
    assert "Only-Baseline" not in html and "首次运行时已有，未推送" not in html
    assert "下次推送：11-06 09:00" in html and "TryNearby" in html
    baseline_html = render_page(path, "detailHtml", "only-baseline")
    assert "首次运行时已有，未推送" not in baseline_html and "下次推送" not in baseline_html
    assert "关键字段" in baseline_html and "全部来源" in baseline_html
    for text in ["简介", "深度解读", "解读生成中", "融资历史", "手写的合成简介"]:
        assert text not in baseline_html
    detail = json.loads((path / "c/baseline-pending.json").read_bytes())
    assert {e["id"] for e in detail["events"]} == {102, 104}
    assert set(detail["companies"][0]["history"]) == {102, 104}
    assert {s["url"] for s in detail["companies"][0]["sources"]} == {
        "https://techcrunch.com/synthetic-funding-102/", "https://techcrunch.com/synthetic-funding-104/"}


def test_detail_loader_fetches_only_requested_company_and_caches(seeded):
    """使用页面实际加载函数读取自足详情，并合并同家公司并发请求。"""
    conn, _ = seeded
    conn.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,published_at,first_seen_at) "
                 "VALUES (200,'cbn_main','synthetic-conflict','https://news.crunchbase.com/synthetic-conflict/',"
                 "'手写金额分歧','2026-10-30T01:00:00Z','2026-10-30 09:00:00')")
    conn.execute("INSERT INTO event_source VALUES (2,200,?,?,?,?,?)",
                 ("https://news.crunchbase.com/synthetic-conflict/", "Crunchbase News", dump({"amount_text": "$36M"}), 1, None))
    path, _ = snapshot(seeded)
    checked = node("""
      const ui=require(process.argv[1]),fs=require('fs'),path=require('path');
      const data=JSON.parse(fs.readFileSync(process.argv[2])),urls=[];
      const load=ui.createDetailLoader(data,async url=>{
        urls.push(url);
        return {ok:true,json:async()=>JSON.parse(fs.readFileSync(path.join(path.dirname(process.argv[2]),decodeURIComponent(url))))};
      });
      (async()=>{
        ui.weekHtml(data); ui.listHtml(data,{period:'all',tag:'all',round:'all',status:'all'}); ui.ycHtml(data);
        const before=urls.length;
        const [first,second]=await Promise.all([load('mach9'),load('mach9')]);
        const cached=await load('mach9'),missing=await load('missing');
        console.log(JSON.stringify({before,urls,same:first===second && first===cached,missing,
          ids:first.events.map(e=>e.id),html:ui.detailHtml(first,'mach9')}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """, path / "data.json")
    assert checked["before"] == 0 and checked["urls"] == ["./c/mach9.json"]
    assert checked["same"] and checked["missing"] is None
    assert set(checked["ids"]) == {2, 15}
    for text in ["产品是什么", "资料有出入：国家", "融资历史", "各来源说法不一", "全部来源",
                 "Mach9 builds software for teams.", "Bessemer Venture Partners"]:
        assert text in checked["html"]


@pytest.mark.parametrize("failure", ["http", "network", "json", "content"])
def test_detail_loader_failure_can_be_retried(seeded, failure):
    """HTTP、连接和内容失败都不能把失败结果永久缓存。"""
    path, _ = snapshot(seeded)
    checked = node("""
      const ui=require(process.argv[1]),fs=require('fs'),path=require('path');
      const data=JSON.parse(fs.readFileSync(process.argv[2])); let calls=0;
      const load=ui.createDetailLoader(data,async url=>{
        if (++calls===1) {
          if(process.argv[3]==='network') throw new Error('synthetic network failure');
          return {ok:process.argv[3]!=='http',json:async()=>{
            if(process.argv[3]==='json') throw new Error('synthetic JSON failure');
            return {events:[],companies:[]};
          }};
        }
        return {ok:true,json:async()=>JSON.parse(fs.readFileSync(path.join(path.dirname(process.argv[2]),decodeURIComponent(url))))};
      });
      (async()=>{let failed=false;try{await load('mach9');}catch(_){failed=true;}
        const detail=await load('mach9');
        console.log(JSON.stringify({failed,calls,id:detail.companies[0].id}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """, path / "data.json", failure)
    assert checked == {"failed": True, "calls": 2, "id": "mach9"}


def test_oversize_index_preserves_entire_previous_snapshot(seeded, monkeypatch):
    """大小保护失败时，本版与上一版的详情文件均保持原样。"""
    path, _ = snapshot(seeded)
    snapshot(seeded)
    before = {p: p.read_bytes() for root in [path, path.with_name("site.prev")]
              for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(site, "MAX_DATA_BYTES", 1)
    with pytest.raises(ValueError, match="超过 500KB"):
        snapshot(seeded)
    assert all(p.read_bytes() == value for p, value in before.items())
    assert not list(path.parent.glob(".site-*"))


def test_nested_detail_write_failure_preserves_entire_snapshot(seeded, monkeypatch):
    """详情写入失败也应清理暂存目录，不替换站点。"""
    path, _ = snapshot(seeded)
    before = {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}
    write_text = Path.write_text

    def fail_detail(target, *args, **kwargs):
        if target.parent.name == "c":
            raise OSError("合成详情写入失败")
        return write_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_detail)
    with pytest.raises(OSError, match="合成详情写入失败"):
        snapshot(seeded)
    assert all(p.read_bytes() == value for p, value in before.items())
    assert not list(path.parent.glob(".site-*"))


@pytest.mark.parametrize("route", ["#week", "#list", "#sources", "#c/mach9", "#c/only-baseline"])
def test_route_bootstrap_does_not_prefetch_details(seeded, route):
    """执行完整前端入口，验证直达详情能加载且其他路由不预取详情。"""
    conn, _ = seeded
    add_company(conn, "only-baseline", "Only-Baseline")
    add_event(conn, 101, "only-baseline", pushed=None)
    conn.execute("UPDATE funding_event SET baseline_unpushed=1 WHERE id=101")
    conn.execute("UPDATE raw_item SET is_baseline=1 WHERE id=101")
    path, _ = snapshot(seeded)
    checked = node("""
      const fs=require('fs'),vm=require('vm'),path=require('path');
      const nodes={header:{innerHTML:''},main:{innerHTML:''}},urls=[];
      const context={URL,location:{hash:process.argv[3]},
        document:{querySelectorAll:()=>[],getElementById:id=>nodes[id],addEventListener:()=>{}},
        window:{addEventListener:()=>{},scrollTo:()=>{}},
        fetch:async url=>{
          urls.push(url);
          return {ok:true,json:async()=>JSON.parse(fs.readFileSync(path.join(path.dirname(process.argv[2]),decodeURIComponent(url))))};
        }};
      vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
      (async()=>{await new Promise(resolve=>setImmediate(resolve));
        console.log(JSON.stringify({urls,html:nodes.main.innerHTML,title:context.document.title}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """, path / "data.json", route)
    assert checked["urls"] == ["./data.json"] + ([f"./c/{route[3:]}.json"] if route.startswith("#c/") else [])
    assert "正在读取" not in checked["html"] and "暂时无法读取" not in checked["html"]
    if route == "#c/mach9":
        assert "产品是什么" in checked["html"] and checked["title"].startswith("Mach9")
    if route == "#c/only-baseline":
        assert "关键字段" in checked["html"] and "全部来源" in checked["html"]
        assert "深度解读" not in checked["html"] and checked["title"].startswith("Only-Baseline")
        assert "下次推送" not in checked["html"]
    if route == "#list":
        assert "Only-Baseline" not in checked["html"] and "首次运行时已有，未推送" not in checked["html"]


def test_slow_detail_request_does_not_overwrite_new_route(seeded):
    """详情请求延迟返回时，页面应继续显示用户后来选中的路由。"""
    path, _ = snapshot(seeded)
    checked = node("""
      const fs=require('fs'),vm=require('vm'),path=require('path');
      const nodes={header:{innerHTML:''},main:{innerHTML:''}},listeners={},urls=[];
      let finish;
      const read=url=>JSON.parse(fs.readFileSync(path.join(path.dirname(process.argv[2]),decodeURIComponent(url))));
      const context={URL,location:{hash:'#c/mach9'},
        document:{querySelectorAll:()=>[],getElementById:id=>nodes[id],addEventListener:()=>{}},
        window:{addEventListener:(name,fn)=>listeners[name]=fn,scrollTo:()=>{}},
        fetch:url=>{
          urls.push(url);
          if(url==='./data.json') return Promise.resolve({ok:true,json:async()=>read(url)});
          return new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>read(url)});});
        }};
      vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
      (async()=>{await tick();const loading=nodes.main.innerHTML.includes('正在读取公司详情');
        context.location.hash='#sources';listeners.hashchange();
        finish();await tick();const sources=nodes.main.innerHTML;
        context.location.hash='#c/mach9';listeners.hashchange();await tick();
        console.log(JSON.stringify({loading,sources,html:nodes.main.innerHTML,urls}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """, path / "data.json")
    assert checked["loading"]
    assert "已接入" not in checked["sources"] and 'class="dgroup-h"' in checked["sources"] and "产品是什么" not in checked["sources"]
    assert "产品是什么" in checked["html"]
    assert checked["urls"] == ["./data.json", "./c/mach9.json"]
