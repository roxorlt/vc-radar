"""V17 PM 第 3/4/5 条：合成库、固定抓取返回和实际前端离线渲染。"""
from copy import deepcopy
from datetime import date, datetime
from html.parser import HTMLParser
import json
import re

import pytest

from vcradar import db, render, site, wording
from vcradar.config import BJ, Config
from vcradar.models import DigestData, ErrorType, PendingLine, PushSection, SourceStatusLine
from test_er_render import card
from test_pipe_pipeline import offline, run_at, write_fixture, SOURCES  # noqa: F401
from test_site_build import NOW, node, render_page, seeded, snapshot  # noqa: F401
from test_site_build import html_text, summary_titles


FORBIDDEN = ("来源网站状态", "#sources", "本周共", "已过滤", "不算新增", "没抓全", "来源网站",
             "提前推送", "重发", "上次推送后", "上一次有新公司", "已接入")
WARNING = "YC 公司目录没抓到：对方限制了访问。本周汇总可能不全，不代表没有新公司。"


def assert_clean(text):
    # D3：只查产品文案，不能把公司名、简介或原文里的同词当成界面统计。
    text = re.sub(r'<article\b[^>]*>.*?</article>', "", text, flags=re.S)
    text = "\n".join(line for line in text.splitlines() if not re.match(r"\d+\. ", line))
    for phrase in FORBIDDEN:
        assert phrase not in text, phrase
    assert not re.search(r"共 \d+ 条|另有 \d+ 家|补充未推送 \d+ 家|本周有 \d+ 天", text)


def digest(**overrides):
    values = dict(push_date=date(2026, 10, 30), window_start=None, window_end=NOW,
                  status="基线", base_url="https://radar.test/private", review_site_passed=True,
                  baseline_count=90, resend=True, new_evidence_count=4, yc_new_batch=("Fall 2026", 40),
                  round_pending=[PendingLine("p", "Pending", "", "来源没写轮次")],
                  filtered={"B 轮及以后": 2}, last_push_summary=(date(2026, 10, 23), 10))
    values.update(overrides)
    return DigestData(**values)


def test_pm3_pm4_push_first_company_footer_and_internal_values_unchanged(tmp_path):
    data = digest(cards=[card()])
    before = deepcopy(data)
    text = render.render_push(data, early_push=True, failed_days=2,
        incomplete_until=date(2026, 10, 29), since_last_push=True, links_file=tmp_path / "no-links.json")
    assert text.splitlines()[1].startswith("1. Aster｜")
    assert text.splitlines()[-1] == "[看本周全部](https://radar.test/private/#week)"
    assert_clean(text)
    assert data == before


@pytest.mark.parametrize("passed", [False, True])
def test_pm4_no_new_has_footer_after_review_passed(passed):
    data = digest(status="没有新增", review_site_passed=passed, yc_new_batch=None)
    text = render.render_push(data, early_push=True, failed_days=3, incomplete_until=date(2026, 10, 29))
    assert text.splitlines() == ["10-30 周五 · 早期融资 · 本周", wording.NO_NEW] + (
        ["[看本周全部](https://radar.test/private/#week)"] if passed else [])
    assert_clean(text)


