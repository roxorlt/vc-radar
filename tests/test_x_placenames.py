"""PRD #42：只用手写文字和临时库验证地名、重试、页面及离线修复。"""
from copy import deepcopy
from datetime import date, datetime, time
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from vcradar import backfill, cli, config, db, deep, extract, http, llm, pipeline, placenames, render, replay, scan, site, wording
from vcradar.config import BJ, Config
from vcradar.deep_fetch import Materials
from vcradar.models import DigestData, RawItem
from vcradar.search import SearchResult
from vcradar.sources.registry import P0_SOURCES
from test_deep_support import ManualClock, pages, payload, seed, database, HQ


NOW = datetime(2026, 10, 30, 8, 40, tzinfo=BJ)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("地名测试不能联网、读密钥或调真实模型")
    monkeypatch.setattr(config, "load_api_key", forbidden)
    monkeypatch.setattr(llm, "urlopen", forbidden)
    monkeypatch.setattr(http.urllib.request, "urlopen", forbidden)


def _normal(text, names=()):
    return placenames.normalize_place_text(text, protected_names=names)[0]


@pytest.mark.parametrize(("before", "after"), [
    ("总部位于哥伦比亚", "在哥伦比亚"),
    ("公司总部设在 San Francisco。", "公司在旧金山。"),
    ("公司总部在 New York。", "公司在纽约。"),
    ("公司是总部位于旧金山的协作软件公司。", "公司是旧金山的协作软件公司。"),
    ("公司的总部位于哥伦比亚。", "公司在哥伦比亚。"),
    ("总部位于美国旧金山的公司开发软件。", "在美国旧金山的公司开发软件。"),
    ("公司在旧金山。", "公司在旧金山。"),
    ("公司总部在 Emeryville。", "公司在 Emeryville。"),
    ("公司在 San Francisco, CA。", "公司在旧金山。"),
    ("公司在 SF，服务 NYC。", "公司在旧金山，服务纽约。"),
    ("公司在 New Mexico，服务 Colombia。", "公司在新墨西哥州，服务哥伦比亚。"),
    ("公司在 New York City。", "公司在纽约。"),
])
def test_42_1_3_common_places_and_fluent_sentences_are_idempotent(before, after):
    normalized = _normal(before)
    assert normalized == after
    assert placenames.normalize_place_text(normalized, protected_names=())[1] == []


@pytest.mark.parametrize(("text", "contains", "excluded"), [
    ("公司在 Paris, Texas。", "Paris, 得克萨斯州", "巴黎"),
    ("公司在 Paris, TX。", "Paris, TX", "巴黎"),
    ("公司在 London, Ontario, Canada。", "London", "伦敦"),
    ("公司在 Portland。", "Portland", "波特兰"),
    ("公司在 Portland, Maine。", "波特兰, 缅因州", "Portland"),
    ("公司在 Portland, ME。", "波特兰", "Portland"),
    ("公司在 Vancouver, WA。", "Vancouver", "温哥华"),
    ("公司在 Cambridge。", "Cambridge", "剑桥"),
    ("公司在 Cambridge, MA。", "剑桥", "Cambridge"),
    ("公司在 Birmingham, UK。", "伯明翰", "Birmingham"),
    ("公司在 Birmingham。", "Birmingham", "伯明翰"),
    ("公司在 Georgia。", "Georgia", "格鲁吉亚"),
    ("公司在 Tbilisi, Georgia。", "第比利斯, 格鲁吉亚", "Georgia"),
    ("公司在 Atlanta, Georgia。", "亚特兰大, 佐治亚州", "格鲁吉亚"),
])
def test_42_ambiguity_uses_qualifiers_and_survives_translated_states(text, contains, excluded):
    new = _normal(text)
    assert contains in new and excluded not in new
    assert _normal(new) == new
    assert not placenames.place_problems(new, protected_names=())


