import json
import time
from dataclasses import asdict, replace
from datetime import datetime
from threading import Event
from urllib.parse import parse_qs

import pytest

from test_sources_support import FIXTURES, NOW, SOURCES, fetch, response, rss, transport, workdays
from vcradar.config import BJ
from vcradar.fetcher import fetch_all, fetch_source
from vcradar.models import ErrorType, FetchResult, SourceState
from vcradar.robots import RobotsChecker
from vcradar.sources.registry import P0_SOURCES

TS = SOURCES["ts_web"].url
TS_ISSUE = "https://content.fortune.com/newsletter/termsheet/?post_id=701"
STRICT_ISSUE = "https://newsletter.strictlyvc.com/p/synthetic-issue"
ALGOLIA = "https://synthetic-dsn.algolia.net/1/indexes/*/queries"


@pytest.mark.parametrize("source_id", list(SOURCES))
def test_every_registered_address_normal(tmp_path, source_id):
    outcome, _ = fetch(tmp_path, source_id)
    assert outcome.ok, outcome.runs
    assert outcome.runs[0].result == FetchResult.SUCCESS
    assert outcome.runs[0].http_code == 200
    assert outcome.items
    if source_id in {"strictlyvc", "ts_web"}:
        assert outcome.latest_label == "最新一期 10-30"
    elif source_id == "yc_directory":
        assert outcome.latest_label == "当期 5 家"
    else:
        assert outcome.latest_label.endswith(" 条")


@pytest.mark.parametrize("spec,expected", [
    ({"status":429}, ErrorType.RATE_LIMITED),
    ({"status":403}, ErrorType.RATE_LIMITED),
    ({"status":401}, ErrorType.RATE_LIMITED),
    ({"status":451}, ErrorType.RATE_LIMITED),
    ({"status":404}, ErrorType.UNREACHABLE),
    ({"status":503}, ErrorType.UNREACHABLE),
    ({"status":None,"error":"timeout"}, ErrorType.TIMEOUT),
    ({"status":None,"error":"conn:offline"}, ErrorType.UNREACHABLE),
    ({"file":"intercepted.html","content_type":"text/html"}, ErrorType.INTERCEPTED),
    ({"body":"broken XML","content_type":"application/rss+xml"}, ErrorType.INTERCEPTED),
    ({"body":"<rss><channel/></rss>","content_type":"application/rss+xml"}, ErrorType.EMPTY),
])
def test_failure_classification(tmp_path, spec, expected):
    outcome, _ = fetch(tmp_path, "tc_venture", {SOURCES["tc_venture"].url:spec})
    assert not outcome.ok and outcome.error_type == expected
    assert outcome.runs[0].result == FetchResult.FAIL


@pytest.mark.parametrize("source_id", ["yc_launches", "launch_hn"])
def test_json_content_type_format_and_empty(tmp_path, source_id):
    for index, (spec, expected) in enumerate([
        ({"body":'{"hits":[]}',"content_type":"text/html"}, ErrorType.INTERCEPTED),
        ({"body":"broken JSON","content_type":"application/json"}, ErrorType.INTERCEPTED),
        ({"body":'{"hits":[]}',"content_type":"application/json"}, ErrorType.EMPTY),
    ]):
        outcome, _ = fetch(tmp_path / str(index), source_id, {SOURCES[source_id].url:spec})
        assert outcome.error_type == expected


def test_rss_interception_words_in_body_are_not_html_signals(tmp_path):
    body = rss(("synthetic", "Thu, 29 Oct 2026 20:00:00 GMT")).replace("Synthetic financing.", "Access Denied captcha recaptcha Just a moment cf-chl are fictional article text.")
    outcome, _ = fetch(tmp_path, "tc_venture", {SOURCES["tc_venture"].url:{"body":body,"content_type":"application/rss+xml"}})
    assert outcome.ok


