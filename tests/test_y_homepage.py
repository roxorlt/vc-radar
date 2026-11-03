"""Y：#41 各项只用手写 HTML、固定搜索、模拟钟和 tmp_path。"""
from datetime import timedelta
from hashlib import sha1, sha256
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from vcradar import backfill, cli, db, homepage, pipeline, render, scan, site, verify, wording
from vcradar.config import Config, effective_links_file
from vcradar.models import DigestData, HttpResponse
from test_t_verify import Clock
from test_v_backfill import NOW, add_event, apply, env, manifest, offline
from test_w_contracts import evidence


def page(root, url, body="", **meta):
    path = root / "verify-pages" / sha1(homepage.fixture_url(url).encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".html").write_text(body, encoding="utf-8")
    if meta:
        path.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")


def searches(root, name, results, **kwargs):
    folder = root / "search"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (name + ".json")).write_text(json.dumps({"match": '"' + name + '"', "results": results, **kwargs}), encoding="utf-8")


def add(env, name, *, terms=("oscillation", "telemetry"), leads=("Orchid Ventures",),
        website=None, source="tc_venture", **kwargs):
    conn, _ = env
    eid = add_event(conn, name, filtered=kwargs.pop("filtered", ()), **kwargs)
    cid = conn.execute("SELECT company_id FROM funding_event WHERE id=?", (eid,)).fetchone()[0]
    record = conn.execute("SELECT * FROM event_source WHERE event_id=? ORDER BY raw_item_id DESC", (eid,)).fetchone()
    ext = json.loads(record["extraction_json"])
    ext["product_terms"] = list(terms)
    ext["website"] = website
    ext["quotes"] = {"amount_text": f"{name} raised $12 million in seed funding."}
    raw_text = f'{name} raised $12 million in seed funding. ' + " ".join(terms) + "."
    conn.execute("UPDATE company SET product_terms_json=?,one_liner=? WHERE id=?", (json.dumps(list(terms)), " ".join(terms), cid))
    conn.execute("UPDATE funding_event SET leads_json=? WHERE id=?", (json.dumps(list(leads)), eid))
    conn.execute("UPDATE event_source SET extraction_json=?,source_name=? WHERE event_id=? AND raw_item_id=?",
                 (json.dumps(ext), "YC 公司目录" if source == "yc_directory" else "Crunchbase News" if source.startswith("cbn_") else "TechCrunch", eid, record["raw_item_id"]))
    structured = {"website": website, "company_name": name} if website else {}
    conn.execute("UPDATE raw_item SET source_id=?,excerpt=?,structured_json=? WHERE id=?",
                 (source, raw_text, json.dumps(structured), record["raw_item_id"]))
    return cid, eid, record["url"]


def run(env, root, ids, *, clock=None, source_bodies=None, policy=None):
    conn, cfg = env
    if policy:
        spec = {**homepage.load_policy(cfg), **policy}
        cfg.homepage_check_file = root / "homepage-check.json"
        cfg.homepage_check_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.homepage_check_file.write_text(json.dumps(spec))
    clock = clock or Clock("2026-10-31")
    services = homepage.FixtureServices(root, clock)
    output = homepage.run_homepage(conn, cfg, clock, company_ids=ids, services=services, source_bodies=source_bodies)
    conn.commit()
    return output, services, clock


def good(name, *, strong=True):
    return f"<html><title>{name}</title><body><h1>{name}</h1><p>" + ("Orchid Ventures" if strong else "oscillation telemetry") + "</p></body></html>"


def test_41_1_source_field_search_and_strong_evidence(env, tmp_path):
    conn, cfg = env
    a, ae, source_a = add(env, "Aster", amount="$12M")
    b, be, source_b = add(env, "Birch", amount="$12M", leads=(), website="https://birch.test/product", source="yc_directory")
    c, ce, source_c = add(env, "Cobalt", amount="$12M")
    root = tmp_path / "fixtures"
    page(root, source_a, '<article><a href="https://aster.test/product">Aster</a></article>')
    page(root, "https://aster.test/", good("Aster"), final_url="https://www.aster.test/landing?from=test")
    page(root, "https://birch.test/", "<title>Birch</title><main>We raised $12M.</main>")
    page(root, "https://cobalt.test/", good("Cobalt"))
    searches(root, "Cobalt", [{"url": "https://cobalt.test/about"}])
    output, services, clock = run(env, root, [a, b, c])
    records = render.read_link_records(cfg.links_file)
    assert len(records) == 3 and len(services.requests) == 1
    assert [r["verified_by"] for r in records] == ["来源页", "官网核对", "官网核对"]
    assert records[0]["url"] == "https://www.aster.test/"
    assert all(r["type"] == "公司" and r["verified"] and r["checker"] == "脚本" and r["verified_at"] == "2026-10-31 08:40" for r in records)
    assert all(r["result"] == "核到" and not r["weak"] for r in output.values())
    assert {r[0] for r in conn.execute("SELECT evidence_strength FROM homepage_check")} == {"强"}
    assert conn.execute("SELECT purpose,cost,model_calls FROM verify_search").fetchone()[:] == ("官网", .005, 0)
    assert db.daily_verify_search_count(conn, clock.now().date()) == 0


