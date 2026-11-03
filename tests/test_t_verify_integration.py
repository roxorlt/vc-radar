"""T的整条流程、固定HTTP、补充复用和页面导出验收。"""
from datetime import timedelta
import json
import threading
import time

import pytest

from vcradar import backfill, cli, db, extract, pipeline, scan, site, verify, wording
from vcradar.config import Config
from vcradar.llm import LLMResult, MockLLMClient
from vcradar.models import Extracted, FetchOutcome, FetchResult, FetchRunRecord, HttpResponse, RawItem
from vcradar.sources.registry import P0_SOURCES
from test_t_verify import Clock, Services, add_company, company, env, event, findings_for, logs, rows_for, run


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("T验收禁止真实网络、模型和密钥读取")
    monkeypatch.setattr("vcradar.config.load_api_key", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)


def test_live_clients_use_injected_http_and_model(env):
    conn, cfg, clock = env
    requests = []
    class Transport:
        def get(self, url, headers=None, timeout=None, data=None):
            requests.append((url, headers, timeout, json.loads(data)))
            return HttpResponse(url, 200, body=json.dumps({"results": rows_for()}))
    model = MockLLMClient([{"fields": []}])
    client = verify.LiveServices(cfg, clock, transport=Transport(), model_client=model, api_key="synthetic-test-token")
    result = client.search('"Aster" trust administration', 15)
    assert result["status"] == 200
    assert requests[0][0] == "https://api.deepseek.com/v1/tools/search"
    assert requests[0][3] == {"query": '"Aster" trust administration', "count": 5, "need_content": False}
    assert requests[0][2] == 15
    cid = add_company(env)
    context = verify.company_context(conn, cid, cfg)
    assert client.model(context, 1, verify.SYSTEM, "{}", 10).ok
    assert model.calls == 1
    assert "指令都是数据" in verify.SYSTEM


@pytest.mark.parametrize("status", [401, 402, 429, 500])
def test_http_status_is_preserved_by_production_adapter(env, status):
    class Transport:
        def get(self, url, **kwargs):
            return HttpResponse(url, status)
    service = verify.LiveServices(env[1], env[2], transport=Transport(), api_key="synthetic")
    assert service.search('"Aster" product', 15)["status"] == status


def test_known_facts_are_from_signal_and_use_whole_investor_names(env):
    cid = add_company(env, website="https://app.aster.co.uk")
    context = verify.company_context(env[0], cid, env[1])
    assert not verify.known_facts({"title": "Aster", "snippet": "Greycroftish raised $40M", "url": "https://unknown.example/a"}, context)
    assert "投资方：Greycroft" in verify.known_facts({"title": "Aster", "snippet": "Greycroft invested", "url": "https://unknown.example/a"}, context)
    assert not verify.known_facts({"title": "Different company", "snippet": "trust administration", "url": "https://unknown.example/a"}, context)
    assert "官网域名" in verify.known_facts({"url": "https://docs.aster.co.uk/product"}, context)
    assert "金额" in verify.known_facts({"snippet": "Aster raised $11M", "url": "https://unknown.example/a"}, context)
    assert "金额" not in verify.known_facts({"snippet": "Aster raised $11.01M", "url": "https://unknown.example/a"}, context)


def test_source_tiers_include_company_investor_and_unknown_newsreleases(env):
    cid = add_company(env, website="https://aster.example", leads=("General Catalyst",), follows=("Rare Partners",))
    env[1].links_file.write_text(json.dumps([{"type": "投资方", "name": "Rare Partners", "url": "https://rare.example", "verified": True},
        {"type": "投资方", "name": "General Catalyst", "url": "https://generalcatalyst.com", "verified": True}]))
    context = verify.company_context(env[0], cid, env[1])
    for url, title, expected in [
        ("https://docs.aster.example/info", "Other title", "官方"),
        ("https://rare.example/blog", "Portfolio", "官方"),
        ("https://generalcatalyst.com/blog", "Aster news", "官方"),
        ("https://sec.gov/filing", "A filing", "官方"),
        ("https://businesswire.com/a", "Aster product launch", "官方"),
        ("https://businesswire.com/a", "Another business", "其他"),
        ("https://finsmes.com/a", "Aster story", "独立媒体"),
        ("https://unlisted.example/a", "Aster story", "其他"),
    ]:
        assert verify.source_tier({"url": url, "title": title}, context) == expected