def test_expiry_uses_eastern_dates_and_strict_greater_than(tmp_path):
    seen = []
    def boundary(a, b):
        seen.append((a.isoformat(), b.isoformat()))
        return 2
    outcome, _ = fetch(tmp_path, "tc_venture", days=boundary)
    assert outcome.ok
    assert seen == [("2026-10-29", "2026-10-29")]
    stale, _ = fetch(tmp_path / "stale", "tc_venture", days=lambda a, b:3)
    assert stale.error_type == ErrorType.STALE
    assert stale.runs[0].result == FetchResult.STALE
    assert stale.latest_beijing_date.isoformat() == "2026-10-30"


def test_staleness_injected_calendar_excludes_weekends_and_holiday(tmp_path):
    body = rss(("synthetic-holiday", "Wed, 25 Nov 2026 20:00:00 GMT"))
    outcome, _ = fetch(tmp_path, "tc_venture", {SOURCES["tc_venture"].url:{"body":body,"content_type":"text/xml"}},
                       now=datetime(2026, 11, 30, 8, 40, tzinfo=BJ))
    assert outcome.ok


def test_robots_forbidden_address_is_a_failure_and_is_not_requested(tmp_path):
    outcome, client = fetch(tmp_path, "tc_venture", {"https://techcrunch.com/robots.txt":{"body":"User-agent: *\nDisallow: /"}})
    assert not outcome.ok
    assert "robots 禁止" in outcome.runs[0].error_summary
    assert [entry["url"] for entry in client.request_log] == ["https://techcrunch.com/robots.txt"]


def test_alleywatch_404_and_aggregate_counts(tmp_path):
    failed, _ = fetch(tmp_path, "alleywatch", {SOURCES["alleywatch"].url:{"status":404}})
    assert failed.error_type == ErrorType.UNREACHABLE
    normal, _ = fetch(tmp_path / "normal", "alleywatch")
    assert normal.runs[0].count == 5
    assert normal.latest_label == "5 条"
    assert [item.external_id for item in normal.items] == ["a1"]


def test_all_alleywatch_posts_skipped_still_have_parsed_count(tmp_path):
    body = (FIXTURES / "alleywatch.xml").read_text().replace("Mintive Raises $7M for Shared Notes", "Monthly Roundup")
    outcome, _ = fetch(tmp_path, "alleywatch", {SOURCES["alleywatch"].url:{"body":body,"content_type":"text/xml"}})
    assert outcome.ok and outcome.items == []
    assert outcome.runs[0].count == 5


@pytest.mark.parametrize("bootstrap", ["<html><title>Companies</title></html>", '<script>window.AlgoliaOpts = {"app":"SYNTHETIC"};</script>'])
def test_yc_missing_key_is_unreadable(tmp_path, bootstrap):
    outcome, _ = fetch(tmp_path, "yc_directory", {SOURCES["yc_directory"].url:{"body":bootstrap,"content_type":"text/html"}})
    assert outcome.error_type == ErrorType.YC_UNREADABLE


@pytest.mark.parametrize("query_spec", [{"status":503}, {"status":None,"error":"timeout"}, {"body":'{"results":[{"nbHits":0,"hits":[]}]}'}, {"body":"not JSON"}])
def test_yc_query_failure_or_zero_is_unreadable(tmp_path, query_spec):
    spec = {"content_type":"application/json","sequence":[{"file":"facets.json"},query_spec]}
    outcome, _ = fetch(tmp_path, "yc_directory", {ALGOLIA:spec})
    assert outcome.error_type == ErrorType.YC_UNREADABLE