def test_41_2_c2_pages_limit_and_no_extra_search(env, tmp_path):
    root = tmp_path / "fixtures"
    cid, _, _ = add(env, "Cedar")
    urls = [f"https://finsmes.com/story-{i}" for i in range(4)]
    searches(root, "Cedar", [{"url": u, "title": "Cedar financing"} for u in urls])
    page(root, urls[0], status=403)
    page(root, urls[1], '<article><a href="https://cedar.test/">Cedar</a></article>')
    page(root, "https://cedar.test/", good("Cedar"))
    output, services, _ = run(env, root, [cid])
    assert output[cid]["candidate_from"] == "搜索结果报道页" and output[cid]["result"] == "核到"
    assert len(services.requests) == 1 and urls[2] not in services.pages
    other, _, _ = add(env, "Maple")
    searches(root, "Maple", [{"url": u, "title": "Maple financing"} for u in urls])
    page(root, urls[1], "<article>No company link.</article>")
    page(root, urls[2], "<article>No company link.</article>")
    page(root, urls[3], '<article><a href="https://maple.test/">Maple</a></article>')
    result, services, _ = run(env, root, [other])
    assert result[other]["result"] == "未核到" and urls[3] not in services.pages
    assert all(services.pages.count(u) == 1 for u in urls[:3])


@pytest.mark.parametrize("content,expected,reason", [("oscillation telemetry", "核到", ""), ("oscillation", "未核到", "只命中 1 个关键词")])
def test_41_3_two_different_keywords_and_weak_marker(env, tmp_path, content, expected, reason):
    cid, _, source = add(env, "Willow", leads=())
    root = tmp_path / "fixtures"
    page(root, source, '<article><a href="https://willow.test/">Willow</a></article>')
    page(root, "https://willow.test/", "<title>Willow</title><main>" + content + "</main>")
    output, _, _ = run(env, root, [cid])
    assert output[cid]["result"] == expected and output[cid]["reason"] == reason
    if expected == "核到":
        assert render.read_link_records(env[1].links_file)[0]["verified_by"] == "来源页（弱证据）"


@pytest.mark.parametrize("variant,reason", [("name", "只对上公司名"), ("parked", "停放页或出售页"),
    ("redirect", "排除名单域名"), ("js", "公司名没对上"), ("one", "只命中 1 个关键词")])
def test_41_4_rejects_without_search_fallback(env, tmp_path, variant, reason):
    cid, _, source = add(env, "FinMaq", leads=())
    root = tmp_path / "fixtures"
    page(root, source, '<article><a href="https://finmaq.test/">FinMaq</a></article>')
    html = {"name": "<title>FinMaq</title>", "parked": "<title>FinMaq</title>domain is for sale oscillation telemetry",
            "redirect": good("FinMaq"), "js": "<script>FinMaq oscillation telemetry</script>",
            "one": "<title>FinMaq</title>oscillation"}[variant]
    page(root, "https://finmaq.test/", html, **({"final_url": "https://crunchbase.com/organization/finmaq"} if variant == "redirect" else {}))
    output, services, _ = run(env, root, [cid])
    assert output[cid]["result"] == "未核到" and output[cid]["reason"] == reason
    assert services.requests == [] and render.read_link_records(env[1].links_file) == []


def test_41_4_no_identity_only_yc_does_not_search(env, tmp_path):
    cid, _, _ = add(env, "Hazel", terms=(), leads=("YC", "General Catalyst"))
    result, services, _ = run(env, tmp_path / "fixtures", [cid])
    assert result[cid]["reason"] == verify.NO_IDENTITY and not services.requests


@pytest.mark.parametrize("failure", [{"status": 403}, {"status": 429}, {"delay_s": 16}, {"robots": False}])
def test_41_5_unverified_reopens_same_url_next_day(env, tmp_path, failure):
    cid, _, source = add(env, "Iris")
    root = tmp_path / "fixtures"
    page(root, source, '<article><a href="https://iris.test/">Iris</a></article>')
    page(root, "https://iris.test/", good("Iris"), **failure)
    output, services, clock = run(env, root, [cid])
    assert output[cid]["result"] == "未核实" and not services.requests
    page(root, "https://iris.test/", good("Iris"), status=200, delay_s=0)
    clock.value = clock.value.replace(day=2, month=11)
    result, services, _ = run(env, root, [], clock=clock)
    assert result[cid]["result"] == "核到" and services.pages == ["https://iris.test/"]
    assert not services.requests and len(render.read_link_records(env[1].links_file)) == 1