def test_42_2_company_product_investor_person_quotes_and_urls_are_protected():
    names = ["San Francisco Compute", "New York Canvas", "London Ventures", "Austin Example",
             "The company is headquartered in New York."]
    text = ("San Francisco Compute 总部在 Tel Aviv。New York Canvas 由 London Ventures 支持，"
            "Austin Example 创办。原句：The company is headquartered in New York. "
            "https://news.example/San Francisco Compute")
    # URL 本身不含空格；名字保护与网址保护都按实际区间生效。
    text = text.replace("https://news.example/San Francisco Compute", "https://news.example/London/New_York?q=Paris")
    new, hits = placenames.normalize_place_text(text, protected_names=names)
    assert "San Francisco Compute 在特拉维夫" in new
    assert all(name in new for name in names)
    assert "https://news.example/London/New_York?q=Paris" in new
    assert [hit["kind"] for hit in hits] == ["总部", "英文地名"]
    assert _normal("san francisco，SAN FRANCISCO，New Yorker，paris") == "san francisco，SAN FRANCISCO，New Yorker，paris"
    assert not placenames.place_problems(new, protected_names=names)
    assert _normal("Austin Example 创办公司，产品叫 New York Canvas，公司在 London。") == \
           "Austin Example 创办公司，产品叫 New York Canvas，公司在伦敦。"
    assert _normal("公司在 New York State。") == "公司在纽约州。"


def _source(name="Pioneer", location="San Francisco"):
    return RawItem("tc_venture", name, "https://news.example/" + scan.name_key(name).replace(" ", "-"),
        name + " raises $2M in seed funding", NOW,
        excerpt=f"{name} is based in {location} and builds software for community organizers.",
        body=f"{name} builds planning tools for community teams. Organizers plan events and coordinate volunteers. "
             "Teams use collaboration software every day. Investors include London Ventures. Products include New York Canvas.",
        structured={"company_name": name, "hq_text": location, "round_text": "Seed", "amount_text": "$2M",
                    "leads": ["London Ventures"]})


def _summary(name, location):
    return (f"{name} 是一家为社区组织者开发协作软件的公司，总部位于 {location}。"
            "产品帮助组织者安排活动、协调志愿者并管理日常事务。公司提供活动规划和团队协作工具，服务本地社区。")


def _extract(item, summaries, **kwargs):
    client = llm.MockLLMClient([{"summary": summary, "product_terms": ["New York Canvas"]} for summary in summaries], model="deepseek-flash")
    result = extract.model_extract(item, extract.rule_extract(item), client, "summary", **kwargs)
    return result, client


def test_42_4_retry_corrects_special_headquarters_and_uses_one_existing_slot():
    item = _source("Spruce", "London")
    broken = _summary("Spruce", "London").replace("总部位于 London", "总部：London")
    corrected = _summary("Spruce", "London").replace("总部位于 London", "在伦敦")
    result, client = _extract(item, [broken, corrected])
    assert result.model_calls == client.calls == 2
    assert "在伦敦" in result.summary and "总部" not in result.summary
    correction = json.loads(client.requests[1]["user"])["summary_correction"]
    assert "总部" in " ".join(correction["problems"])
    # 传给模型的是有问题的那句，并附规则，不另开第 3 次调用。
    assert "总部" in correction["previous_summary"]
    exhausted, client = _extract(item, [broken, broken, corrected])
    assert exhausted.model_calls == client.calls == 2 and "总部：伦敦" in exhausted.summary
    short_budget, client = _extract(item, [broken, corrected], budget_s=29)
    assert client.calls == 1 and "总部：伦敦" in short_budget.summary


def test_42_length_is_validated_before_place_replacement(caplog):
    item = _source()
    # 原文字数正好够，换成中文后不足 80 字，不为此增加调用。
    summary = "总部位于 San Francisco。" + "协" * 31 + "。" + "作" * 31 + "。"
    assert extract.summary_len(summary) >= 80
    result, client = _extract(item, [summary])
    assert extract.summary_len(result.summary) < 80 and client.calls == 1
    assert "summary_length" not in caplog.text