@pytest.mark.parametrize("failed", [False, True])
def test_pm3_pm4_yc_429_push_page_and_status_schema(tmp_path, failed):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True, review_site_passed=True,
                 base_url="https://radar.test/private", links_file=tmp_path / "links.json")
    fixture = write_fixture(tmp_path / "fixture", day="2026-10-30")
    manifest_path = fixture / "manifest.json"
    if failed:
        manifest = json.loads(manifest_path.read_text())
        manifest[SOURCES["yc_directory"].url] = {"status": 429}
        manifest_path.write_text(json.dumps(manifest))
    first, _ = run_at(cfg, fixture, "2026-10-30", first_issue=True)
    status_path = cfg.state_dir / "test/status.json"
    before = json.loads(status_path.read_text())
    # A1/N17-02：锁定运行字段与 V16o 状态的合并键表，不混入 digest 计数。
    expected = {"date", "status", "push", "alarm", "outbox", "simulated", "fx", "sources", "not_connected",
                "pending_ratios", "verification", "homepage", "deepdive_progress", "deepdive_budget",
                "extract_budget", "quality_pending", "new_evidence", "missed"}
    assert set(before) == expected
    assert set(before["fx"]) == {"date", "source", "ok", "message", "currencies"}
    with db.connect(cfg.db_path) as conn:
        path = site.build_site(conn, cfg, now=datetime(2026, 10, 30, 9, tzinfo=BJ))
        saved = dict(conn.execute("SELECT * FROM digest").fetchone())
        assert saved["status"] == "基线" and saved["event_ids_json"]
    week = render_page(path, "weekHtml")
    if failed:
        expected_warning = WARNING
        assert first["text"].splitlines()[1] == expected_warning
        assert first["text"].count(wording.FETCH_WARNING_END) == 1
        assert expected_warning not in week and 'class="alert"' not in week
        assert before["alarm"] is True
    else:
        assert first["text"].splitlines()[1].startswith("1. ")
        assert wording.FETCH_WARNING_END not in first["text"] + week
        assert before["alarm"] is False
    second, _ = run_at(cfg, fixture, "2026-10-30")
    after = json.loads(status_path.read_text())
    assert set(after) == expected and after["outbox"] != before["outbox"]
    assert first["alarm"] == second["alarm"]
    assert_clean(first["text"] + week)


def test_pm4_multiple_failures_and_unfinished_backfill_are_one_line():
    data = digest(sources=[SourceStatusLine("YC 公司目录", False, False, "", ErrorType.RATE_LIMITED),
                           SourceStatusLine("TechCrunch", False, False, "", ErrorType.TIMEOUT),
                           SourceStatusLine("AlleyWatch", True, False, "", missed_range="10-20 至 10-29")])
    warning = render.render_push(data).splitlines()[1]
    assert warning == "TechCrunch、YC 公司目录、AlleyWatch 没抓到：网站出了问题" + wording.FETCH_WARNING_END
    assert warning.count(wording.FETCH_WARNING_END) == 1


@pytest.mark.parametrize("passed", [False, True])
def test_pm4_supplement_keeps_companies_without_counts(passed):
    section = PushSection("backfill", wording.BACKFILL_TITLE, "另补充此前漏掉的 12 家",
                          [card("Extra")], overflow_count=2, overflow_text="另有 2 家没有列出")
    data = digest(extra_sections=[section], review_site_passed=passed)
    text = render.render_push(data)
    assert wording.BACKFILL_TITLE in text and "1. Extra｜" in text
    expected = "还有一些没有列出" + ("，[在往期页看](https://radar.test/private/#backfill)" if passed else "")
    assert expected in text
    assert_clean(text)


@pytest.mark.parametrize(("summary", "expected"), [
    ("没有句号", ["没有句号", ""]), ("只有一句。", ["只有一句。", ""]),
    ("第一句。第二句。第三句。", ["第一句。", "第二句。第三句。"]),
    ("We build software.", ["We build software.", ""]),
    ("We build software. Teams use it. More facts.", ["We build software.", "Teams use it. More facts."]),
    ("融资 1.3 亿美元。用于产品。", ["融资 1.3 亿美元。", "用于产品。"]),
    ("Raised 1.3 billion. Builds tools.", ["Raised 1.3 billion.", "Builds tools."]),
    ("", ["", ""]), ("公司说“产品已上线。”其余介绍。", ["公司说“产品已上线。”", "其余介绍。"]),
])
def test_pm5_summary_sentence_split(summary, expected):
    assert node("const ui=require(process.argv[1]);console.log(JSON.stringify(ui.splitSummary(process.argv[2])));", summary) == expected


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.top, self.depth = [], 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if self.depth == 0:
            self.top.append((tag, dict(attrs)))
        self.depth += 1

    def handle_endtag(self, tag):
        self.depth -= 1