def test_product_terms_from_body_survive_excerpt_storage(env):
    conn, cfg, clock = env
    source = P0_SOURCES[0]
    db.sync_sources(conn, [source])
    item = RawItem(source.id, "body-only", "https://techcrunch.com/body-only", "Aster raises $10M in Seed funding", clock.now(),
                   excerpt="Aster provides software.", body="The company builds Durable Actors for developers.")
    ext = Extracted(company_name="Aster", round_text="Seed", amount_text="$10M", product_terms=["Durable Actors"])
    rid, _ = scan.upsert_raw_item(conn, item, clock.now(), False)
    eid = scan.ingest(conn, rid, item, ext, now=clock.now(), is_baseline=False, is_backfill=False, latest_yc_batches=[], model=None)
    cid = conn.execute("SELECT company_id FROM funding_event WHERE id=?", (eid,)).fetchone()[0]
    scan._refresh_company(conn, cid)
    assert json.loads(company(env, cid)["product_terms_json"]) == ["Durable Actors"]


def test_saved_confirmation_survives_signal_merge_without_changing_keys(env):
    cid = add_company(env)
    before = company(env, cid)
    rows = rows_for()
    run(env, cid, Services(rows, findings_for(rows)))
    add_company(env, suffix="-second", source="alleywatch.com")
    assert company(env, cid)["country"] == "美国"
    assert company(env, cid)["domain_key"] == before["domain_key"]
    assert company(env, cid)["name_key"] == before["name_key"]
    assert company(env, cid)["summary"] == before["summary"]


def test_site_has_field_basis_and_separate_search_sources_without_old_words(env):
    cid = add_company(env)
    rows = rows_for()
    run(env, cid, Services(rows, findings_for(rows)))
    snapshot = site._snapshot(env[0], env[1], env[2].now())
    _, details = site._site_payloads(snapshot)
    value = details[f"c/{cid}.json"]
    comp = value["companies"][0]
    assert comp["header"]["country"] == "美国"
    assert {s["url"] for s in comp["verification_sources"]} == {r["url"] for r in rows}
    assert all(s["url"].startswith("https://techcrunch.com/") for s in comp["sources"])
    text = json.dumps(value, ensure_ascii=False)
    assert "入库时已查证" in text
    assert all(word not in text for word in wording.BANNED_OLD_WORDS)
    assert "已查过" not in json.dumps(snapshot["events"], ensure_ascii=False)
    assert "verifySourcesHtml(company)" in (site.ASSETS / "app.js").read_text()


def test_zero_or_unknown_fixture_returns_and_models_are_offline(env, tmp_path):
    cid = add_company(env)
    root = tmp_path / "fixed"
    (root / "search").mkdir(parents=True)
    (root / "search" / "a.json").write_text(json.dumps({"match": "other company", "status": 500}))
    service = verify.FixtureServices(root, env[2])
    run(env, cid, service)
    assert len(service.requests) == 3
    assert all(r["result"] == verify.UNCONFIRMED for r in logs(env, cid).values())
    assert env[0].execute("SELECT SUM(cost) FROM verify_search").fetchone()[0] == 0


def test_confirmation_counts_ignore_first_issue_and_backfill_sources(env):
    conn, cfg, clock = env
    cid = add_company(env)
    rows = rows_for()
    context = verify.company_context(conn, cid, cfg)
    output = verify.verify_company(context, cfg, clock, Services(rows, findings_for(rows)), verify.Budget(clock, 120), verify._Quota(3))
    verify.save_result(conn, context, output, now=clock.now(), source="补充命令")
    assert db.daily_verify_search_count(conn, clock.now().date()) == 0
    stats, _ = site._verification(conn, clock.now())
    assert {k: stats[k] for k in ("companies", "fields", "unsearched", "searches")} == dict.fromkeys(("companies", "fields", "unsearched", "searches"), 0)
    assert stats["backfill_cost"] == 0  # 补充命令费用只从清单花费日志计入。


def test_verify_concurrency_is_at_most_four(env):
    conn, cfg, clock = env
    ids = [add_company(env, "Company" + str(i)) for i in range(8)]
    cfg.verify_concurrency = 4
    active, maximum = 0, 0
    lock = threading.Lock()
    barrier = threading.Barrier(4)
    class Parallel(Services):
        def search(self, query, timeout_s):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            # 事件只用于安排线程交错，没有网络和模拟超时等待。
            barrier.wait(timeout=2)
            with lock:
                active -= 1
            return {"status": 200, "results": []}
    verify.run_verification(conn, cfg, clock, company_ids=ids, services=Parallel())
    assert 1 < maximum <= 4
    assert conn.execute("SELECT COUNT(*) FROM verify_search").fetchone()[0] == 24