@pytest.mark.parametrize("last_batch,last_total,error", [("Fall 2026", 12, ErrorType.YC_DROP), ("Fall 2026", 10, None), ("Summer 2026", 200, None)])
def test_yc_drop_compares_only_same_batch_strictly_below_half(tmp_path, last_batch, last_total, error):
    state = SourceState("yc_directory", yc_last_batch=last_batch, yc_last_batch_total=last_total)
    original = asdict(state)
    outcome, _ = fetch(tmp_path, "yc_directory", state=state, days=lambda a,b:9999)
    assert outcome.error_type == error
    assert outcome.ok == (error is None)
    assert outcome.yc_batch == "Fall 2026" and outcome.yc_batch_total == 5
    assert asdict(state) == original


def test_yc_four_batch_post_queries_and_raw_key_redaction(tmp_path):
    outcome, client = fetch(tmp_path, "yc_directory", raw_dir=tmp_path / "raw")
    assert outcome.ok and len(outcome.items) == 4
    queries = [entry for entry in client.request_log if entry["url"] == ALGOLIA]
    assert len(queries) == 5
    params = [parse_qs(json.loads(entry["data"])["requests"][0]["params"], keep_blank_values=True) for entry in queries]
    assert json.loads(params[0]["facets"][0]) == ["batch"]
    assert params[0]["hitsPerPage"] == ["0"]
    assert [json.loads(param["facetFilters"][0])[0][0] for param in params[1:]] == ["batch:Fall 2026", "batch:Summer 2026", "batch:Spring 2026", "batch:Winter 2026"]
    assert all(param["hitsPerPage"] == ["1000"] for param in params[1:])
    assert [item.structured["yc_batch"] for item in outcome.items] == ["Fall 2026", "Summer 2026", "Spring 2026", "Winter 2026"]
    assert not any(entry["url"].startswith(SOURCES["yc_directory"].url + "?") for entry in client.request_log)
    assert all(entry["headers"]["Referer"] == SOURCES["yc_directory"].url for entry in queries)
    raw = list((tmp_path / "raw" / "2026-10-30").glob("*.out"))
    assert len(raw) == 6
    assert all("synthetic-public-search-value" not in path.read_text() for path in raw)
    assert "synthetic-public-search-value" not in json.dumps(client.request_log)


def test_yc_normal_html_body_words_do_not_trigger_interception(tmp_path):
    body = (FIXTURES / "directory.html").read_text().replace("Companies</div>", "Companies captcha Access Denied recaptcha</div>")
    outcome, _ = fetch(tmp_path, "yc_directory", {SOURCES["yc_directory"].url:{"body":body,"content_type":"text/html"}})
    assert outcome.ok


@pytest.mark.parametrize("source_id,issue", [("strictlyvc",STRICT_ISSUE), ("ts_web",TS_ISSUE)])
def test_newsletter_no_deals_and_stale_when_no_new_issue(tmp_path, source_id, issue):
    no_deals, _ = fetch(tmp_path, source_id, {issue:{"file":"empty-newsletter.html","content_type":"text/html"}})
    assert no_deals.error_type == ErrorType.NEWSLETTER_NO_DEALS
    state = SourceState(source_id, known_issue_urls={issue})
    stale, _ = fetch(tmp_path / "stale", source_id, state=state, days=lambda a,b:3)
    assert stale.error_type == ErrorType.NEWSLETTER_STALE
    assert stale.items == []
    assert stale.runs[0].count == (4 if source_id == "strictlyvc" else 2)
    assert stale.latest_label == "最新一期 10-30"


@pytest.mark.parametrize("file", ["termsheet.html", "termsheet-fallback.html"])
def test_term_sheet_deal_variants_through_fetcher(tmp_path, file):
    outcome, _ = fetch(tmp_path, "ts_web", {TS_ISSUE:{"file":file,"content_type":"text/html"}})
    assert outcome.ok