def test_pm5_dates_descending_primary_source_unique_and_unknown_last(seeded):
    conn, cfg = seeded
    conn.execute("UPDATE fetch_run SET result='成功',error_type=NULL,missed_range='' WHERE source_id='yc_launches'")
    conn.execute("UPDATE raw_item SET published_at='2026-10-28T23:00:00Z' WHERE id=3")
    conn.execute("UPDATE raw_item SET published_at='2026-10-30T17:00:00Z' WHERE id=2")
    conn.execute("UPDATE raw_item SET published_at=NULL WHERE id=4")
    # 另一来源比第一来源晚；公司仍只落在第一来源的北京日期。
    conn.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,published_at,first_seen_at) VALUES "
                 "(99,'cbn_venture','r14-secondary','https://news.test/secondary','合成报道','2026-11-01T03:00:00Z','2026-10-31 08:00:00')")
    conn.execute("INSERT INTO event_source VALUES (3,99,'https://news.test/secondary','Crunchbase News','{}',1,NULL)")
    path, data = snapshot(seeded)
    week = render_page(path, "weekHtml")
    archive = node("const fs=require('fs'),ui=require(process.argv[1]),d=JSON.parse(fs.readFileSync(process.argv[2]));"
                   "console.log(JSON.stringify(ui.listHtml(d,ui.defaultFilters())));", path / "data.json")
    for html in (week, archive):
        days = re.findall(r'<section class="dgroup" data-date="([^"]*)">', html)
        assert days[-1] == "" and ">发布时间未知</span></h2>" in html
        assert days[:-1] == sorted(days[:-1], reverse=True)
        assert "2026-11-01" not in days
        assert html.count('>Flapping Airplanes</a>') == 1
        assert html.count('>Mach9</a>') == 1
        assert html.index('data-date="2026-10-29"><h2 class="dgroup-h"><span class="tnum">10-29 周四') < html.index('>Flapping Airplanes</a>')
        assert re.search(r'<h2 class="dgroup-h"><span class="tnum">[^<]+</span></h2><div class="co-list"><article\b', html)
        assert_clean(html)
    assert Elements(week).top[0] == ("section", {"class": "dgroup", "data-date": "2026-10-30"})
    internal = site._snapshot(conn, cfg, NOW)
    assert data["listing"] == internal["listing"]
    assert "status" not in data and "filtered_counts" not in data["week"]
    assert set(internal["status"]) >= {"sources", "pending_ratios", "fx", "homepage", "new_evidence"}
    assert summary_titles(week) == ["轮次待确认", "不在收录范围"]
    assert "<details open" not in week


def test_pm5_same_day_uses_push_sort_and_company_uses_selected_issue_card(seeded):
    conn, _ = seeded
    conn.execute("UPDATE fetch_run SET result='成功',error_type=NULL,missed_range='' WHERE source_id='yc_launches'")
    conn.execute("UPDATE funding_event SET first_ingested_at='2026-10-30 08:42:00' WHERE id=2")
    conn.execute("UPDATE raw_item SET published_at='2026-10-30T01:00:00Z'")
    path, data = snapshot(seeded)
    html = render_page(path, "weekHtml")
    names = re.findall(r'<h3 class="co-name"><a[^>]+>([^<]+)</a>', html)
    assert names[:2] == ["Flapping Airplanes", "Mach9"]
    assert names.count("Mach9") == 1
    # 同日金额说法不一在已披露金额之后，金额未披露排最后。
    assert names.index("Capveon") > names.index("Talos")
    assert names.index("Aviern") > names.index("Capveon")
    assert re.search(r'<p class="co-fund"><span>种子轮</span><span[^>]*>3400 万美元', html)
    assert "first_ingested_at" in data["events"][0]


