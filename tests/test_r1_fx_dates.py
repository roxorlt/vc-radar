"""PM 10-31 17:15 裁定 R1：固定汇率不算取不到；用的汇率日期不是公告日时，卡片和详情页括号注明汇率日期。
全部真的渲染本周页、详情页、来源状态页（app.js + node）后断言页面文字。"""
from argparse import Namespace
from datetime import datetime
import json
import re

import pytest

from vcradar import fx, outbox, scan, site
from vcradar.config import BJ, Config

from tests.test_site_build import render_page
from tests.test_v16_fx import NOW, add, connection, offline  # noqa: F401  offline 是 autouse 夹具

EVENT_DAY = datetime(2026, 10, 30, 9, 0, tzinfo=BJ)


def _rates(path, *days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base": "USD", "source": "probe", "days": {
        day: {"fetched_at": day + " 16:00:00", "rates": {"EUR": 0.9, "GBP": 0.75}} for day in days}}))
    return path


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _pages(tmp_path, monkeypatch, args, *, test_db_days=()):
    cfg = Config(state_dir=tmp_path, test_mode=True)
    monkeypatch.setattr(site, "LINK_RECORDS", tmp_path / "missing-links.json")
    if test_db_days:
        _rates(cfg.rates_file, *test_db_days)
    session = fx.rates_for_run(cfg, args, now=NOW)
    with connection(tmp_path) as conn:
        euro = add(conn, session, name="Euro", amount="€10M", day=EVENT_DAY)
        conn.execute("UPDATE funding_event SET push_date=? WHERE id=?", (str(NOW.date()), euro))
        scan.upsert_digest(conn, {"date": NOW.date(), "push": True, "event_ids": [euro],
                                  "generated_at": NOW, "status": "有新增", "text": ""})
        outbox.write_atomic(tmp_path / "test" / "status.json", {"fx": session.status})
        path = site.build_site(conn, cfg, now=NOW)
        status = site._snapshot(conn, cfg, NOW)["status"]["fx"]
    data = json.loads((path / "data.json").read_text())
    cid = data["events"][0]["company_id"]
    return (_text(render_page(path, "weekHtml")), _text(render_page(path, "detailHtml", cid)),
            "汇率日期：" + status["date"][5:] if status["ok"] else status["message"])


def test_r1_fixed_rates_file_older_day_notes_date_on_card_and_detail_but_status_stays_grey(tmp_path, monkeypatch):
    args = Namespace(rates_file=_rates(tmp_path / "fixed.json", "2026-09-15"), rates_fail=False, dry_run=True)
    week, detail, status = _pages(tmp_path, monkeypatch, args)
    amount = "约 1111 万美元（原 €1000 万，按 09-15 汇率）"
    assert amount in week and amount in detail
    assert "汇率日期：09-15" in status and "没更新" not in status and "没有取到" not in status


def test_r1_fixed_rates_file_on_announcement_day_has_no_date_note(tmp_path, monkeypatch):
    args = Namespace(rates_file=_rates(tmp_path / "fixed.json", "2026-10-30"), rates_fail=False, dry_run=True)
    week, detail, status = _pages(tmp_path, monkeypatch, args)
    assert "约 1111 万美元（原 €1000 万）" in week and "约 1111 万美元（原 €1000 万）" in detail
    assert "按 " not in week.split("约 1111 万美元")[1][:20]
    assert "汇率日期：10-30" in status


@pytest.mark.parametrize("with_file", [False, True], ids=["2-rates-fail", "4-rates-fail-and-file"])
def test_r1_rates_fail_uses_latest_and_status_says_not_updated(tmp_path, monkeypatch, with_file):
    # ② 只带 --rates-fail：用测试库 rates.json 最近一次；④ 再加 --rates-file：文件内容当最近一次。
    if with_file:
        args = Namespace(rates_file=_rates(tmp_path / "fixed.json", "2026-10-28"), rates_fail=True, dry_run=True)
        week, detail, status = _pages(tmp_path, monkeypatch, args, test_db_days=("2026-10-29",))
        day = "10-28"
    else:
        args = Namespace(rates_file=None, rates_fail=True, dry_run=True)
        week, detail, status = _pages(tmp_path, monkeypatch, args, test_db_days=("2026-10-29",))
        day = "10-29"
    amount = f"约 1111 万美元（原 €1000 万，按 {day} 汇率）"
    assert amount in week and amount in detail
    assert f"汇率没更新，用的是 {day} 的汇率" in status