def test_42_7_concurrent_ingest_logs_replacements_retry_and_no_remaining(tmp_path):
    cfg = Config(state_dir=tmp_path, test_mode=True, links_file=tmp_path / "links.json")
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, P0_SOURCES)
    clock = pipeline.Clock(NOW.date(), time(8, 40))
    items = [_source("Pioneer"), _source("San Francisco Compute", "Tel Aviv"),
             _source("Reed", "Emeryville"), _source("Spruce", "London")]
    class PerCompanyClient:
        model = "deepseek-flash"
        def __init__(self):
            self.clients = {}
            for item in items:
                name, location = item.structured["company_name"], item.structured["hq_text"]
                first = _summary(name, location)
                outputs = [first]
                if name == "Spruce":
                    outputs = [first.replace("总部位于 London", "总部：London"), first.replace("总部位于 London", "在伦敦")]
                self.clients[name] = llm.MockLLMClient([{"summary": s} for s in outputs])
        def complete_json(self, system, user, timeout):
            name = json.loads(user)["rule_extracted"]["company_name"]
            return self.clients[name].complete_json(system, user, timeout)
    client = PerCompanyClient()
    entries = [(scan.upsert_raw_item(conn, item, NOW, False)[0], item, False) for item in items]
    log = []
    with placenames.place_audit(log.append):
        assert not pipeline.ingest_items(conn, entries, cfg=cfg, clock=clock, budget=pipeline._Budget(cfg, clock),
                                         client=client, batches=[])
    summaries = {row["name"]: row["summary"] for row in conn.execute("SELECT name,summary FROM company")}
    assert "在旧金山" in summaries["Pioneer"]
    assert "San Francisco Compute" in summaries["San Francisco Compute"] and "在特拉维夫" in summaries["San Francisco Compute"]
    assert "在 Emeryville" in summaries["Reed"] and "在伦敦" in summaries["Spruce"]
    assert client.clients["Spruce"].calls == 2
    assert all(client.clients[name].calls == 1 for name in summaries if name != "Spruce")
    totals = next(event for event in log if event["event"] == "place_summary")
    assert totals["checked"] == 5 and totals["retries"] == 1 and totals["remaining"] == 0
    assert totals["总部"] == 3 and totals["英文地名"] == 3
    assert len([event for event in log if event["event"] == "place_company"]) == 4
    conn.close()


@pytest.mark.parametrize("retry", [False, True])
def test_42_5_deep_normalizes_points_keeps_english_quote_and_retries_once(tmp_path, monkeypatch, retry):
    conn, cfg = database(tmp_path)
    seed(conn)
    material = pages()
    material[0].body = material[0].body.replace(HQ, "Cedar Labs is headquartered in New York.")
    monkeypatch.setattr(deep, "fetch_materials", lambda *a, **kw: Materials(pages=material, search=SearchResult(True)))
    original = payload()
    original["团队"][0].update(text="总部：New York。" if retry else "总部在 New York。",
                              quote="Cedar Labs is headquartered in New York.", value="New York")
    good = deepcopy(original)
    good["团队"][0]["text"] = "在纽约。"
    client = llm.MockLLMClient([original, good], model="deepseek-flash")
    outputs = deep.run_for_today(conn, cfg, ManualClock(), client=client, transport=object(),
                                search_client=object(), link_records=[])
    assert len(outputs) == 1 and outputs[0].status == "完成"
    point = json.loads(conn.execute("SELECT result_json FROM deepdive").fetchone()[0])["团队"][0]
    assert point["text"] == "在纽约。" and point["quote"] == "Cedar Labs is headquartered in New York."
    assert point["field"] == "hq" and point["value"] == "New York"
    assert client.calls == (2 if retry else 1)
    if retry:
        assert "place_wording" in client.requests[1]["user"]
    log = (cfg.log_dir / "deep-2026-10-31.log").read_text()
    assert '"event": "place_summary"' in log and "仍未替换 0" in log
    conn.close()