@pytest.mark.parametrize("single", [False, True])
def test_pm5_card_six_rows_and_single_sentence_no_description(seeded, single):
    conn, _ = seeded
    summary = "公司做协作软件。" + ("融资 1.3 亿美元。团队继续开发。" if not single else "")
    conn.execute("UPDATE company SET summary=? WHERE id='flapping'", (summary,))
    path, _ = snapshot(seeded)
    html = render_page(path, "weekHtml")
    article = next(s for s in re.findall(r'<article\b[^>]*>.*?</article>', html, re.S) if '>Flapping Airplanes</a>' in s)
    rows = re.findall(r'<(?:div|p) class="(co-r1|co-prod|co-fund|co-desc|co-srcs|co-more-l)"', article)
    assert rows == ["co-r1", "co-prod", "co-fund"] + ([] if single else ["co-desc"]) + ["co-srcs", "co-more-l"]
    plain = html_text(article)
    positions = [plain.index("Flapping Airplanes"), plain.index("产品：公司做协作软件。"),
                 plain.index("A 轮"), plain.index("10 亿美元"), *([] if single else [plain.index("简介：融资 1.3 亿美元。")]),
                 plain.index("来源："), plain.index("详情")]
    assert positions == sorted(positions)
    assert article.count("公司做协作软件。") == 1
    assert ("简介：" not in article) is single


def test_pm3_removed_status_route_falls_back_to_week_and_pages_are_clean(seeded):
    path, data = snapshot(seeded)
    checked = node("""
      const fs=require('fs'),vm=require('vm');
      const nodes={header:{innerHTML:''},main:{innerHTML:''}};
      const context={URL,location:{hash:'#sources'},
        document:{querySelectorAll:()=>[],getElementById:id=>nodes[id],addEventListener:()=>{}},
        window:{addEventListener:()=>{}},fetch:async()=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(process.argv[2]))})};
      vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
      setImmediate(()=>console.log(JSON.stringify({html:nodes.main.innerHTML,header:nodes.header.innerHTML})));
    """, path / "data.json")
    assert checked["html"] == render_page(path, "weekHtml")
    assert "每周推送" not in checked["header"]
    assert "status" not in data
    for cid in (c["id"] for c in data["companies"]):
        assert_clean(render_page(path, "detailHtml", cid))
    for name in ("index.html", "app.js"):
        assert_clean((path / name).read_text())


def test_pm3_pm4_resend_trims_legacy_copy_and_preserves_original():
    old = ("10-30 周五 · 早期融资 · 本周\n本周共 1 家 · 已过滤 2 家\n"
           "提前推送 · 重发 · YC 公司目录没抓到：对方限制了访问。本周汇总可能不全，不代表没有新公司。\n"
           "1. Aster｜公司开发软件。｜A 轮 · 3400 万美元｜[来源](https://news.test/aster)\n"
           "YC 新一批 40 家\n[看本周全部 1 家](https://radar.test/#week)｜[来源网站状态](https://radar.test/#sources)")
    text = render.add_resend_notice(old)
    assert text.splitlines()[1] == WARNING and text.splitlines()[2].startswith("1. Aster｜")
    assert text.splitlines()[-1] == "[看本周全部](https://radar.test/#week)"
    assert "[来源](https://news.test/aster)" in text
    assert "本周共 1 家" in old
    assert_clean(text)


@pytest.mark.parametrize(("old", "new"), [
    (wording.RUN_ERRORS["全部失败"], "所有来源都没抓到：今天每个网站都出了问题"),
    (wording.RUN_ERRORS["没跑完"], "部分来源没抓到：今天没能按时抓完"),
])
def test_pm4_legacy_run_failures_use_one_short_warning(old, new):
    original = "10-30 周五 · 早期融资 · 本周\n" + wording.NO_NEW + "\n提前推送 · " + old
    text = render.add_resend_notice(original)
    assert text.splitlines() == ["10-30 周五 · 早期融资 · 本周", new + wording.FETCH_WARNING_END, wording.NO_NEW]
    assert render.warning_from_text(text) == new + wording.FETCH_WARNING_END
    assert_clean(text)
