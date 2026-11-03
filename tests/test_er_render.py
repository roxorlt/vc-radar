"""PRD V16g 周报、页面卡片、排序和报警的离线场景。"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys

import pytest

# 仓库骨架没有安装元数据，兼容任务指定的 pytest 可执行文件入口。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vcradar.models import Card, CardSource, DigestData, ErrorType, PendingLine, SourceStatusLine
from vcradar.render import ERROR_TEXT, first_lines, is_alarm, render_card, render_push, sort_cards


BASE = "https://vc.example.invalid/review"
DAY = date(2026, 10, 30)


@pytest.fixture
def digest():
    sources = [
        SourceStatusLine("TechCrunch", True, False, "31 条"),
        SourceStatusLine("Crunchbase News", True, False, "21 条"),
        SourceStatusLine("YC Launches", True, False, "20 条"),
        SourceStatusLine("YC 公司目录", True, False, "当期 193 家"),
        SourceStatusLine("Launch HN", True, False, "50 条"),
        SourceStatusLine("StrictlyVC", True, False, "最新一期 10-29"),
        SourceStatusLine("AlleyWatch", True, False, "10 条"),
        SourceStatusLine("Term Sheet", True, False, "最新一期 10-30"),
    ]
    return DigestData(DAY, None, datetime(2026, 10, 30, tzinfo=timezone.utc), "有新增",
                      sources=sources, base_url=BASE, review_site_passed=True)


def card(name="Aster", group="AI", amount=34_000_000, **overrides):
    labels = {"AI+社交": "AI相关 + 社交/社群相关", "社交": "社交/社群相关",
              "AI": "AI相关", "都不是": "都不是", "标签待核": "AI相关 + 标签待核"}
    data = dict(company_id=name.lower().replace(" ", "-"), name=name, marks=[], round_label="A 轮",
                amount_label="$34M" if amount is not None else "金额未披露", lead_label="Cedar Capital",
                tag_label=labels[group], group=group,
                summary="公司为活动组织者开发规划工具。用于处理活动安排和志愿者协调。融资用于产品开发。",
                sources=[CardSource("TechCrunch", date(2026, 10, 22), "https://news.example.invalid/aster")],
                amount_usd=amount, event_date=DAY)
    data.update(overrides)
    return Card(**data)


def highlights(text):
    return [line for line in text.splitlines() if re.match(r"\d+\. ", line)]


def test_weekly_push_matches_new_structure(digest, tmp_path):
    digest.cards = [card("Terse", amount=None, round_label="种子轮", summary="Terse 提供状态管理软件。产品用于协作。"),
                    card("Flapping Airplanes", amount=1_000_000_000, marks=["大额"], amount_label="$1B",
                         summary="公司开发人工智能模型。团队提供训练工具。")]
    links = tmp_path / "links.json"
    links.write_text(json.dumps([
        {"type": "公司", "company": "Terse", "verified": True, "url": "https://terse.example/"},
        {"type": "公司", "company": "Flapping Airplanes", "verified": True, "url": "https://flapping.example/"},
    ]))
    assert render_push(digest, links_file=links).splitlines() == [
        "10-30 周五 · 早期融资 · 本周",
        "本周共 2 家：AI相关 2 · 社交/社群相关 0 · 新一轮 0 · 大额 1",
        "1. Flapping Airplanes【大额】｜公司开发人工智能模型。｜A 轮 · 10 亿美元｜"
        "[官网](https://flapping.example/) [来源](https://news.example.invalid/aster)",
        "2. Terse｜Terse 提供状态管理软件。｜种子轮 · 金额未披露｜"
        "[官网](https://terse.example/) [来源](https://news.example.invalid/aster)",
        f"[看本周全部 2 家]({BASE}/#week)｜[来源网站状态]({BASE}/#sources)",
    ]


def test_pending_and_filtered_counts_are_in_first_line_not_lists(digest):
    digest.cards = [card(f"AI{i}") for i in range(5)] + [card("Other", "都不是")]
    digest.round_pending = [PendingLine("unknown", "Unknown", "$2M", "来源没写轮次")]
    digest.filtered = {"轮次不在范围": 4, "批次超过 12 个月": 1}
    text = render_push(digest)
    assert text.splitlines()[1] == (
        "本周共 6 家：AI相关 5 · 社交/社群相关 0 · 新一轮 0 · 大额 0"
        " · 另有 1 家轮次待确认 · 已过滤 5 家")
    assert "Dream" not in text and "Unknown" not in text and "总部来源都没写" not in text
    assert len(highlights(text)) == 6 and "总部非美国" not in text


def test_ai_social_counts_include_combined_and_partially_confirmed_tags(digest):
    digest.cards = [card("Both", "AI+社交"), card("Pending", "标签待核"),
                    card("Social", "社交", tag_label="社交/社群相关 + 标签待核"),
                    card("Unknown", "标签待核", tag_label="标签待核"),
                    card("New", "都不是", marks=["新一轮", "大额"])]
    assert render_push(digest).splitlines()[1] == (
        "本周共 5 家：AI相关 2 · 社交/社群相关 2 · 新一轮 1 · 大额 1")


@pytest.mark.parametrize("filtered_count", [0, 3])
def test_filtered_count_override_is_unique_company_total(digest, filtered_count):
    digest.cards = [card()]
    digest.filtered = {"批次超过 12 个月": 4, "轮次不在范围": 4}
    first = render_push(digest, filtered_count=filtered_count).splitlines()[1]
    assert "已过滤 8 家" not in first
    assert (f"已过滤 {filtered_count} 家" in first) == (filtered_count > 0)
    assert "另有" not in first


def test_global_amount_sorting_crosses_all_tag_groups(digest):
    digest.cards = [card("Both", "AI+社交", 2), card("Social", "社交", 3), card("AI", "AI", 4),
                    card("Pending", "标签待核", 10), card("Other", "都不是", 20)]
    names = [line.split("｜")[0] for line in highlights(render_push(digest))]
    assert names == ["1. Other", "2. Pending", "3. AI", "4. Social", "5. Both"]


def test_ten_highlights_even_when_all_have_same_tag(digest):
    digest.cards = [card(f"Company{index:02}", amount=index) for index in range(17)]
    text = render_push(digest)
    rows = highlights(text)
    assert len(rows) == 10 and rows[0].startswith("1. Company16｜") and rows[-1].startswith("10. Company07｜")
    assert "本周共 17 家" in text and f"[看本周全部 17 家]({BASE}/#week)" in text
    assert all(old not in text for old in ("再看", "来源：", "领投", "#today", "AI相关 ·"))


def test_disclosed_disputed_and_undisclosed_amount_buckets(digest):
    digest.cards = [card("Unknown", amount=None), card("Disputed high", amount=2_000, marks=["金额待核"]),
                    card("Small", amount=1), card("Disputed low", amount=500, marks=["金额待核"]),
                    card("Large", amount=100), card("New unknown", amount=None, marks=["新一轮"])]
    assert [company.name for company in sort_cards(digest.cards)] == [
        "Large", "Small", "Disputed high", "Disputed low", "New unknown", "Unknown"]
    assert [line.split("｜")[0] for line in highlights(render_push(digest))] == [
        "1. Large", "2. Small", "3. Disputed high【金额说法不一】", "4. Disputed low【金额说法不一】",
        "5. New unknown【新一轮】", "6. Unknown"]


def test_amount_priority_precedes_marks_and_first_seen():
    early = datetime(2026, 10, 26, tzinfo=timezone.utc)
    companies = [card("Marked small", amount=1, marks=["新一轮", "大额"]), card("Plain large", amount=2)]
    assert [company.name for company in sort_cards(companies, first_seen={"marked-small": early})] == [
        "Plain large", "Marked small"]


@pytest.mark.parametrize("amount", [34_000_000, None])
def test_equal_amount_uses_marks_then_first_seen_with_timezone(amount):
    utc = datetime(2026, 10, 26, 6, tzinfo=timezone.utc)
    bj = timezone(timedelta(hours=8))
    companies = [card("Plain", amount=amount), card("New", amount=amount, marks=["新一轮"]),
                 card("Large", amount=amount, marks=["大额"]), card("Backfill", amount=amount, marks=["补抓"])]
    seen = {"plain": utc - timedelta(hours=2), "new": utc, "large": datetime(2026, 10, 26, 13, tzinfo=bj),
            "backfill": utc + timedelta(hours=1)}
    assert [company.name for company in sort_cards(companies, first_seen=seen)] == ["New", "Large", "Plain", "Backfill"]


@pytest.mark.parametrize("bucket", ["披露", "金额待核", "未披露"])
def test_new_round_then_large_then_first_seen_priority_in_every_amount_bucket(bucket):
    amount = None if bucket == "未披露" else 34_000_000
    base_marks = ["金额待核"] if bucket == "金额待核" else []
    companies = [card("Plain", amount=amount, marks=base_marks),
                 card("Large", amount=amount, marks=[*base_marks, "大额"]),
                 card("New later", amount=amount, marks=[*base_marks, "新一轮"]),
                 card("New earlier", amount=amount, marks=[*base_marks, "新一轮"]),
                 card("Both", amount=amount, marks=[*base_marks, "新一轮", "大额"])]
    first = datetime(2026, 10, 26, tzinfo=timezone.utc)
    seen = {"plain": first, "large": first + timedelta(seconds=1),
            "new-earlier": first + timedelta(seconds=2), "new-later": first + timedelta(seconds=3),
            "both": first + timedelta(seconds=4)}
    assert [company.name for company in sort_cards(companies, first_seen=seen)] == [
        "Both", "New earlier", "New later", "Large", "Plain"]


def test_equal_amount_marks_and_first_seen_use_case_insensitive_name_for_tenth_place(digest):
    names = ["Kilo", "juliet", "India", "hotel", "Golf", "foxtrot", "Echo", "delta", "Charlie", "bravo", "Alpha"]
    digest.cards = [card(name) for name in names]
    seen = {company.company_id: datetime(2026, 10, 26, tzinfo=timezone.utc) for company in digest.cards}
    text = render_push(digest, first_seen=seen)
    assert [company.name for company in sort_cards(digest.cards, first_seen=seen)] == list(reversed(names))
    rows = highlights(text)
    assert len(rows) == 10 and rows[0].startswith("1. Alpha｜") and rows[-1].startswith("10. juliet｜")
    assert "Kilo｜" not in text and f"[看本周全部 11 家]({BASE}/#week)" in text
    digest.cards.reverse()
    assert render_push(digest, first_seen=seen) == text


def test_first_seen_beats_event_date_and_missing_times_sort_last(digest):
    digest.cards = [card("Missing", event_date=date(2020, 1, 1)), card("Later", event_date=date(2021, 1, 1)),
                    card("Earlier", event_date=DAY)]
    seen = {"earlier": datetime(2026, 10, 26, tzinfo=timezone.utc),
            "later": datetime(2026, 10, 27, tzinfo=timezone.utc)}
    assert [line.split("｜")[0] for line in highlights(render_push(digest, first_seen=seen))] == [
        "1. Earlier", "2. Later", "3. Missing"]


def test_disputed_amount_sorts_by_first_source_not_largest_source(digest):
    digest.cards = [card("First low", amount=10_000_000, marks=["金额待核"],
                         amount_label="TechCrunch 写 $10M，Crunchbase News 写 $90M"),
                    card("First high", amount=12_000_000, marks=["金额待核"],
                         amount_label="TechCrunch 写 $12M，Crunchbase News 写 $20M")]
    rows = highlights(render_push(digest))
    assert rows[0].startswith("1. First high【金额说法不一】")
    assert rows[1].endswith("TechCrunch 写 1000 万美元，Crunchbase News 写 9000 万美元｜"
                            "[来源](https://news.example.invalid/aster)")


def test_sort_and_render_do_not_mutate_input(digest):
    digest.cards = [card("Z", amount=1), card("A", amount=2)]
    before = deepcopy(digest)
    render_push(digest)
    assert digest == before
    assert [company.name for company in sort_cards(list(reversed(digest.cards)))] == ["A", "Z"]


@pytest.mark.parametrize(("summary", "expected"), [
    ("第一句。第二句。第三句。", "第一句。"),
    ("金额为 €4.5 million。第二句。", "金额为 €450 万（暂未换算成美元）。"),
    ("第一句\n继续。第二句。", "第一句 继续。"),
    ("没有中文句号", "没有中文句号"), ("", ""),
])
def test_highlight_uses_only_summary_first_sentence(digest, summary, expected):
    digest.cards = [card(summary=summary)]
    middle = f"{expected}｜" if expected else ""  # 没有简介时省掉这一段（v16a 验收，见 open-questions V18）
    assert highlights(render_push(digest)) == [f"1. Aster｜{middle}A 轮 · 3400 万美元｜"
                                               "[来源](https://news.example.invalid/aster)"]


def test_counts_and_ordered_hints_use_separate_lines(digest):
    digest.cards = [card()]
    digest.resend = True
    digest.sources[0].ok = False
    digest.sources[0].error_type = ErrorType.RATE_LIMITED
    digest.sources[1].ok = False
    digest.sources[1].error_type = ErrorType.TIMEOUT
    digest.sources[1].missed_range = "10-24 09:00 至 10-26 08:30"
    digest.ts_stopped_notice = "封禁"
    digest.new_evidence_count = 3
    expected = [
        "提前推送", "重发", "本周有 2 天没抓到",
        "TechCrunch 没抓到：对方限制了访问。本周汇总可能不全，不代表没有新公司。",
        "Crunchbase News 没抓到：网站没响应。本周汇总可能不全，不代表没有新公司。",
        "可能有遗漏：Crunchbase News，10-24 09:00 至 10-26 08:30的内容没抓全",
        "Term Sheet 已停止抓取：对方封禁",
        f"[本周 3 家资料有出入]({BASE}/#sources)",
    ]
    assert first_lines(digest, early_push=True, failed_days=2) == expected
    lines = render_push(digest, early_push=True, failed_days=2).splitlines()
    assert lines[0] == "10-30 周五 · 早期融资 · 本周"
    assert lines[1] == "本周共 1 家：AI相关 1 · 社交/社群相关 0 · 新一轮 0 · 大额 0"
    assert lines[2] == " · ".join(expected)
    assert lines[3].startswith("1. Aster｜")
    assert is_alarm(digest, failed_days=2)


def test_complete_hint_order_and_punctuation_keep_count_line_separate(digest):
    digest.cards = [card()]
    digest.baseline_count = 24
    digest.resend = True
    digest.sources[0].ok = False
    digest.sources[0].error_type = ErrorType.TIMEOUT
    digest.sources[0].missed_range = "10-26 至 10-27"
    digest.ts_stopped_notice = "异议"
    digest.new_evidence_count = 2
    digest.round_pending = [PendingLine("pending", "Dream", "$17M", "来源没写轮次")]
    options = {"early_push": True, "failed_days": 1, "incomplete_until": date(2026, 10, 27),
               "since_last_push": True}
    expected = [
        "首次运行收录 24 家以前的公司，不算新增", "本周一到 10-27 日的内容没抓全", "提前推送", "重发",
        "本周有 1 天没抓到",
        "TechCrunch 没抓到：网站没响应。本周汇总可能不全，不代表没有新公司。",
        "可能有遗漏：TechCrunch，10-26 至 10-27的内容没抓全", "Term Sheet 已停止抓取：对方提出异议",
        f"[上次推送后 2 家资料有出入]({BASE}/#sources)",
    ]
    assert first_lines(digest, **options) == expected
    lines = render_push(digest, **options).splitlines()
    assert lines[1] == ("上次推送后共 1 家：AI相关 1 · 社交/社群相关 0 · 新一轮 0 · 大额 0"
                        " · 另有 1 家轮次待确认")
    assert lines[2] == " · ".join(expected)
    assert lines[2].count("。") == 2
    assert "。 · 可能有遗漏" in lines[2] and lines[3].startswith("1. Aster｜")


def test_advance_title_uses_actual_weekday(digest):
    digest.push_date = date(2026, 10, 29)
    digest.cards = [card()]
    lines = render_push(digest, early_push=True).splitlines()
    assert lines[0] == "10-29 周四 · 早期融资 · 本周"
    assert lines[1].startswith("本周共 1 家")
    assert lines[2] == "提前推送" and not is_alarm(digest)


@pytest.mark.parametrize(("error", "expected"), [
    (ErrorType.RATE_LIMITED, "News 没抓到：对方限制了访问。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.UNREACHABLE, "News 没抓到：页面打不开。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.TIMEOUT, "News 没抓到：网站没响应。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.EMPTY, "News 没抓到：页面能打开，但一条都读不出来。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.STALE, "News 可能没更新：最新一条是 2026-10-20 的。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.INTERCEPTED, "News 没抓到：被网站拦下来了。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.YC_UNREADABLE, "YC 公司目录没抓到：读不到公司目录。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.YC_DROP, "YC 公司目录可能出错：公司数量比上次少一半以上。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.NEWSLETTER_NO_DEALS, "News 没抓到：页面能打开，但读不到融资清单。本周汇总可能不全，不代表没有新公司。"),
    (ErrorType.NEWSLETTER_STALE, "News 可能没更新：最新一期是 2026-10-20 的。本周汇总可能不全，不代表没有新公司。"),
])
def test_each_v16g_9_1_source_error_text(digest, error, expected):
    digest.sources = [SourceStatusLine("News", False, False, "", error, date(2026, 10, 20))]
    assert first_lines(digest) == [expected]
    assert render_push(digest).splitlines()[2] == expected and is_alarm(digest)
    assert error in ERROR_TEXT


@pytest.mark.parametrize(("status", "prefix"), [("全部失败", "今天所有来源都没抓到"), ("没跑完", "今天没抓完")])
def test_failed_today_still_pushes_saved_weekly_cards(digest, status, prefix):
    digest.status = status
    digest.sources[0].ok = False
    digest.sources[0].error_type = ErrorType.TIMEOUT
    digest.cards = [card()]
    digest.new_evidence_count = 9
    message = f"{prefix}：本周汇总按已有的数据发，今天的新公司可能没收进来。已通知维护的人，恢复后补上的放进下一期。"
    hints = first_lines(digest, failed_days=1)
    assert hints == [message, "本周有 1 天没抓到", f"[本周 9 家资料有出入]({BASE}/#sources)"]
    text = render_push(digest, failed_days=1)
    assert text.splitlines()[1].startswith("本周共 1 家：")
    assert text.splitlines()[2] == " · ".join(hints)
    assert len(highlights(text)) == 1 and "[看本周全部 1 家]" in text
    assert "没有结果" not in text and "不是没有新增" not in text and is_alarm(digest)


@pytest.mark.parametrize("status", ["全部失败", "没跑完"])
def test_failed_today_with_empty_database_does_not_hide_alarm(digest, status):
    digest.status = status
    digest.sources = [SourceStatusLine("News", False, False, "", ErrorType.TIMEOUT)]
    lines = render_push(digest).splitlines()
    assert lines[0].endswith("早期融资 · 本周")
    assert "本周没有发现新的天使轮、种子轮、A 轮融资。" in lines[1]
    assert "本周汇总按已有的数据发" in lines[2]
    assert "全部正常" not in lines[3] and lines[3].startswith("已接入 1 个来源网站，其中 1 个出错")
    assert lines[4] == f"[看本周全部 0 家]({BASE}/#week)｜[来源网站状态]({BASE}/#sources)"
    assert is_alarm(digest)


@pytest.mark.parametrize("baseline_count", [0, 42])
def test_first_issue_includes_baseline_current_cards_and_incomplete_coverage(digest, baseline_count):
    digest.status = "基线"
    digest.baseline_count = baseline_count
    digest.cards = [card()]
    lines = render_push(digest, incomplete_until=date(2026, 10, 27)).splitlines()
    assert lines[1] == "本周共 1 家：AI相关 1 · 社交/社群相关 0 · 新一轮 0 · 大额 0"
    assert lines[2] == ((f"首次运行收录 {baseline_count} 家以前的公司，不算新增 · " if baseline_count > 0 else "")
                        + "本周一到 10-27 日的内容没抓全")
    assert len(highlights("\n".join(lines))) == 1 and "[看本周全部 1 家]" in lines[-1]
    assert "下一次推送" not in "\n".join(lines)


def test_zero_baseline_without_other_hints_omits_hint_line(digest):
    digest.status = "基线"
    digest.cards = [card()]
    lines = render_push(digest).splitlines()
    assert first_lines(digest) == []
    assert lines[1].startswith("本周共 1 家：") and lines[2].startswith("1. Aster｜")
    assert "已收录" not in "\n".join(lines) and "" not in lines


def test_baseline_does_not_suppress_resend_or_current_errors(digest):
    digest.status = "基线"
    digest.resend = True
    digest.sources[0].ok = False
    digest.sources[0].error_type = ErrorType.TIMEOUT
    lines = render_push(digest, early_push=True).splitlines()
    assert lines[1] == "本周没有发现新的天使轮、种子轮、A 轮融资。"
    assert lines[2].startswith("提前推送 · 重发 · TechCrunch 没抓到：网站没响应。")
    assert "首次运行收录 0 家以前的公司，不算新增" not in "\n".join(lines) and is_alarm(digest)


@pytest.mark.parametrize("duration", [timedelta(days=6), timedelta(days=7),
                                      timedelta(days=7, seconds=1), timedelta(days=14)])
@pytest.mark.parametrize("since_last_push", [False, True])
def test_window_does_not_override_explicit_since_last_push(digest, duration, since_last_push):
    digest.cards = [card()]
    digest.window_start = digest.window_end - duration
    digest.new_evidence_count = 2
    lines = render_push(digest, since_last_push=since_last_push).splitlines()
    since = "上次推送后" if since_last_push else "本周"
    assert lines[1].startswith(f"{since}共 1 家")
    assert lines[2] == f"[{since} 2 家资料有出入]({BASE}/#sources)"


def test_since_last_push_is_controlled_by_caller(digest):
    digest.cards = [card()]
    digest.new_evidence_count = 1
    lines = render_push(digest, since_last_push=True).splitlines()
    assert lines[1].startswith("上次推送后共 1 家") and "上次推送后 1 家资料有出入" in lines[2]
    assert not is_alarm(digest)


@pytest.mark.parametrize("old_label", ["上次推送后", "昨天", "本周", ""])
def test_legacy_evidence_label_cannot_override_since_last_push(digest, old_label):
    digest.cards = [card()]
    digest.new_evidence_count = 2
    digest.new_evidence_since_label = old_label
    assert render_push(digest).splitlines()[1].startswith("本周共 1 家")
    assert first_lines(digest) == [f"[本周 2 家资料有出入]({BASE}/#sources)"]
    assert first_lines(digest, since_last_push=True) == [f"[上次推送后 2 家资料有出入]({BASE}/#sources)"]
    digest.new_evidence_count = 0
    assert first_lines(digest) == []


def test_no_new_keeps_status_and_last_issue_before_footer(digest):
    digest.status = "没有新增"
    digest.last_push_summary = (date(2026, 10, 23), 3)
    assert render_push(digest).splitlines() == [
        "10-30 周五 · 早期融资 · 本周",
        "本周没有发现新的天使轮、种子轮、A 轮融资。",
        "已接入 8 个来源网站，全部正常：TechCrunch 31 条 · Crunchbase News 21 条 · YC Launches 20 条 · "
        "YC 公司目录 当期 193 家 · Launch HN 50 条 · StrictlyVC 最新一期 10-29 · AlleyWatch 10 条 · Term Sheet 最新一期 10-30",
        "上一次有新公司是 10-23 那期，3 家",
        f"[看本周全部 0 家]({BASE}/#week)｜[来源网站状态]({BASE}/#sources)",
    ]
    assert not is_alarm(digest)


def test_no_new_keeps_pending_summary_and_errors(digest):
    digest.status = "部分失败"
    digest.sources[0].ok = False
    digest.sources[0].error_type = ErrorType.INTERCEPTED
    digest.round_pending = [PendingLine("dream", "Dream", "$17M", "来源没写轮次")]
    text = render_push(digest, filtered_count=2)
    assert "另有 1 家轮次待确认 · 已过滤 2 家" in text.splitlines()[1]
    assert "TechCrunch 没抓到：被网站拦下来了。本周汇总可能不全" in text.splitlines()[2]
    assert "TechCrunch 未抓到" in text.splitlines()[3] and "全部正常" not in text and is_alarm(digest)
    assert text.splitlines()[4] == f"[看本周全部 0 家]({BASE}/#week)｜[来源网站状态]({BASE}/#sources)"


@pytest.mark.parametrize("reason,display_reason", [("封禁", "对方封禁"), ("异议", "对方提出异议")])
def test_stopped_notice_only_in_requested_issue_and_stopped_source_not_stale(digest, reason, display_reason):
    digest.sources[-1] = SourceStatusLine("Term Sheet", False, True, "", ErrorType.NEWSLETTER_STALE)
    digest.ts_stopped_notice = reason
    assert first_lines(digest) == [f"Term Sheet 已停止抓取：{display_reason}"] and is_alarm(digest)
    assert not is_alarm(digest, push=False)
    digest.ts_stopped_notice = None
    text = render_push(digest)
    assert first_lines(digest) == [] and not is_alarm(digest)
    assert "已接入 8 个来源网站，其中 1 个已停止抓取：" in text
    assert "Term Sheet 可能没更新" not in text and "全部正常" not in text


def test_prior_failed_days_alarm_only_on_push_day(digest):
    assert first_lines(digest, failed_days=2) == ["本周有 2 天没抓到"]
    assert is_alarm(digest, failed_days=2) and not is_alarm(digest, failed_days=2, push=False)
    assert "本周有" not in render_push(digest, failed_days=0).splitlines()[1]


@pytest.mark.parametrize("case", ["source", "incomplete", "all_failed", "missed"])
def test_today_failure_alarms_on_non_push_day_too(digest, case):
    if case == "source":
        digest.sources[0].ok = False
    elif case == "incomplete":
        digest.status = "没跑完"
    elif case == "all_failed":
        digest.status = "全部失败"
    else:
        digest.sources[0].missed_range = "10-20 至 10-23"
    assert is_alarm(digest) and is_alarm(digest, push=False)


def test_retry_success_resend_early_push_and_evidence_are_not_alarms(digest):
    # 只传当天最终结果：封禁信号之后重试成功，不当作信源出错。
    digest.sources[-1].error_type = ErrorType.RATE_LIMITED
    digest.resend = True
    digest.new_evidence_count = 2
    assert first_lines(digest, early_push=True) == ["提前推送", "重发", f"[本周 2 家资料有出入]({BASE}/#sources)"]
    assert not is_alarm(digest)


def test_missed_range_and_unknown_error_category(digest):
    digest.sources[0].missed_range = "10-20 至 10-23"
    assert first_lines(digest) == ["可能有遗漏：TechCrunch，10-20 至 10-23的内容没抓全"]
    digest.sources[0].ok = False
    assert "页面打不开" in first_lines(digest)[0]


@pytest.mark.parametrize("company_count", [0, 1, 12])
def test_yc_new_batch_follows_highlights_and_precedes_links_without_taking_a_slot(digest, company_count):
    digest.cards = [card(f"Company{index:02}", amount=index) for index in range(company_count)]
    digest.yc_new_batch = ("Fall 2026", 193)
    digest.last_push_summary = (date(2026, 10, 23), 3)
    lines = render_push(digest).splitlines()
    assert len(highlights("\n".join(lines))) == min(company_count, 10)
    batch_index = 2 + min(company_count, 10)
    assert lines[batch_index] == f"YC 新一批 193 家，[名单见网页]({BASE}/#yc)"
    assert lines[-1] == (
        f"[看本周全部 {company_count} 家]({BASE}/#week)｜[来源网站状态]({BASE}/#sources)")
    if company_count == 0:
        assert lines[1] == "本周没有发现新的天使轮、种子轮、A 轮融资。"
        assert lines[batch_index + 1].startswith("已接入 8 个来源网站，全部正常：")
        assert lines[batch_index + 2] == "上一次有新公司是 10-23 那期，3 家"


def test_page_card_keeps_three_lines_and_all_sources():
    company = card("Flapping Airplanes", marks=["大额"], amount_label="$1B",
                   lead_label="Lux Capital、Index Ventures 共同领投")
    assert render_card(company, BASE).splitlines() == [
        "Flapping Airplanes【大额】｜国家待确认｜A 轮 · 10 亿美元 · Lux Capital、Index Ventures 共同领投｜AI相关",
        company.summary,
        f"来源：[TechCrunch · 10-22](https://news.example.invalid/aster)　[详情]({BASE}/#c/flapping-airplanes)",
    ]


def test_page_and_push_use_four_marks_in_order_without_holiday_mark(digest):
    company = card(marks=["金额待核", "补抓", "新一轮", "大额", "假期补发", "金额有分歧", "大额"],
                   summary="第一句。\n第二句。\n第三句。")
    page = render_card(company, BASE)
    assert page.startswith("Aster【新一轮】【大额】【补录】【金额说法不一】｜") and page.count("\n") == 2
    digest.cards = [company]
    push = render_push(digest)
    assert highlights(push)[0].startswith("1. Aster【新一轮】【大额】【补录】【金额说法不一】｜第一句。｜")
    assert all(mark not in page + push for mark in ("假期补发", "金额有分歧"))


def test_page_disagreement_preserves_source_order_amounts_and_dates():
    sources = [CardSource("TechCrunch", date(2026, 10, 22), "https://news.example.invalid/a"),
               CardSource("Crunchbase News", date(2026, 10, 23), "https://news.example.invalid/b")]
    company = card(marks=["金额待核"], sources=sources,
                   amount_label="TechCrunch 写 $10M，Crunchbase News 写 $12M")
    text = render_card(company, BASE + "/")
    assert "【金额说法不一】｜国家待确认｜A 轮 · TechCrunch 写 1000 万美元，Crunchbase News 写 1200 万美元" in text
    assert text.splitlines()[2] == (
        "来源：[TechCrunch · 10-22](https://news.example.invalid/a)　"
        f"[Crunchbase News · 10-23](https://news.example.invalid/b)　[详情]({BASE}/#c/aster)")


def test_no_new_source_count_uses_names_not_fetch_addresses(digest):
    digest.sources.append(SourceStatusLine("TechCrunch", True, False, "12 条"))
    assert "已接入 8 个来源网站，全部正常" in render_push(digest)


def test_push_does_not_emit_old_structure_or_internal_technical_details(digest):
    digest.sources[0].ok = False
    digest.sources[0].error_type = ErrorType.RATE_LIMITED
    digest.ts_stopped_notice = "封禁"
    digest.cards = [card()]
    text = render_push(digest)
    assert all(word not in text for word in ("HTTP", "429", "403", "deepseek", "error_type", "接口",
                                             "今天的结果可能不全", "#today", "来源：", "（", "）"))