@pytest.mark.parametrize("remaining", [29, 180])
def test_42_5_deep_keeps_valid_text_when_no_retry_time_or_retry_fails(tmp_path, monkeypatch, remaining):
    conn, cfg = database(tmp_path)
    seed(conn)
    monkeypatch.setattr(deep, "fetch_materials", lambda *a, **kw: Materials(pages=pages(), search=SearchResult(True)))
    first = payload()
    first["团队"][0]["text"] = "总部：Boston。"
    company = deep.select_companies(conn, date(2026, 10, 31), now=ManualClock().now())[0]
    client = llm.MockLLMClient([first, {"error": "timeout"}])
    clock = ManualClock()
    from vcradar.deep_fetch import Budget
    output = deep.DeepResult(company["id"], company["name"], started_at=clock.now().isoformat())
    result = deep._work(company, cfg, clock, Budget(clock, remaining), object(), None, object(), client, [], output)
    assert result.status == "完成" and result.result["团队"][0]["text"] == "总部：波士顿。"
    assert client.calls == (1 if remaining == 29 else 2)


def test_42_backfill_summary_uses_the_same_replacements_retry_limit_and_fallback(tmp_path):
    conn, cfg = database(tmp_path)
    eid = seed(conn)
    candidate = {"company_id": "cedar", "name": "Cedar Labs", "event_id": eid, "model_calls": []}
    broken = _summary("Cedar Labs", "London").replace("总部位于 London", "总部：London")
    fixed = broken.replace("总部：London", "在伦敦")
    client = llm.MockLLMClient([{"summary": broken}, {"summary": fixed}])
    backfill._fill_summary(conn, candidate, cfg, NOW, client)
    assert "在伦敦" in candidate["summary"] and client.calls == 2
    assert len(candidate["model_calls"]) == 2
    candidate["model_calls"] = []
    client = llm.MockLLMClient([{"summary": broken}, {"error": "timeout"}])
    backfill._fill_summary(conn, candidate, cfg, NOW, client)
    assert "总部：伦敦" in candidate["summary"] and client.calls == 2
    conn.close()


def test_42_6_history_site_and_push_share_text_and_leave_source_quotes(tmp_path):
    cfg = Config(state_dir=tmp_path, test_mode=True, links_file=tmp_path / "links.json")
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, P0_SOURCES)
    item = _source("San Francisco Compute", "Tel Aviv")
    ext = extract.rule_extract(item)
    ext.summary = "San Francisco Compute 总部在 New York，开发 New York Canvas。产品服务社区。"
    ext.product_terms = ["New York Canvas"]
    raw_id, _ = scan.upsert_raw_item(conn, item, NOW, False)
    scan.ingest(conn, raw_id, item, ext, now=NOW, is_baseline=False, is_backfill=False, latest_yc_batches=[], model=None)
    cid = conn.execute("SELECT id FROM company").fetchone()[0]
    conn.execute("UPDATE company SET city='Tel Aviv',country='以色列' WHERE id=?", (cid,))
    old = "总部在 New York。"
    quote = "The company is headquartered in New York."
    result = {"团队": [{"text": old, "quote": quote, "page": 1}], "产品是什么": [], "融资": []}
    conn.execute("INSERT INTO deepdive(company_id,run_date,status,result_json,new_evidence_json,new_evidence_found_at) VALUES (?,?,'完成',?,?,?)",
                 (cid, str(NOW.date()), json.dumps(result), json.dumps([{"field": "总部", "quote": quote, "page": 1}]), NOW.isoformat()))
    conn.execute("INSERT INTO deepdive_pages(company_id,run_date,page_no,url,fetched_at,body) VALUES (?,?,1,?,?,?)",
                 (cid, str(NOW.date()), item.url, NOW.isoformat(), quote))
    before = conn.execute("SELECT summary FROM company").fetchone()[0]
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    card = listing["cards"][0]
    push = render.render_push(DigestData(NOW.date(), None, NOW, "有新增", cards=[card]), links_file=cfg.links_file)
    scan.mark_listed(conn, listing["event_ids"], NOW.date())
    scan.upsert_digest(conn, {"date": str(NOW.date()), "push": 1, "status": "有新增", "event_ids_json": json.dumps(listing["event_ids"]),
                              "text": push, "generated_at": NOW.isoformat()})
    conn.commit()
    path = site.build_site(conn, cfg, now=NOW)
    detail = json.loads((path / "c" / (cid + ".json")).read_text())
    company = detail["company"] if "company" in detail else detail["companies"][0]
    assert detail["events"][0]["city"] == "特拉维夫"
    assert company["summary"] == card.summary
    assert card.summary.partition("。")[0] + "。" in push
    assert "San Francisco Compute 在纽约" in push and "New York Canvas" in push
    assert "总部" not in push
    assert company["deepdive"]["items"][3]["points"][0]["text"] == "在纽约。"
    assert company["deepdive"]["items"][3]["points"][0]["evidence"][0]["quote"] == quote
    assert company["deepdive"]["new_evidence"][0]["field"] == "国家"
    snapshot = json.loads((path / "data.json").read_text())
    assert snapshot["status"]["new_evidence"][0]["fields"] == ["国家"]
    assert conn.execute("SELECT summary FROM company").fetchone()[0] == before
    assert conn.execute("SELECT result_json FROM deepdive").fetchone()[0] == json.dumps(result)
    assert {"总部位于", "总部在", "总部设在"} <= wording.BANNED_OLD_WORDS
    assert all("总部" not in value for value in wording.SITE_TEXT.values() if isinstance(value, str))
    from test_v16_wording import node
    for route in ("#week", "#sources", "#c/" + cid):
        script = r"""
          const fs=require('fs'),p=require('path'),vm=require('vm'),root=process.argv[1];
          const nodes={header:{innerHTML:''},main:{innerHTML:''}};
          const context={URL,location:{hash:ROUTE},
            document:{querySelectorAll:()=>[],getElementById:id=>nodes[id],addEventListener:()=>{}},
            window:{addEventListener:()=>{},scrollTo:()=>{}},
            fetch:async url=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(p.join(root,url)))})};
          vm.runInNewContext(fs.readFileSync(p.join(root,'app.js'),'utf8'),context);
          (async()=>{await new Promise(resolve=>setImmediate(resolve));
            console.log(JSON.stringify({html:nodes.main.innerHTML}));})();
        """.replace("ROUTE", json.dumps(route))
        html = node(path, script)["html"]
        assert "总部" not in html
        if route.startswith("#c/"):
            assert "国家和依据" in html and "资料有出入：国家" in html and "在纽约" in html
            assert quote in html
    conn.close()