@pytest.mark.parametrize("blocked", [{"status":403}, {"status":401}, {"status":451}, {"status":429}, {"status":200,"file":"intercepted.html","content_type":"text/html"}])
def test_term_sheet_blocked_retry_stops_with_two_records(tmp_path, blocked):
    sleeps = []
    state = SourceState("ts_web")
    outcome, client = fetch(tmp_path, "ts_web", {TS:blocked}, state=state, sleep=sleeps.append)
    assert outcome.ok and outcome.stopped_now and outcome.error_type == ErrorType.TS_STOPPED
    assert [run.result for run in outcome.runs] == [FetchResult.FAIL, FetchResult.STOPPED]
    assert [run.note for run in outcome.runs] == ["拦截页" if blocked.get("status") == 200 else str(blocked["status"])] * 2
    assert sleeps == [120]
    assert outcome.runs[1].started_at >= NOW.replace(minute=42)
    assert outcome.runs[1].ended_at < NOW.replace(minute=45)
    assert [entry["url"] for entry in client.request_log] == [TS, TS]
    assert state.crawl_status == "正常"


@pytest.mark.parametrize("target", [TS, TS_ISSUE])
@pytest.mark.parametrize("retry,error,result", [({"status":200},None,FetchResult.SUCCESS), ({"status":503},ErrorType.UNREACHABLE,FetchResult.FAIL), ({"status":None,"error":"timeout"},ErrorType.TIMEOUT,FetchResult.FAIL)])
def test_term_sheet_retry_branches_at_api_and_issue(tmp_path, target, retry, error, result):
    spec = {"file":"termsheet-list.json" if target == TS else "termsheet.html", "content_type":"application/json" if target == TS else "text/html", "sequence":[{"status":403},retry]}
    outcome, client = fetch(tmp_path, "ts_web", {target:spec})
    assert not outcome.stopped_now
    assert outcome.error_type == error and outcome.ok == (error is None)
    assert [run.result for run in outcome.runs] == [FetchResult.FAIL, result]
    assert outcome.runs[0].note == "403"
    assert len([entry for entry in client.request_log if entry["url"] == target]) == 2


def test_term_sheet_stopped_no_requests_or_records(tmp_path):
    state = SourceState("ts_web", crawl_status="已停抓", last_ok=False)
    outcome, client = fetch(tmp_path, "ts_web", state=state)
    assert outcome.ok and outcome.error_type == ErrorType.TS_STOPPED
    assert outcome.runs == [] and outcome.items == [] and client.request_log == []


def test_term_sheet_5xx_does_not_retry_or_stop(tmp_path):
    sleeps = []
    outcome, client = fetch(tmp_path, "ts_web", {TS:{"status":503}}, sleep=sleeps.append)
    assert outcome.error_type == ErrorType.UNREACHABLE
    assert not outcome.stopped_now and len(outcome.runs) == 1 and sleeps == []
    assert len(client.request_log) == 1


def test_term_sheet_exception_scope_is_exact(tmp_path):
    client = transport(tmp_path, {"https://content.fortune.com/robots.txt":{"body":"User-agent: *\nDisallow: /"}})
    robots = RobotsChecker(client, tmp_path / "cache", NOW.date())
    src = replace(SOURCES["ts_web"], url="https://content.fortune.com/other")
    outcome = fetch_source(src, SourceState(src.id), client, robots, NOW, workdays, lambda _:None)
    assert not outcome.ok and "robots 禁止" in outcome.runs[0].error_summary
    assert [entry["url"] for entry in client.request_log] == ["https://content.fortune.com/robots.txt"]


def test_backfill_catches_known_on_second_page(tmp_path):
    src = SOURCES["tc_venture"]
    overrides = {
        src.url:{"body":rss(("newest","Thu, 29 Oct 2026 21:00:00 GMT")),"content_type":"text/xml"},
        src.url + "?paged=2":{"body":rss(("missed","Wed, 28 Oct 2026 21:00:00 GMT"),("known","Tue, 27 Oct 2026 21:00:00 GMT"),("older","Mon, 26 Oct 2026 21:00:00 GMT")),"content_type":"text/xml"},
    }
    state = SourceState(src.id, last_ok=False, known_external_ids={"known"})
    outcome, client = fetch(tmp_path, src.id, overrides, state=state)
    assert outcome.ok and outcome.missed_range == ""
    assert [item.external_id for item in outcome.items] == ["newest", "missed"]
    assert all(item.structured["backfill"] for item in outcome.items)
    assert outcome.runs[0].is_backfill and outcome.runs[0].pages == 2
    assert outcome.runs[0].count == 4
    assert not any("paged=3" in entry["url"] for entry in client.request_log)