def test_41_5_seven_day_stop_and_new_source_only_for_not_found(env, tmp_path):
    conn, _ = env
    cid, _, source = add(env, "Juniper")
    root = tmp_path / "fixtures"
    page(root, source, '<article><a href="https://juniper.test/">Juniper</a></article>')
    page(root, "https://juniper.test/", status=403)
    for day in range(7):
        clock = Clock("2026-10-31"); clock.advance(day * 86400)
        output, services, _ = run(env, root, [cid] if day == 0 else [], clock=clock)
        assert output[cid]["result"] == "未核实" and "https://juniper.test/" in services.pages
    clock = Clock("2026-11-07")
    output, services, _ = run(env, root, [], clock=clock)
    assert output[cid]["result"] == "停止重核" and not services.pages
    assert conn.execute("SELECT COUNT(*) FROM homepage_check WHERE company_id=?", (cid,)).fetchone()[0] == 8
    other, eid, source2 = add(env, "Linden")
    page(root, source2, '<article><a href="https://linden.test/">Linden</a></article>')
    page(root, "https://linden.test/", "<title>Linden</title>")
    run(env, root, [other])
    output, services, _ = run(env, root, [other])
    assert other not in output and not services.pages
    add(env, "Linden")
    page(root, "https://linden.test/", good("Linden"))
    output, _, _ = run(env, root, [other])
    assert output[other]["result"] == "核到"


def test_41_6_manual_records_order_and_duplicate_suppression(env, tmp_path):
    _, cfg = env
    a, _, _ = add(env, "Laurel")
    b, _, _ = add(env, "Myrtle")
    raw = '[\n {"type":"公司","company":" Laurel ","verified":true,"url":"https://human.test/"},\n {"type":"公司","company":"Myrtle","verified":false,"url":""}\n]'
    cfg.links_file.write_text(raw)
    output, services, _ = run(env, tmp_path / "fixtures", [a, b])
    assert output == {} and not services.pages and cfg.links_file.read_text() == raw
    check = {"result": "核到", "candidate_from": "来源页", "final_url": "https://new.test/", "checked_at": "2026-10-31 08:42:01", "weak": False}
    assert not homepage.append_verified(cfg, "Laurel", check)
    assert not homepage.append_verified(cfg, "Myrtle", check)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(lambda _: homepage.append_verified(cfg, "Nettle", check), range(2))) == 1
    assert cfg.links_file.read_text().startswith(raw[:raw.rfind("]")])
    assert len(render.read_link_records(cfg.links_file)) == 3
    assert render.official_links(cfg.links_file)["laurel"] == "https://human.test/"


def test_41_7_daily_quota_survives_same_day_rerun(env, tmp_path):
    conn, _ = env
    root = tmp_path / "fixtures"
    ids = [add(env, name)[0] for name in ("Oak", "Pine", "Elm")]
    for name in ("Oak", "Pine", "Elm"):
        url = "https://" + name.lower() + ".test/"
        searches(root, name, [{"url": url}]); page(root, url, good(name))
    output, services, clock = run(env, root, ids, policy={"daily_search_limit": 2})
    assert len(services.requests) == 2 and output[ids[2]]["reason"] == "超当天上限"
    assert not output[ids[2]]["searched"]
    again, services, _ = run(env, root, [], clock=clock)
    assert not services.requests and again[ids[2]]["reason"] == "超当天上限"
    clock.advance(86400)
    again, services, _ = run(env, root, [], clock=clock)
    assert len(services.requests) == 1 and again[ids[2]]["result"] == "核到"
    assert db.daily_verify_search_count(conn, clock.now().date()) == 0