def _old_database(cfg):
    conn = db.connect(cfg.db_path)
    old = ["总部位于哥伦比亚。", "总部在 San Francisco。", "在 Emeryville。", "公司在旧金山。", "总部所在地未写明。"]
    for index, summary in enumerate(old):
        seed(conn, cid="old" + str(index))
        conn.execute("UPDATE company SET summary=? WHERE id=?", (summary, "old" + str(index)))
    points = [{"text": "总部在 New York。", "quote": "headquartered in New York", "field": "hq", "value": "New York"},
              {"text": "在 San Francisco。", "quote": "based in San Francisco"},
              {"text": "在 Emeryville。", "quote": "based in Emeryville"}]
    conn.execute("INSERT INTO deepdive(company_id,run_date,status,result_json) VALUES ('old0','2025-01-01','完成',?)",
                 (json.dumps({"团队": points}),))
    conn.commit()
    conn.close()
    return old, points


def test_42_8_fix_preview_apply_backups_unresolved_and_idempotence(tmp_path, capsys):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    old, points = _old_database(cfg)
    # 模拟生产文件和 research 文件，只在 tmp_path 造副本，测试全程不碰实际生产文件。
    prod = cfg.state_dir / "vc-radar.db"
    prod.write_bytes(b"untouched production sentinel")
    research = tmp_path / "research" / "links.json"
    research.parent.mkdir()
    research.write_text('[{"name":"preserved"}]')
    hashes = [sha256(path.read_bytes()).hexdigest() for path in (prod, research, cfg.db_path)]
    args = cli.parse_args(["--fix-place-names", "--to", "test", "--report-dir", str(tmp_path / "report")])
    assert placenames.run_fix_place_names(cfg, args, ManualClock()) == 0
    assert [sha256(path.read_bytes()).hexdigest() for path in (prod, research, cfg.db_path)] == hashes
    assert not (cfg.db_path.parent / "data").exists()
    preview = json.loads((args.report_dir / "fix-place-names.json").read_text())
    assert preview["changed"] == 3 and not preview["backups"]
    assert any(entry["text"] == "总部所在地未写明。" for entry in preview["unresolved"])
    assert all({"company", "field", "before", "after", "hits"} <= change.keys() for change in preview["changes"])
    assert all(change["hit_count"] == len(change["hits"]) for change in preview["changes"])
    assert preview["counts"] == {"company": {"fields": 2, "总部": 2, "英文地名": 1},
                                 "deepdive": {"fields": 1, "总部": 1, "英文地名": 2}}
    args.apply = True
    assert placenames.run_fix_place_names(cfg, args, ManualClock()) == 0
    applied = json.loads((args.report_dir / "fix-place-names.json").read_text())
    assert applied["changed"] == 3 and applied["model_calls"] == applied["searches"] == 0
    assert len(applied["backups"]) == 2
    for path, table in zip(applied["backups"], ("company", "deepdive")):
        assert Path(path).parent == cfg.db_path.parent / "data" / "backup"
        with sqlite3.connect(path) as backup:
            if table == "company":
                assert backup.execute("SELECT summary FROM company WHERE id='old1'").fetchone()[0] == old[1]
            else:
                assert json.loads(backup.execute("SELECT result_json FROM deepdive").fetchone()[0])["团队"] == points
    with sqlite3.connect(cfg.db_path) as check:
        assert check.execute("SELECT summary FROM company WHERE id='old1'").fetchone()[0] == "在旧金山。"
        assert check.execute("SELECT summary FROM company WHERE id='old4'").fetchone()[0] == old[4]
        fixed = json.loads(check.execute("SELECT result_json FROM deepdive").fetchone()[0])["团队"]
        assert fixed[0]["text"] == "在纽约。" and fixed[0]["quote"] == points[0]["quote"]
        assert fixed[0]["field"] == "hq" and fixed[0]["value"] == "New York"
    log = cfg.db_path.parent / "data" / "fix-place-names.log"
    assert json.loads(log.read_text().splitlines()[0])["changed"] == applied["changed"]
    assert placenames.run_fix_place_names(cfg, args, ManualClock()) == 0
    assert json.loads((args.report_dir / "fix-place-names.json").read_text())["changed"] == 0
    assert len(list((cfg.db_path.parent / "data" / "backup").glob("*.db"))) == 2
    assert [sha256(path.read_bytes()).hexdigest() for path in (prod, research)] == hashes[:2]
    assert "模型调用 0，搜索 0" in capsys.readouterr().out