def test_backfill_three_page_limit_has_honest_missing_time_upper_bound(tmp_path):
    src = SOURCES["tc_venture"]
    overrides = {}
    for page, day in [(1,29),(2,28),(3,27),(4,26)]:
        url = src.url + (f"?paged={page}" if page > 1 else "")
        overrides[url] = {"body":rss((f"new-{page}",f"2026-10-{day}T20:00:00Z")),"content_type":"text/xml"}
    outcome, client = fetch(tmp_path, src.id, overrides, state=SourceState(src.id,last_ok=False,known_external_ids={"old"}))
    assert outcome.ok and len(outcome.items) == 3
    assert outcome.runs[0].pages == 3 and outcome.runs[0].is_backfill
    assert "2026-10-28 04:00" in outcome.missed_range and "北京时间" in outcome.missed_range
    assert outcome.missed_range == "时间未知 ~ 2026-10-28 04:00（北京时间）"
    assert outcome.runs[0].missed_range == outcome.missed_range
    assert not any("paged=4" in entry["url"] for entry in client.request_log)


def test_backfill_page_failure_keeps_partial_items_and_missing_range(tmp_path):
    src = SOURCES["tc_venture"]
    outcome, client = fetch(tmp_path, src.id, {src.url + "?paged=2":{"status":503}},
                            state=SourceState(src.id,last_ok=False,known_external_ids={"older-known"}))
    assert not outcome.ok and outcome.error_type == ErrorType.UNREACHABLE
    assert len(outcome.items) == 2 and outcome.runs[0].count == 2
    assert outcome.latest_label == "2 条" and outcome.missed_range
    assert outcome.runs[0].pages == 2
    assert not any("paged=3" in entry["url"] for entry in client.request_log)


@pytest.mark.parametrize("source_id,separator", [("yc_launches","?page="), ("launch_hn","&page=")])
def test_backfill_api_page_url_and_first_page_known_stop(tmp_path, source_id, separator):
    state = SourceState(source_id,last_ok=False,known_external_ids={"known"})
    outcome, client = fetch(tmp_path, source_id, state=state)
    assert outcome.runs[0].pages == 3
    assert any(entry["url"] == SOURCES[source_id].url + separator + "2" for entry in client.request_log)
    assert all(item.structured["backfill"] for item in outcome.items)
    known = "501" if source_id == "yc_launches" else "901"
    caught, client = fetch(tmp_path / "caught", source_id, state=SourceState(source_id,last_ok=False,known_external_ids={known}))
    assert caught.ok and caught.runs[0].pages == 1 and caught.items == [] and not caught.missed_range