def test_41_7_company_90_seconds_and_step_deadline(env, tmp_path):
    root = tmp_path / "fixtures"
    slow, _, source = add(env, "Tardy")
    fast, _, fast_source = add(env, "Swift")
    page(root, source, "<article>No link</article>", delay_s=14)
    results = [{"url": "https://tardy.one/result", "title": "Tardy report"}, {"url": "https://tardy.two/result", "title": "Tardy report"}]
    searches(root, "Tardy", results, delay_s=14)
    page(root, "https://tardy.one/", "<title>Tardy</title>", delay_s=14)
    page(root, "https://tardy.two/", "<title>Tardy</title>", delay_s=14)
    page(root, results[0]["url"], '<article><a href="https://tardy.three/">Tardy</a></article>', delay_s=14)
    page(root, "https://tardy.three/", "<title>Tardy</title>", delay_s=14)
    page(root, results[1]["url"], '<article><a href="https://tardy.four/">Tardy</a></article>', delay_s=14)
    page(root, "https://tardy.four/", good("Tardy"), delay_s=14)
    page(root, fast_source, '<article><a href="https://swift.test/">Swift</a></article>')
    page(root, "https://swift.test/", good("Swift"))
    output, services, clock = run(env, root, [slow, fast])
    assert output[slow]["reason"] == "单家到点" and output[fast]["result"] == "核到"
    assert 89.9 <= (clock.now() - Clock("2026-10-31").now()).total_seconds() <= 90.1
    clock.advance(86400)
    retried, retry_services, _ = run(env, root, [], clock=clock)
    assert retried[slow]["result"] == "核到" and not retry_services.requests
    later, _, later_source = add(env, "Later")
    page(root, later_source, "<article>No link</article>", delay_s=14)
    output, _, _ = run(env, root, [later], clock=clock, policy={"step_timeout": .1})
    assert output[later]["reason"] == "整步到点"


def test_41_7_step_deadline_preserves_prior_candidate_for_next_day(env, tmp_path):
    conn, _ = env
    root = tmp_path / "fixtures"
    cid, _, source = add(env, "Retained")
    page(root, source, '<article><a href="https://retained.test/">Retained</a></article>')
    page(root, "https://retained.test/", status=403)
    _, _, clock = run(env, root, [cid])
    clock.advance(86400)
    output, services, _ = run(env, root, [], clock=clock, policy={"step_timeout": 0})
    assert output[cid]["reason"] == "整步到点" and output[cid]["candidate_url"] == "https://retained.test/"
    assert not services.pages
    clock.advance(86400)
    page(root, "https://retained.test/", good("Retained"), status=200)
    output, services, _ = run(env, root, [], clock=clock, policy={"step_timeout": 120})
    assert output[cid]["result"] == "核到" and services.pages == ["https://retained.test/"]
    assert conn.execute("SELECT COUNT(*) FROM verify_search").fetchone()[0] == 0


def test_41_8_status_snapshot_page_links_wording_and_research_unchanged(env, tmp_path):
    from test_site_build import render_page
    conn, cfg = env
    research = Config().links_file
    before = sha256((research.read_bytes() if research.exists() else b"[]")).hexdigest()
    root = tmp_path / "fixtures"
    a, ae, source = add(env, "Ash", leads=())
    b, be, source2 = add(env, "Beech")
    page(root, source, '<article><a href="https://ash.test/">Ash</a></article>')
    page(root, "https://ash.test/", good("Ash", strong=False))
    searches(root, "Beech", [{"url": "https://beech.test/"}])
    page(root, "https://beech.test/", "<title>Beech</title>")
    _, _, clock = run(env, root, [a, b])
    listing = scan.collect_listing(conn, clock.now().date(), weekly=True, event_ids=[ae, be])
    scan.upsert_digest(conn, {"date": clock.now().date(), "push": True, "event_ids": [ae, be], "generated_at": clock.now()})
    homepage.record_digest_snapshot(conn, cfg, clock.now().date(), listing["cards"], clock.now())
    conn.execute("INSERT INTO verify_search(company_id,run_date,requested_at,query,cost,model_cost,purpose) VALUES (?,'2026-10-31','2026-10-31 08:40:00','ordinary',.01,.0035,'查证')", (a,))
    conn.commit()
    path = site.build_site(conn, cfg, now=clock.now(), out_dir=tmp_path / "site")
    status_html = render_page(path, "sourcesHtml")
    detail_a = render_page(path, "detailHtml", a)
    detail_b = render_page(path, "detailHtml", b)
    data = DigestData(clock.now().date(), None, clock.now(), "有新增", cards=listing["cards"])
    text = render.render_push(data, links_file=cfg.links_file)
    assert "本期核到 1 / 2 家" in status_html and 'class="alert"' in status_html
    assert "官网未核到 1 家：Beech" in status_html and "今天 1 次 · 本月 ¥0.0050" in status_html
    assert 'href="https://ash.test/"' in detail_a and ">官网</a>" in detail_a
    assert ">官网</a>" not in detail_b and "[官网](https://ash.test/)" in text
    for word in ("脚本", "自动", "核对", "弱证据", "官网未知"):
        assert word in wording.BANNED_OLD_WORDS
        assert word not in status_html + detail_a + detail_b + text
    snap = site._snapshot(conn, cfg, clock.now())
    assert snap["status"]["verification"]["month_used"] == .0135
    assert snap["status"]["verification"]["searches"] == 1
    assert snap["status"]["homepage"]["month_used"] == .005
    homepage.append_verified(cfg, "Beech", {"result": "核到", "final_url": "https://beech.test/", "checked_at": "2026-11-02 08:40:00"})
    status = site._snapshot(conn, cfg, clock.now())["status"]["homepage"]
    assert status["found"] == 1 and status["total"] == 2 and not status["missing"]
    assert sha256((research.read_bytes() if research.exists() else b"[]")).hexdigest() == before