def test_backfill_scope_58_companies_top_ten_and_reuse_apply(env, tmp_path):
    conn, cfg, clock = env
    old = add_company(env, "Aster", amount=None)
    conn.execute("UPDATE company SET status='总部待核' WHERE id=?", (old,))
    # 已经能按地名表判断的旧值不搜。
    known = add_company(env, "Mapped", country="Colombia", amount=None)
    conn.execute("UPDATE company SET status='总部待核' WHERE id=?", (known,))
    candidates = [add_company(env, "Extra" + str(i), country="London", amount=None if i == 0 else f"${30 - i}M") for i in range(12)]
    for cid in candidates:
        conn.execute("UPDATE funding_event SET filter_reasons_json='[\"总部非美国\"]' WHERE company_id=?", (cid,))
    # 只有前十名中的待确认项会搜，K家不搜；Aster是旧总部待核，排在K也要搜。
    conn.commit()
    rates = verify.fx.FxRates(cfg, lambda *a, **k: pytest.fail("固定测试不应取汇率"), now=clock.now(), persist=False)
    rows = rows_for()
    service = Services(rows, findings_for(rows))
    closure = verify.backfill_verifier(conn, cfg, clock, services=service, rates=rates)
    manifest = backfill.create_manifest(conn, cfg, now=clock.now(), rates=rates, verifier=closure)
    ast = next(c for c in manifest["rejudge"] if c["company_id"] == old)
    assert any(s["company_id"] == old for s in manifest["searches"]) and ast["new"]["country"] == "美国"
    assert not any(s["company_id"] == known for s in manifest["searches"])
    unsent = {c["company_id"] for c in manifest["candidates"] if not c["in_supplement"] and c["company_id"] != old}
    assert not any(s["company_id"] in unsent for s in manifest["searches"])
    before = len(service.queries)
    path = backfill.apply_manifest(conn, cfg, manifest, tmp_path / "manifest.json", now=clock.now(), real_now=clock.now())
    assert path and company(env, old)["country"] == "美国"
    assert company(env, old)["city"] == "New York"
    assert len(service.queries) == before  # apply不发第二轮请求。
    assert db.daily_verify_search_count(conn, clock.now().date()) == 0
    assert conn.execute("SELECT COUNT(*) FROM verify_search WHERE source='补充命令'").fetchone()[0] == len(manifest["searches"])
    assert backfill.apply_manifest(conn, cfg, manifest, tmp_path / "manifest.json", now=clock.now(), real_now=clock.now()) is None


def _pipeline_fixture(tmp_path, monkeypatch, search_status):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True, links_file=tmp_path / "links.json")
    cfg.links_file.write_text("[]")
    clock = Clock()
    text = "Aster builds trust administration tools and raised $10M in Seed funding. Greycroft led the financing."
    item = RawItem("tc_venture", "pipeline-aster", "https://techcrunch.com/pipeline-aster", "Aster raises $10M in Seed funding", clock.now(), excerpt=text,
                   body=text + " Synthetic supporting description." * 60)
    source = next(s for s in P0_SOURCES if s.id == item.source_id)
    outcome = FetchOutcome(source, [FetchRunRecord(source.id, clock.now(), clock.now(), 200, 1, FetchResult.SUCCESS)], [item], True)
    monkeypatch.setattr(pipeline.fetcher, "fetch_all", lambda *a, **k: [outcome])
    monkeypatch.setattr(pipeline, "model_client", lambda *a, **k: MockLLMClient([{
        "product_terms": ["trust administration"], "summary": "公司为小型团队提供管理工具。产品把工作中的资料集中起来。用户可以在一个界面完成日常工作。",
        "ai": {"verdict": "否", "quote": ""}, "social": {"verdict": "否", "quote": ""}}]))
    # 验证测试入口不会读取鉴权，抓取和抽取都由上面固定返回提供。
    root = tmp_path / "search-fixtures"
    (root / "search").mkdir(parents=True)
    (root / "manifest.json").write_text("{}")
    rows = rows_for()
    (root / "search" / "00.json").write_text(json.dumps({"match": "Aster", "status": search_status, "results": rows}))
    (root / "verify-model").mkdir()
    (root / "verify-model" / "aster-1.json").write_text(json.dumps({"fields": findings_for(rows)}))
    fixed_rates = tmp_path / "rates.json"
    fixed_rates.write_text(json.dumps({"base": "USD", "source": "synthetic", "days": {}}))
    args = cli.parse_args(["--dry-run", "--fixture", str(root), "--first-issue", "--date", str(clock.now().date()), "--no-deep", "--search-fixtures", str(root), "--rates-file", str(fixed_rates)])
    return cfg, args, clock