@pytest.mark.parametrize("source_id", ["strictlyvc", "ts_web"])
def test_newsletters_three_issue_limit_count_latest_and_backfill(tmp_path, source_id):
    strict = source_id == "strictlyvc"
    refs = [(f"https://newsletter.strictlyvc.com/p/synthetic-{i}" if strict else f"https://content.fortune.com/newsletter/termsheet/?post_id={i}") for i in range(10, 5, -1)]
    if strict:
        index_body = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{url}</loc><lastmod>2026-10-{day}</lastmod></url>" for day,url in zip(range(29,24,-1),refs)) + "</urlset>"
    else:
        index_body = json.dumps([{"id":i,"date_gmt":f"2026-10-{day}T21:30:00"} for i,day in zip(range(10,5,-1),range(29,24,-1))])
    overrides = {SOURCES[source_id].url:{"body":index_body,"content_type":"application/xml" if strict else "application/json"}}
    overrides.update({url:{"file":"strictlyvc.html" if strict else "termsheet.html","content_type":"text/html"} for url in refs})
    outcome, client = fetch(tmp_path, source_id, overrides, state=SourceState(source_id,last_ok=False,known_issue_urls={refs[-1]}))
    assert outcome.ok and outcome.missed_range
    assert outcome.runs[0].count == (4 if strict else 2)
    assert len(outcome.items) == (12 if strict else 6)
    assert all(item.structured["backfill"] for item in outcome.items)
    assert outcome.runs[0].pages == 3
    assert not any(entry["url"] == refs[3] for entry in client.request_log)


def test_fetch_all_deadline_keeps_finished_and_marks_inflight_not_run(tmp_path):
    slow, fast = SOURCES["tc_venture"], SOURCES["cbn_main"]
    client = transport(tmp_path, {slow.url:{"file":"feed.xml","content_type":"text/xml","delay_s":0.2}})
    robots = RobotsChecker(client, tmp_path / "cache", NOW.date())
    started = time.monotonic()
    outcomes = fetch_all([slow,fast], {}, client, robots, NOW, workdays, lambda _:None, deadline_monotonic=started+0.06,max_workers=2)
    assert time.monotonic() - started < 0.16
    assert outcomes[0].runs[0].result == FetchResult.NOT_RUN
    assert outcomes[1].ok


def test_fetch_all_expired_deadline_has_no_requests_and_stopped_ts_is_ignored(tmp_path):
    client = transport(tmp_path)
    robots = RobotsChecker(client,tmp_path / "cache",NOW.date())
    outcomes = fetch_all([SOURCES["tc_venture"],SOURCES["ts_web"]], {"ts_web":SourceState("ts_web",crawl_status="已停抓")},
                         client,robots,NOW,workdays,lambda _:None,deadline_monotonic=time.monotonic()-1)
    assert outcomes[0].error_type == ErrorType.TS_STOPPED and outcomes[0].runs == []
    assert outcomes[1].runs[0].result == FetchResult.NOT_RUN and client.request_log == []


def test_fetch_all_ts_starts_first_and_retry_sleep_does_not_block_others(tmp_path):
    client = transport(tmp_path, {TS:{"file":"termsheet-list.json","content_type":"application/json","sequence":[{"status":403},{"status":200}]}})
    robots = RobotsChecker(client,tmp_path / "cache",NOW.date())
    other_started = Event()
    original_get = client.get
    def get(url, **kwargs):
        result = original_get(url, **kwargs)
        if url == SOURCES["tc_venture"].url:
            other_started.set()
        return result
    client.get = get
    def sleep(seconds):
        assert seconds == 120
        assert other_started.wait(1)
    outcomes = fetch_all([SOURCES["tc_venture"],SOURCES["ts_web"]],{},client,robots,NOW,workdays,sleep,max_workers=2)
    assert all(outcome.ok for outcome in outcomes)
    assert client.request_log[0]["url"] == TS
    assert [run.result for run in outcomes[0].runs] == [FetchResult.FAIL,FetchResult.SUCCESS]


def test_fetch_all_normal_all_sources_and_same_host_robots_cached(tmp_path):
    client = transport(tmp_path)
    robots = RobotsChecker(client,tmp_path / "cache",NOW.date())
    outcomes = fetch_all(P0_SOURCES,{},client,robots,NOW,workdays,lambda _:None)
    assert len(outcomes) == 11 and all(outcome.ok for outcome in outcomes)
    assert client.request_log[0]["url"] == TS
    robot_urls = [entry["url"] for entry in client.request_log if entry["url"].endswith("/robots.txt")]
    assert len(robot_urls) == len(set(robot_urls)) == 7