def test_41_9_atomic_append_failure_leaves_parseable_original(env):
    _, cfg = env
    raw = b'[\n {"type":"company","name":"old record","note":"keep  spaces"}\n]\n'
    cfg.links_file.write_bytes(raw)
    check = {"result": "核到", "candidate_from": "搜索结果", "final_url": "https://verified.test/", "checked_at": "2026-10-31 08:40:12"}
    def crash(_):
        raise SystemExit("synthetic termination")
    with pytest.raises(SystemExit):
        homepage.append_verified(cfg, "Verified", check, before_replace=crash)
    assert cfg.links_file.read_bytes() == raw and json.loads(raw)
    assert homepage.append_verified(cfg, "Verified", check)
    assert cfg.links_file.read_bytes().startswith(raw[:raw.rfind(b"]")])
    assert len(json.loads(cfg.links_file.read_text())) == 2


def test_41_10_default_test_links_reset_and_fixture_hash_are_isolated(tmp_path):
    cfg = Config(state_dir=tmp_path, test_mode=True)
    research = Config().links_file
    before = sha256((research.read_bytes() if research.exists() else b"[]")).hexdigest()
    pipeline.reset_test_db(cfg)
    assert effective_links_file(cfg).read_bytes() == (research.read_bytes() if research.exists() else b"[]")
    check = {"result": "核到", "final_url": "https://synthetic.test/", "checked_at": "2026-10-31 08:40:12"}
    assert homepage.append_verified(cfg, "Synthetic Offline Company", check)
    assert sha256((research.read_bytes() if research.exists() else b"[]")).hexdigest() == before
    root = tmp_path / "fixtures"
    url = "https://MIXED.test/path?utm_source=exact&v=a%20b#fragment"
    page(root, url, "<title>Exact</title>")
    services = homepage.FixtureServices(root, Clock())
    assert services.page("  " + url + "  ", 15)["body"] == "<title>Exact</title>"


def test_41_11_backfill_freezes_homepage_and_apply_never_fetches(env, tmp_path, monkeypatch):
    conn, cfg = env
    root = tmp_path / "fixtures"
    names = ["Aster", "Birch", "Cedar", "Dogwood", "Elm"] + [f"Filler {i}" for i in range(7)]
    ids = [add(env, name, amount=f"${30-i}M", filtered=("总部非美国",))[0] for i, name in enumerate(names)]
    sources = {r["company_id"]: r["url"] for r in conn.execute("SELECT e.company_id,s.url FROM event_source s JOIN funding_event e ON e.id=s.event_id")}
    page(root, sources[ids[0]], '<article><a href="https://aster.test/">Aster</a></article>')
    page(root, "https://aster.test/", good("Aster"))
    searches(root, "Birch", [{"url": "https://birch.test/"}]); page(root, "https://birch.test/", good("Birch"))
    searches(root, "Cedar", [{"url": "https://finsmes.com/cedar", "title": "Cedar financing"}])
    page(root, "https://finsmes.com/cedar", '<article><a href="https://cedar.test/">Cedar</a></article>')
    page(root, "https://cedar.test/", good("Cedar", strong=False))
    page(root, sources[ids[4]], '<article><a href="https://elm.test/">Elm</a></article>')
    page(root, "https://elm.test/", status=403)
    cfg.links_file.write_text(json.dumps([{"company": name, "type": "公司", "verified": False, "url": ""} for name in ["Dogwood", *names[5:10]]]))
    conn.commit()
    clock = Clock("2026-10-31"); services = homepage.FixtureServices(root, clock)
    checker = homepage.backfill_checker(conn, cfg, clock, services=services)
    data = manifest(env, homepage_checker=checker)
    checks = {c["name"]: c["homepage_check"] for c in data["candidates"]}
    assert [checks[name]["result"] for name in names[:3]] == ["核到"] * 3
    assert checks["Dogwood"] is None and checks["Elm"]["result"] == "未核实"
    assert all(checks[name] is None for name in names[10:])
    assert len([s for s in data["searches"] if s["kind"] == "E"]) == 2
    requests, pages = list(services.requests), list(services.pages)
    monkeypatch.setattr(services, "page", lambda *a: pytest.fail("apply 不抓页"))
    monkeypatch.setattr(services, "search", lambda *a: pytest.fail("apply 不搜索"))
    apply(env, data)
    records = render.read_link_records(cfg.links_file)
    assert len(records) == 9 and sum(r.get("checker") == "脚本" for r in records) == 3
    assert next(r for r in records if r.get("company") == "Cedar")["verified_by"] == "官网核对（弱证据）"
    assert requests == services.requests and pages == services.pages
    assert conn.execute("SELECT COUNT(*) FROM verify_search WHERE purpose='官网' AND source='补充命令'").fetchone()[0] == 2