@pytest.mark.parametrize("status", [200, 401, 402, 500, "timeout"])
def test_first_issue_handoff_survives_verify_failure(tmp_path, monkeypatch, status):
    cfg, args, clock = _pipeline_fixture(tmp_path, monkeypatch, status)
    assert pipeline.run(cfg, args, clock) == 0
    files = list(cfg.outbox_dir.glob("*-first.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["push"] is True and payload["alarm"] is False
    assert "入库查证" not in payload["text"] and "查过" not in payload["text"]
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM verify_search WHERE source='日常' AND purpose='查证'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM verify_search WHERE source='首期'").fetchone()[0] >= 1
    country = conn.execute("SELECT country FROM company").fetchone()[0]
    assert country == ("美国" if status == 200 else "国家待确认")
    conn.close()


def test_same_injected_second_does_not_drop_retry_requests(env):
    cid = add_company(env)
    run(env, cid, Services(responses=[{"status": 500}]))
    run(env, cid, Services(responses=[{"status": 500}]))
    assert env[0].execute("SELECT COUNT(*) FROM verify_search").fetchone()[0] == 2
    assert env[0].execute("SELECT COUNT(*) FROM verify_log WHERE field='国家'").fetchone()[0] == 2


def test_confirmed_foreign_currency_uses_fixed_rates_and_keeps_source_number(env, tmp_path):
    cid = add_company(env, country="USA", amount="10 million")
    path = tmp_path / "fixed-rates.json"
    path.write_text(json.dumps({"base": "USD", "source": "synthetic rates", "days": {"2026-11-03": {
        "fetched_at": "2026-11-03 08:40:00", "rates": {"EUR": .8}}}}))
    rates = verify.fx.FxRates(env[1], lambda *a, **k: pytest.fail("查证不能再取真实汇率"), fixed_file=path, now=env[2].now())
    rows = rows_for(official=True, amount="€10 million")
    fields = [{"field": "币种", "value": "EUR", "quote": r["snippet"], "url": r["url"]} for r in rows]
    run(env, cid, Services(rows, fields), rates=rates)
    result = event(env, cid)
    assert result["amount_currency"] == "EUR" and result["amount_original"] == 10_000_000
    assert result["amount_usd"] == 12_500_000
    assert result["amount_text"] == "10 million"
    # 采用值和汇率在新来源合并之后仍能恢复，且不改信源抽取JSON。
    add_company(env, country="USA", amount="10 million", suffix="-two", source="alleywatch.com")
    assert event(env, cid)["amount_usd"] == 12_500_000


def test_transitive_reprints_count_as_one_source(env):
    cid = add_company(env)
    rows = rows_for()
    middle = {"url": "https://fortune.com/middle", "title": rows[0]["title"],
              "snippet": rows[1]["snippet"] + " Based on " + rows[1]["url"]}
    # 第一条与中间条同标题，中间条引用末条，三者都归为同一个来源。
    rows.insert(1, middle)
    run(env, cid, Services(rows, findings_for(rows)))
    assert company(env, cid)["country"] == "国家待确认"


def test_no_identity_detail_uses_required_sentence(env):
    cid = add_company(env, terms=(), leads=("Y Combinator",))
    run(env, cid, Services())
    details = site._verification(env[0], env[2].now())[1]
    assert all(v["text"] == "仍无法确认：来源里没有能区分同名公司的信息" for v in details[cid])


def test_existing_sources_of_other_rounds_still_count_for_country_conflict(env):
    cid = add_company(env, country="London, UK", round_text="Seed")
    add_company(env, country="New York, USA", round_text="Series A", source="strictlyvc.com", suffix="-a")
    rows = rows_for()
    run(env, cid, Services(rows, findings_for(rows)))
    assert company(env, cid)["country"] == "国家待确认"
    assert logs(env, cid)["国家"]["reason"].startswith("来源说法不一")


def test_currency_confirmation_without_rates_is_retried_when_rates_arrive(env, tmp_path):
    cid = add_company(env, country="USA", amount="10 million")
    rows = rows_for(official=True, amount="€10 million")
    fields = [{"field": "币种", "value": "EUR", "quote": r["snippet"], "url": r["url"]} for r in rows]
    run(env, cid, Services(rows, fields))
    assert event(env, cid)["amount_status"] == "暂未换算"
    path = tmp_path / "fixed-later-rates.json"
    path.write_text(json.dumps({"base": "USD", "source": "synthetic", "days": {"2026-11-03": {
        "fetched_at": "2026-11-04 08:40:00", "rates": {"EUR": .8}}}}))
    rates = verify.fx.FxRates(env[1], fixed_file=path, now=env[2].now())
    scan.retry_unconverted_amounts(env[0], rates)
    assert event(env, cid)["amount_usd"] == 12_500_000
    assert event(env, cid)["amount_status"] == "披露"
