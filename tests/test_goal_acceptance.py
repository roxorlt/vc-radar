"""项目核心目标端到端验收测试与阻断缺陷回归测试（离线，不联网）。"""
from __future__ import annotations

from datetime import date, datetime, time as daytime
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from vcradar import cal, cli, config, db, deep, extract, fetcher, fx, http, llm, pipeline, scan, site, verify
from vcradar.config import BJ, UTC, Config
from vcradar.models import Extracted, HttpResponse, RawItem, SourceState
from vcradar.robots import RobotsChecker
from vcradar.sources import rss, yc_directory
from vcradar.sources.registry import ALL_SOURCES, DISPLAY_ORDER, EXTENDED_SOURCES, P0_SOURCES, display_name, display_rank, name_for_url


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("验收测试必须离线运行，禁止联网或读取真实密钥")

    monkeypatch.setattr(config, "load_api_key", forbidden)
    monkeypatch.setattr(llm, "urlopen", forbidden)
    monkeypatch.setattr(http.urllib.request, "urlopen", forbidden)


def test_robots_transient_errors_do_not_poison_disk_cache(tmp_path):
    """回归：robots.txt 本机瞬断（conn:*）只在当次会话生效，不写盘锁死全天；5xx/timeout 仍按天缓存。"""
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    manifest = {
        "https://algolia-node.example/robots.txt": {
            "sequence": [
                {"status": None, "error": "conn:reset"},
                {"status": 404, "body": ""},
            ]
        },
        "https://five-xx.example/robots.txt": {"status": 503, "body": "unavailable"},
    }
    (fixture_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    transport = http.FixtureTransport(fixture_dir)
    cache_dir = tmp_path / "cache"
    today = date(2026, 11, 4)

    first = RobotsChecker(transport, cache_dir, today)
    assert first.allowed("https://algolia-node.example/1/indexes/*/queries") is False
    # 同一会话内不重复请求。
    assert first.allowed("https://algolia-node.example/1/indexes/*/queries") is False
    assert len(transport.request_log) == 1
    assert not list(cache_dir.rglob("*.json"))

    second = RobotsChecker(transport, cache_dir, today)
    assert second.allowed("https://algolia-node.example/1/indexes/*/queries") is True
    assert len(transport.request_log) == 2
    assert len(list(cache_dir.rglob("*.json"))) == 1

    # 5xx 仍按天落盘禁止。
    assert second.allowed("https://five-xx.example/news") is False
    third = RobotsChecker(transport, cache_dir, today)
    assert third.allowed("https://five-xx.example/news") is False
    assert [r["url"] for r in transport.request_log].count("https://five-xx.example/robots.txt") == 1


def test_robots_ignores_legacy_poisoned_disk_cache(tmp_path):
    """回归：若磁盘残留旧版写入的 conn:* 断连缓存，读取时自动忽略并重新请求。"""
    cache_dir = tmp_path / "cache"
    day_dir = cache_dir / "robots" / "2026-11-04"
    day_dir.mkdir(parents=True)
    (day_dir / "yc-dsn.example.txt").write_text("", encoding="utf-8")
    (day_dir / "yc-dsn.example.json").write_text('{"status": null, "error": "conn:ConnectionResetError"}', encoding="utf-8")

    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    (fixture_dir / "manifest.json").write_text(
        json.dumps({"https://yc-dsn.example/robots.txt": {"status": 404}}),
        encoding="utf-8",
    )
    checker = RobotsChecker(http.FixtureTransport(fixture_dir), cache_dir, date(2026, 11, 4))
    assert checker.allowed("https://yc-dsn.example/1/indexes/*/queries") is True


def test_yc_batches_exclude_future_stubs_and_match_short_codes():
    """回归：YC 未来预录小批次不挤掉当季四批，且 Launch HN 的 F26/X26/W26/S26 正常匹配。"""
    facets_payload = json.dumps({
        "results": [{
            "facets": {
                "batch": {
                    "Summer 2027": 1,
                    "Winter 2027": 8,
                    "Fall 2026": 202,
                    "Summer 2026": 228,
                    "Spring 2026": 193,
                    "Winter 2026": 197,
                    "Fall 2025": 180,
                }
            }
        }]
    })
    resp = HttpResponse("https://algolia.example/queries", 200, "application/json", facets_payload)
    active = yc_directory.latest_batches(resp, as_of=date(2026, 11, 4))
    assert active == ["Fall 2026", "Summer 2026", "Spring 2026", "Winter 2026"]

    # 12 月下旬下一季大批量开营（>=30 家）时允许自动纳入。
    dec_payload = json.dumps({
        "results": [{
            "facets": {
                "batch": {
                    "Winter 2027": 140,
                    "Fall 2026": 202,
                    "Summer 2026": 228,
                    "Spring 2026": 193,
                    "Winter 2026": 197,
                }
            }
        }]
    })
    dec_resp = HttpResponse("https://algolia.example/queries", 200, "application/json", dec_payload)
    assert yc_directory.latest_batches(dec_resp, as_of=date(2026, 12, 20))[0] == "Winter 2027"

    # 混合简写与全称去重为四个独立季度。
    deduped = yc_directory.canonical_latest_batches(
        ["F26", "Fall 2026", "S26", "Summer 2026", "X26", "Spring 2026", "W26", "Winter 2026", "W25"],
        as_of=date(2026, 11, 4),
    )
    assert deduped == ["Fall 2026", "Summer 2026", "Spring 2026", "Winter 2026"]

    for code in ("F26", "X26", "SP26", "S26", "W26"):
        hn_item = RawItem("launch_hn", "99", "https://news.ycombinator.com/item?id=99",
                          f"Launch HN: Orbit ({code})", None, structured={"yc_batch": code})
        assert scan.filter_reasons(hn_item, Extracted(), "美国", "YC 标准投资", active) == []

    old_item = RawItem("launch_hn", "100", "https://news.ycombinator.com/item?id=100",
                       "Launch HN: Relic (F24)", None, structured={"yc_batch": "F24"})
    assert scan.filter_reasons(old_item, Extracted(), "美国", "YC 标准投资", active) == ["批次超过 12 个月"]


def test_atom_feed_and_extended_sources_fetching(tmp_path):
    """验证 Atom 1.0 解析能力与 6 路新接通公开信源的抓取与分级。"""
    atom_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Startup Feed</title>
      <entry>
        <id>tag:pulse.example,2026:kestrel-seed</id>
        <title>Kestrel AI Raises $4.5M in Seed Funding</title>
        <link rel="self" href="https://pulse.example/api/kestrel"/>
        <link rel="alternate" type="text/html" href="https://pulse.example/2026/11/kestrel-seed"/>
        <published>2026-11-03T14:20:00Z</published>
        <summary type="html">&lt;p&gt;Austin-based Kestrel AI raised $4.5M led by Foundry.&lt;/p&gt;</summary>
        <content type="html">&lt;div&gt;Austin-based Kestrel AI raised $4.5M in seed funding led by Foundry.&lt;/div&gt;</content>
      </entry>
    </feed>"""
    parsed = rss.parse(HttpResponse("https://pulse.example/atom.xml", 200, "application/atom+xml; charset=utf-8", atom_xml), "pulse2")
    assert parsed.error_type is None
    assert len(parsed.items) == 1
    assert parsed.items[0].url == "https://pulse.example/2026/11/kestrel-seed"
    assert parsed.items[0].published_at == datetime(2026, 11, 3, 14, 20, tzinfo=UTC)
    assert "Austin-based Kestrel AI" in parsed.items[0].body

    assert len(P0_SOURCES) == 11
    assert len(EXTENDED_SOURCES) == 7
    assert len(ALL_SOURCES) == 18
    assert display_rank("cbn_ai") == DISPLAY_ORDER.index("Crunchbase News")
    assert display_name("prn_vc") == "PR Newswire"
    assert display_name("siliconangle") == "SiliconANGLE"
    assert display_name("eu_startups") == "EU-Startups"
    assert name_for_url("https://pulse2.com/2026/11/deal/") == "Pulse 2.0"

    # 验证所有扩展源（含单页静态 .rss 的 prn_vc）在首次回补抓时均正常成功。
    fixture_dir = tmp_path / "ext"
    fixture_dir.mkdir()
    now = datetime(2026, 11, 4, 8, 40, tzinfo=BJ)
    rss_body = (
        "<rss version='2.0'><channel><item><guid>g1</guid>"
        "<link>https://news.example/deal-1</link>"
        "<title>Vesper Robotics Raises $6M Seed Round</title>"
        "<pubDate>Tue, 03 Nov 2026 15:00:00 GMT</pubDate>"
        "<description>Seattle-based Vesper Robotics raised $6M in seed funding.</description>"
        "</item></channel></rss>"
    )
    manifest = {}
    for src in EXTENDED_SOURCES:
        host = f"{urlsplit(src.url).scheme}://{urlsplit(src.url).netloc}"
        manifest[f"{host}/robots.txt"] = {"status": 404}
        manifest[src.url] = {"status": 200, "content_type": "application/rss+xml", "body": rss_body}
    (fixture_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    transport = http.FixtureTransport(fixture_dir)
    robots = RobotsChecker(transport, tmp_path / "cache", now.date())
    for src in EXTENDED_SOURCES:
        outcome = fetcher.fetch_source(src, SourceState(src.id, last_ok=False), transport, robots,
                                       now, cal.us_workdays_between, lambda _: None)
        assert outcome.ok, (src.id, outcome.runs)
        assert len(outcome.items) == 1
    # prn_vc 不应请求 ?paged=2。
    prn_urls = [entry["url"] for entry in transport.request_log if "prnewswire.com/rss/" in entry["url"]]
    assert prn_urls == [next(s.url for s in EXTENDED_SOURCES if s.id == "prn_vc")]


def test_year_boundary_calendar_safety_for_billing_and_limits(tmp_path):
    """回归：严格推送日历仍对未配置年份报错，但计费与日频控跨年不崩溃。"""
    with pytest.raises(cal.CalendarMissing):
        cal.is_cn_workday(date(2027, 1, 1))

    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    # 2025-01-01 的前一日是 2024-12-31（2024 不在 CN_HOLIDAYS），不应崩溃。
    assert verify.daily_limit(cfg, date(2025, 1, 1)) in {cfg.verify_daily_limit, cfg.verify_daily_limit_after_break}
    future_peak = datetime(2027, 1, 5, 10, 0, tzinfo=BJ)
    assert extract.usage_cost({"prompt_tokens": 1000, "completion_tokens": 200}, future_peak) > 0
    assert deep.usage_cost({"prompt_tokens": 1000, "completion_tokens": 200}, future_peak) > 0
    assert pipeline._peak(future_peak) is True


def test_end_to_end_pipeline_with_manifest_path_extended_sources_and_site(tmp_path):
    """端到端目标验收：README 的 manifest.json 路径调用 + 扩展信源去重入库 + 汇率换算 + 推送文件 + 静态回看站。"""
    state_dir = tmp_path / "state"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'state_dir = "{state_dir.as_posix()}"\n', encoding="utf-8")

    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    manifest = {}
    for src in ALL_SOURCES:
        parsed = urlsplit(src.url)
        manifest[f"{parsed.scheme}://{parsed.netloc}/robots.txt"] = {"status": 404}
    manifest["https://handwritten-dsn.algolia.net/robots.txt"] = {"status": 404}
    manifest["https://deal-media.example/robots.txt"] = {"status": 404}
    manifest["https://deal-media.example/"] = {
        "status": 200,
        "content_type": "text/html",
        "body": (
            "<p>Berlin-based Boreal Grid raised €10M in Series A funding led by Index Ventures.</p>"
            "<p>Boreal Grid builds AI workflow software for power grid operators across Europe.</p>"
        ),
    }
    manifest["https://techcrunch.com/2026/11/05/boreal-grid"] = manifest["https://deal-media.example/"]
    manifest["https://pulse2.com/2026/11/05/boreal-grid"] = manifest["https://deal-media.example/"]

    tc_rss = (
        "<rss><channel>"
        "<item><guid>boreal-tc</guid><link>https://techcrunch.com/2026/11/05/boreal-grid</link>"
        "<title>Boreal Grid Raises €10M in Series A funding</title>"
        "<pubDate>Thu, 05 Nov 2026 10:00:00 GMT</pubDate>"
        "<description>Berlin-based Boreal Grid raised €10M in Series A funding led by Index Ventures.</description></item>"
        "<item><guid>late-b</guid><link>https://deal-media.example/late-b</link>"
        "<title>Monolith Corp Raises $80M in Series C funding</title>"
        "<pubDate>Thu, 05 Nov 2026 11:00:00 GMT</pubDate>"
        "<description>New York-based Monolith Corp raised $80M in Series C funding.</description></item>"
        "</channel></rss>"
    )
    pulse_rss = (
        "<rss><channel>"
        "<item><guid>boreal-pulse</guid><link>https://pulse2.com/2026/11/05/boreal-grid</link>"
        "<title>Boreal Grid Closes €10M Series A Round</title>"
        "<pubDate>Thu, 05 Nov 2026 12:00:00 GMT</pubDate>"
        "<description>Berlin-based Boreal Grid raised €10M in Series A funding led by Index Ventures.</description></item>"
        "<item><guid>solace-prn</guid><link>https://deal-media.example/solace-prn</link>"
        "<title>Boston-based Solace Labs Raises $3.2M in Seed funding</title>"
        "<pubDate>Thu, 05 Nov 2026 13:00:00 GMT</pubDate>"
        "<description>Boston-based Solace Labs raised $3.2M in seed funding led by Accomplice.</description></item>"
        "</channel></rss>"
    )
    for src in P0_SOURCES:
        if src.kind == "rss":
            body = tc_rss if src.id == "tc_venture" else (
                "<rss><channel><item><guid>editorial-p0</guid>"
                f"<link>https://{urlsplit(src.url).netloc}/2026/11/05/market-notes</link>"
                "<title>Weekly notes on cloud infrastructure</title>"
                "<pubDate>Thu, 05 Nov 2026 10:00:00 GMT</pubDate>"
                "<description>General commentary without a specific startup round.</description></item></channel></rss>"
            )
            manifest[src.url] = {"status": 200, "content_type": "application/rss+xml", "body": body}
    for src in EXTENDED_SOURCES:
        body = pulse_rss if src.id == "pulse2" else (
            "<rss><channel><item><guid>solace-" + src.id + "</guid>"
            "<link>https://deal-media.example/solace-prn</link>"
            "<title>Boston-based Solace Labs Raises $3.2M in Seed funding</title>"
            "<pubDate>Thu, 05 Nov 2026 13:00:00 GMT</pubDate>"
            "<description>Boston-based Solace Labs raised $3.2M in seed funding led by Accomplice.</description></item></channel></rss>"
        )
        manifest[src.url] = {"status": 200, "content_type": "application/rss+xml", "body": body}

    sources_by_id = {s.id: s for s in ALL_SOURCES}
    manifest[sources_by_id["yc_launches"].url] = {
        "content_type": "application/json",
        "body": json.dumps({"hits": [{
            "id": 301, "slug": "harbor-ai", "title": "Harbor AI launch",
            "created_at": "2026-11-05T09:00:00Z",
            "tagline": "Harbor AI builds agentic code review for firmware teams.",
            "company": {"name": "Harbor AI", "url": "https://harbor-ai.example", "batch": "Fall 2026", "tags": ["AI"]},
        }]}),
    }
    manifest[sources_by_id["launch_hn"].url] = {
        "content_type": "application/json",
        "body": json.dumps({"hits": [{
            "objectID": "401",
            "title": "Launch HN: Harbor AI (YC F26) - Firmware verification",
            "created_at": "2026-11-05T11:00:00Z",
            "story_text": "San Francisco-based Harbor AI builds firmware verification tools.",
        }]}),
    }
    manifest[sources_by_id["yc_directory"].url] = {
        "content_type": "text/html",
        "body": '<script>window.AlgoliaOpts = {"app":"HANDWRITTEN","key":"public-search-placeholder"};</script>',
    }
    manifest["https://handwritten-dsn.algolia.net/"] = {
        "content_type": "application/json",
        "sequence": [
            {"body": json.dumps({"results": [{"facets": {"batch": {"Summer 2027": 1, "Fall 2026": 1}}}]})},
            {"body": json.dumps({"results": [{"nbHits": 1, "hits": [{
                "objectID": "harbor-dir", "slug": "harbor-ai", "name": "Harbor AI", "batch": "Fall 2026",
                "website": "https://harbor-ai.example", "all_locations": "San Francisco, CA, USA",
                "launched_at": 1793880000, "one_liner": "Agentic firmware verification.",
            }]}]})},
        ],
    }
    strict_url = "https://newsletter.strictlyvc.com/p/nov-5-issue"
    manifest[sources_by_id["strictlyvc"].url] = {
        "content_type": "application/xml",
        "body": f"<urlset><url><loc>{strict_url}</loc><lastmod>2026-11-05T08:00:00Z</lastmod></url></urlset>",
    }
    manifest[strict_url] = {
        "content_type": "text/html",
        "body": (
            '<script type="application/ld+json">{"datePublished":"2026-11-05T08:00:00Z"}</script>'
            "<h2>Smaller Fundings</h2><p>LumenMesh, a Austin-based networking startup, raised $4M in seed funding led by Coatue.</p>"
            "<h2>New Funds</h2>"
        ),
    }
    manifest[sources_by_id["ts_web"].url] = {
        "content_type": "application/json",
        "body": json.dumps([{"id": 88, "date_gmt": "2026-11-05T10:00:00Z"}]),
    }
    manifest["https://content.fortune.com/newsletter/termsheet/?post_id=88"] = {
        "content_type": "text/html",
        "body": "<h2>VENTURE CAPITAL</h2><p>CinderBio, a Boston-based biotech company, raised $5M in seed funding led by Polaris.</p>",
    }
    manifest_file = fixture_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    rates_file = tmp_path / "rates.json"
    rates_file.write_text(json.dumps({
        "base": "USD",
        "source": fx.SOURCE,
        "days": {
            "2026-11-04": {
                "fetched_at": "2026-11-05 08:00:00",
                "rates": {"USD": 1.0, "EUR": 0.9},
            }
        },
    }), encoding="utf-8")

    # 按 README 写法直接传 manifest.json 文件路径，启用 --extended-sources 在周五推送日跑完整流程。
    rc = cli.main([
        "--config", str(cfg_path),
        "--test",
        "--fixture", str(manifest_file),
        "--extended-sources",
        "--first-issue",
        "--no-verify",
        "--no-deep",
        "--rates-file", str(rates_file),
        "--date", "2026-11-06",
        "--now", "08:40",
        "--dry-run",
    ])
    assert rc == 0

    outbox_files = list((state_dir / "outbox-test").glob("2026-11-06*.json"))
    assert outbox_files

    rc_site = cli.main([
        "--config", str(cfg_path),
        "--test",
        "--build-site",
        "--date", "2026-11-06",
        "--now", "09:05",
    ])
    assert rc_site == 0

    site_dir = state_dir / "test" / "site"
    data = json.loads((site_dir / "data.json").read_text(encoding="utf-8"))
    names = {c["header"]["name"]: c for c in data["companies"]}
    # Boreal Grid 从 TechCrunch 与 Pulse 2.0 多源合并为同一家公司，且 €10M 按 1.1 换算为约 $11M（触发大额标记）。
    assert "Boreal Grid" in names
    assert "Solace Labs" in names
    assert "Harbor AI" in names
    # Series C 应被过滤，不进收录列表卡片。
    card_names = {e["name"] for e in data["events"] if e["id"] in data["week"]["cards"]}
    assert "Boreal Grid" in card_names
    assert "Solace Labs" in card_names
    assert "Harbor AI" in card_names
    assert "Monolith Corp" not in card_names

    boreal_event = next(e for e in data["events"] if e["name"] == "Boreal Grid")
    boreal_sources = {s["name"] for s in boreal_event["sources"]}
    assert {"TechCrunch", "Pulse 2.0"} <= boreal_sources
    assert "约" in boreal_event["amount"] or "€10M" in boreal_event["amount"]

    # 内部 status.json 从 config/not-connected.json 加载未接入信源说明。
    internal_status = json.loads((state_dir / "test" / "status.json").read_text(encoding="utf-8"))
    not_connected_names = {entry["name"] for entry in internal_status["not_connected"]}
    assert "Axios Pro Rata 邮件" in not_connected_names
    assert "Crunchbase 数据库" in not_connected_names
    assert "VentureBeat" in not_connected_names
    assert "Pulse 2.0" not in not_connected_names


def test_robots_wildcard_rule_supports_both_encoded_and_literal_wildcards():
    from types import SimpleNamespace
    from vcradar.robots import _WildcardRule

    for raw_path in ("/companies?%2A", "/companies?*"):
        rule = _WildcardRule(SimpleNamespace(path=raw_path, allowance=False))
        assert not rule.applies_to("/companies")
        assert rule.applies_to("/companies?batch=Fall+2026")

    for end_path in ("/feed/%24", "/feed/$"):
        rule = _WildcardRule(SimpleNamespace(path=end_path, allowance=True))
        assert rule.applies_to("/feed/")
        assert not rule.applies_to("/feed/extra")