def test_41_11_apply_receipt_recovers_after_link_write_without_duplicate(env, monkeypatch):
    conn, cfg = env
    cid, _, _ = add(env, "Recovery", amount="$12M", filtered=("总部非美国",))
    check = {"result": "核到", "candidate_from": "来源页", "candidate_url": "https://recovery.test/",
             "final_url": "https://recovery.test/", "http": 200, "weak": False,
             "matched_facts": ["投资方：Orchid Ventures"], "checked_at": "2026-10-31 08:40:02", "searches": []}
    data = manifest(env, homepage_checker=lambda candidate: check)
    original = homepage.append_verified
    attempts = []
    def fail_after_append(*args, **kwargs):
        result = original(*args, **kwargs)
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("synthetic termination after replace")
        return result
    monkeypatch.setattr(homepage, "append_verified", fail_after_append)
    with pytest.raises(OSError):
        apply(env, data)
    file, = cfg.outbox_dir.glob("backfill-verify-*.json")
    assert not json.loads(file.read_text()).get("applied_at")
    assert conn.execute("SELECT backfill FROM company WHERE id=?", (cid,)).fetchone()[0] == "补充未推送"
    assert len(render.read_link_records(cfg.links_file)) == 1
    assert conn.execute("SELECT links_written FROM homepage_check").fetchone()[0] == 0
    assert apply(env, data, file=file)[0] is None
    assert json.loads(file.read_text())["applied_at"]
    assert len(render.read_link_records(cfg.links_file)) == 1
    assert conn.execute("SELECT links_written FROM homepage_check").fetchone()[0] == 1
    assert len(list(cfg.outbox_dir.glob("*-backfill.json"))) == 1


def test_v16o_last_non_e_row(env, tmp_path):
    conn, cfg = env
    cid, _, _ = add(env, "Both", country=None, amount="$12M", filtered=("总部非美国",))
    root = tmp_path / "fixtures"
    searches(root, "Both", [{"url": "https://both.test/"}]); page(root, "https://both.test/", good("Both"))
    checker = homepage.backfill_checker(conn, cfg, Clock("2026-10-31"), fixtures=root)
    data = manifest(env, verifier=evidence, homepage_checker=checker)
    rows = [s for s in data["searches"] if s["company_id"] == cid]
    assert rows[-1]["kind"] == "E" and rows[-1]["conclusion"] == {} and rows[-1]["model_calls"] == []
    last = [s for s in rows if s["kind"] != "E"][-1]
    assert last["conclusion"] and last["model_calls"] == [{"cost": .0035}]
    apply(env, data)
    assert conn.execute("SELECT COUNT(*) FROM verify_log WHERE company_id=?", (cid,)).fetchone()[0] == len(last["conclusion"])
    assert conn.execute("SELECT model_calls,model_cost FROM verify_search WHERE purpose='官网'").fetchone()[:] == (0, 0)
    assert conn.execute("SELECT model_calls,model_cost FROM verify_search WHERE purpose='查证'").fetchone()[:] == (1, .0035)


def test_41_migration_keeps_old_searches_and_adds_homepage_table(env):
    conn, cfg = env
    cid, _, _ = add(env, "Old Search")
    conn.execute("INSERT INTO verify_search(company_id,run_date,requested_at,query,cost) VALUES (?,'2026-10-31','2026-10-31 08:40:00','old',.005)", (cid,))
    conn.execute("DROP TABLE homepage_check")
    conn.execute("DROP INDEX verify_purpose_daily")
    conn.execute("ALTER TABLE verify_search DROP COLUMN purpose")
    conn.commit(); conn.close()
    reopened = db.connect(cfg.db_path)
    assert reopened.execute("SELECT purpose,cost FROM verify_search").fetchone()[:] == ("查证", .005)
    columns = {row[1] for row in reopened.execute("PRAGMA table_info(homepage_check)")}
    assert {"company_id", "source", "result", "candidate_from", "candidate_url", "final_url", "http", "reason",
            "matched_facts_json", "evidence_strength", "first_unverified_date", "searched", "checked_at", "links_written"} <= columns
    reopened.close()