def test_42_8_fix_transaction_rolls_back_all_fields(tmp_path):
    cfg = Config(state_dir=tmp_path, test_mode=True)
    _old_database(cfg)
    with sqlite3.connect(cfg.db_path) as conn:
        before = conn.execute("SELECT summary FROM company ORDER BY id").fetchall()
        conn.execute("CREATE TRIGGER reject_deep BEFORE UPDATE ON deepdive BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    args = cli.parse_args(["--fix-place-names", "--to", "test", "--apply"])
    with pytest.raises(sqlite3.IntegrityError):
        placenames.run_fix_place_names(cfg, args, ManualClock())
    with sqlite3.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT summary FROM company ORDER BY id").fetchall() == before
    assert not (cfg.db_path.parent / "data" / "fix-place-names.log").exists()


@pytest.mark.parametrize("flags", [[], ["--date", "2026-10-30"], ["--to", "test"], ["--no-verify"],
    ["--fixture", "unread-fixture"], ["--replay", "unread-replay.json"], ["--reset-test-db"],
    ["--dry-run"], ["--rates-fail"], ["--prod-rules"], ["--now", "08:40"],
    ["--rates-file", "unread-rates.json"], ["--links-file", "unread-links.json"],
    ["--search-fixtures", "unread-search"], ["--assume-review-site-passed", "--dry-run"]])
def test_42_fix_is_b_parameter_and_all_test_flags_isolate(flags):
    args = cli.parse_args(["--fix-place-names", *flags])
    assert config.is_test_run(args) == bool(flags and flags[0] != "--date")


def test_42_prompts_fixed_replay_and_cli_do_not_read_models_or_other_commands(tmp_path, monkeypatch):
    sentence = "说公司在哪里，写「在<地点>」或「<地点>的公司」"
    root = Path(deep.__file__).parent / "prompts"
    assert all(sentence in (root / file).read_text() for file in ("extract_full.txt", "extract_summary.txt", "deep.txt"))
    assert 'field="hq"' in (root / "deep.txt").read_text()
    item = _source()
    ext = replay.fixed_extraction(item, {"summary": _summary("Pioneer", "San Francisco")})
    assert "在旧金山" in ext.summary and not ext.model_calls
    cfg = Config(state_dir=tmp_path, test_mode=True)
    _old_database(cfg)
    monkeypatch.setattr(cli, "load_config", lambda *a, **kw: cfg)
    assert cli.main(["--fix-place-names", "--apply", "--no-verify"]) == 0
    assert not cfg.outbox_dir.exists() and not (cfg.state_dir / "test-site").exists()


# #42①：译成中文后两侧不留和中文相邻的空格（实测样例 General Compute）。
def test_42_1_translated_place_drops_spaces_next_to_chinese():
    text = "General Compute 是总部位于 San Francisco 的云基础设施提供商。"
    after, hits = placenames.normalize_place_text(text, protected_names=["General Compute"])
    assert after == "General Compute 是旧金山的云基础设施提供商。"
    assert placenames.normalize_place_text(after, protected_names=["General Compute"]) == (after, [])
    assert placenames.normalize_place_text("在 Emeryville 的公司", protected_names=[])[0] == "在 Emeryville 的公司"


# PM 10-31 17:15 R2：「总部地点」→「所在地」，和「总部位于/设在/在」同一批检查；名称、原句、网址里的不动。
@pytest.mark.parametrize("before,after", [
    ("Viraj Aero 做的是航改型燃气轮机发电机组。来源未披露融资轮次、金额、领投方和总部地点。",
     "Viraj Aero 做的是航改型燃气轮机发电机组。来源未披露融资轮次、金额、领投方和所在地。"),
    ("来源未披露融资金额、轮次、领投方与总部地点，也未提及团队信息。", "来源未披露融资金额、轮次、领投方与所在地，也未提及团队信息。"),
])
def test_r2_headquarters_location_becomes_location_and_is_idempotent(before, after):
    text, hits = placenames.normalize_place_text(before, protected_names=["Viraj Aero"])
    assert text == after and [h["kind"] for h in hits] == ["总部"]
    assert placenames.normalize_place_text(text, protected_names=["Viraj Aero"]) == (text, [])
    assert placenames.place_problems(text, protected_names=["Viraj Aero"]) == []


def test_r2_headquarters_location_inside_protected_name_or_url_is_kept():
    text = "总部地点 Labs 的网址是 https://example.com/总部地点。"
    assert placenames.normalize_place_text(text, protected_names=["总部地点 Labs"]) == (text, [])


# R2：城市不在译名表时「城市, 州」整段保留英文，不拆开、不单独译州；表里的城市照旧整体换中文。
@pytest.mark.parametrize("before,after", [
    ("公司在 Framingham, MA，成立六年。", "公司在 Framingham, MA，成立六年。"),
    ("Re:Build总部位于Framingham, MA，成立六年。", "Re:Build在 Framingham, MA，成立六年。"),
    ("公司位于Framingham, Massachusetts。", "公司位于Framingham, Massachusetts。"),
    ("公司在Gardena, California。", "公司在Gardena, California。"),
    ("公司位于Mountain View, CA。", "公司位于山景城。"),
    ("公司位于Texas。", "公司位于得克萨斯州。"),
])
def test_r2_unknown_city_with_state_is_kept_whole(before, after):
    text, _ = placenames.normalize_place_text(before, protected_names=["Re:Build"])
    assert text == after
    assert placenames.normalize_place_text(text, protected_names=["Re:Build"])[0] == after