def test_41_1_domain_rule_body_order_and_known_facts_are_from_sources(env, tmp_path):
    conn, cfg = env
    cid, eid, source = add(env, "Bites", amount=None, terms=(), leads=("YC", "GV"))
    context = homepage.company_context(conn, cid, cfg)
    # 7.4 后来确认的金额不是信源金额，不能成为官网强证据。
    context["event"].update(amount_original=12e6, amount_currency="USD", amount_status="披露")
    policy = homepage.load_policy(cfg)
    facts = homepage._facts(context, policy, "")
    assert facts["amounts"] == [] and facts["investors"] == []
    assert homepage.domain_matches("Bites", "https://mybites.io/")
    assert homepage.domain_matches("North Vector Dynamics", "https://northvectordy.test/")
    assert not homepage.domain_matches("North Vector Dynamics", "https://northvector.test/")
    assert not homepage.domain_matches("North Vector Dynamics", "https://north.test/")
    assert not homepage.domain_matches("Bites", "https://unrelated.test/", "Bites product")
    assert homepage.domain_matches("Bites", "https://unrelated.test/", " BITES ")
    html = '<nav><a href="https://bites-nav.test/">Bites</a></nav><article><a href="https://bites-first.test/">Bites</a><a href="https://mybites.io/">Bites</a><a href="https://bites-third.test/">Bites</a></article>'
    candidates = homepage._candidates(homepage.page_parts(html, source)["links"], context, policy, "来源页")
    assert [c["url"] for c in candidates] == ["https://bites-first.test/", "https://mybites.io/"]


@pytest.mark.parametrize("html,result", [
    ('<meta property="og:title" content="Aster"><body>Orchid Ventures</body>', "核到"),
    ('<meta property="og:site_name" content="Aster"><body>Orchid Ventures</body>', "核到"),
    ('<title>Asterish</title><body>Orchid Ventures</body>', "未核到"),
    ('<h1>Another company</h1>' + 'x' * 1600 + '<h1>Aster</h1>Orchid Ventures', "未核到"),
    ('<title>Aster</title><body>' + 'x' * 4100 + ' domain is for sale Orchid Ventures</body>', "未核到"),
])
def test_41_3_name_scope_first_h1_and_long_parked_page(env, html, result):
    conn, cfg = env
    cid, _, _ = add(env, "Aster", amount="$12M")
    context = homepage.company_context(conn, cid, cfg)
    policy = homepage.load_policy(cfg)
    facts = homepage._facts(context, policy, "")
    output = homepage._evaluate(context, {"url": "https://aster.test/", "from": "来源页"},
        {"status": 200, "body": html}, facts, policy)
    assert output["result"] == result


def test_41_4_missing_home_page_is_test_skip_and_never_network(env, tmp_path):
    cid, _, source = add(env, "Absent")
    root = tmp_path / "fixtures"
    page(root, source, '<article><a href="https://absent.test/">Absent</a></article>')
    result, services, _ = run(env, root, [cid])
    assert result[cid]["result"] == "未核实" and result[cid]["reason"] == "测试跳过"
    assert not services.requests


def test_41_7_main_flow_logs_separate_verification_and_homepage_budgets(tmp_path, monkeypatch):
    from test_t_verify_integration import _pipeline_fixture
    cfg, args, clock = _pipeline_fixture(tmp_path, monkeypatch, 200)
    root = args.search_fixtures
    page(root, "https://techcrunch.com/pipeline-aster", '<article><a href="https://aster.test/">Aster</a></article>')
    page(root, "https://aster.test/", "<title>Aster</title>We raised $10M.", delay_s=12)
    def verification(conn, cfg, clock, **kwargs):
        clock.advance(119)
        return {}
    monkeypatch.setattr(verify, "run_verification", verification)
    assert pipeline.run(cfg, args, clock) == 0
    records = [json.loads(line) for line in (cfg.log_dir / "2026-11-03.log").read_text().splitlines()]
    stamps = {r["event"]: verify._time(r["at"]) for r in records if r["event"] in {"verification_start", "verification_end", "homepage_start", "homepage_end"}}
    assert (stamps["verification_end"] - stamps["verification_start"]).total_seconds() >= 119
    assert stamps["homepage_start"] >= stamps["verification_end"]
    assert (stamps["homepage_end"] - stamps["homepage_start"]).total_seconds() >= 12
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT result FROM homepage_check").fetchone()[0] == "核到"
    conn.close()
    assert render.official_links(cfg.links_file)["aster"] == "https://aster.test/"


def test_41_5_one_search_lifetime_and_stopped_missing_list(env, tmp_path):
    conn, cfg = env
    root = tmp_path / "fixtures"
    cid, eid, _ = add(env, "Spruce")
    searches(root, "Spruce", [{"url": "https://spruce.test/"}])
    page(root, "https://spruce.test/", "<title>Spruce</title>")
    run(env, root, [cid])
    add(env, "Spruce")
    page(root, "https://spruce.test/", good("Spruce"))
    output, services, clock = run(env, root, [cid])
    assert output[cid]["result"] == "核到" and not services.requests
    assert conn.execute("SELECT COUNT(*) FROM verify_search WHERE purpose='官网'").fetchone()[0] == 1
    stopped, _, _ = add(env, "Stopped")
    homepage.save_check(conn, stopped, {"result": "停止重核", "reason": "HTTP 403", "checked_at": "2026-10-31 08:40:00"}, now=clock.now())
    missing = homepage.status(conn, cfg, clock.now(), None, [])["missing"]
    assert missing == [{"company_id": stopped, "name": "Stopped"}]
    cfg.links_file.write_text(json.dumps([*render.read_link_records(cfg.links_file), {"type": "公司", "company": "Stopped", "verified": False}]))
    assert homepage.status(conn, cfg, clock.now(), None, [])["missing"] == []


@pytest.mark.parametrize("missing", ["verified_by", "verified_at"])
def test_38_script_links_require_audit_fields(env, missing):
    _, cfg = env
    record = {"type": "公司", "company": "Aster", "verified": True, "url": "https://aster.test/",
              "verified_by": "官网核对（弱证据）", "verified_at": "2026-10-31 08:40", "checker": "脚本"}
    record.pop(missing)
    cfg.links_file.write_text(json.dumps([record]))
    assert render.official_links(cfg.links_file) == {}


def test_41_1_reuses_main_source_and_crunchbase_field(env, tmp_path):
    conn, cfg = env
    cid, _, source = add(env, "Source Reuse")
    root = tmp_path / "fixtures"
    page(root, "https://reuse.test/", good("Source Reuse"))
    body = '<article><a href="https://reuse.test/">Source Reuse</a></article>'
    output, services, _ = run(env, root, [cid], source_bodies={source: body})
    assert output[cid]["result"] == "核到" and services.pages == ["https://reuse.test/"]
    other, _, _ = add(env, "Crunch Field", source="cbn_main", website="https://crunchfield.test/path", amount="$12M")
    page(root, "https://crunchfield.test/", good("Crunch Field"))
    output, services, _ = run(env, root, [other])
    assert output[other]["candidate_from"] == "Crunchbase News" and output[other]["result"] == "核到"
    assert not services.requests


def test_41_4_live_page_adapter_uses_ua_robots_and_does_not_retry(env):
    from vcradar.config import UA
    _, cfg = env
    class Transport:
        def __init__(self):
            self.requests = []
        def get(self, url, headers=None, timeout=None, **kwargs):
            self.requests.append((url, headers, timeout))
            if url.endswith("robots.txt"):
                return HttpResponse(url, 200, body="User-agent: *\nDisallow: /\n" if "blocked" in url else "User-agent: *\nAllow: /\n")
            return HttpResponse(url, 403, body="denied")
    transport = Transport()
    services = homepage.LiveServices(cfg, Clock(), transport=transport, search_service=object())
    assert services.page("https://allowed.test/", 15)["status"] == 403
    assert len(transport.requests) == 2
    assert services.page("https://blocked.test/", 15)["error"] == "robots 禁止"
    assert len(transport.requests) == 3
    assert all(headers["User-Agent"] == UA and timeout <= 15 for _, headers, timeout in transport.requests)


def test_41_1_scope_excludes_pending_filtered_baseline_and_folded_yc(env, tmp_path):
    conn, _ = env
    pending, pe, _ = add(env, "Pending Round", round_text=None)
    filtered, fe, _ = add(env, "Filtered", round_text="Series B")
    baseline, be, _ = add(env, "Baseline", baseline=True)
    conn.execute("UPDATE company SET status='轮次待核' WHERE id=?", (pending,))
    conn.execute("UPDATE funding_event SET filter_reasons_json='[\"轮次不在范围\"]' WHERE id=?", (fe,))
    folded = [add(env, f"YC Folded {i}", source="yc_directory", batch="Fall 2026")[0] for i in range(31)]
    output, services, _ = run(env, tmp_path / "fixtures", [pending, filtered, baseline, *folded])
    assert output == {} and not services.pages and not services.requests
