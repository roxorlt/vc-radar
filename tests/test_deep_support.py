"""深度测试共享的手写样例，不读取真实网页或密钥。"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from threading import Lock

from vcradar.config import BJ, Config
from vcradar.deep_fetch import Page
from vcradar import db

FIXTURE = Path(__file__).parent / "fixtures" / "deep"
PRODUCT = "Cedar Labs builds an AI runtime for developers."
USE = "Developers deploy Cedar Actors through a Python SDK."
FUNDING = "Cedar Labs raised $3 million in a Seed round led by Maple Ventures."
HQ = "Cedar Labs is headquartered in Boston."
TEAM = "Mira Example founded Cedar Labs in 2025 after working at Sample Robotics."
RISK = "Cedar Labs reports 3 pilot customers."


class ManualClock:
    simulated = True

    def __init__(self, stamp="2026-10-31T11:00:00+08:00"):
        self.stamp, self.lock = datetime.fromisoformat(stamp), Lock()

    def now(self):
        with self.lock:
            return self.stamp

    def advance(self, seconds):
        with self.lock:
            self.stamp += timedelta(seconds=seconds)


def point(text="提供智能体运行时。", quote=PRODUCT, **extra):
    return {"text": text, "quote": quote, "page": 1, **extra}


def payload():
    return {
        "产品是什么": [point(), point("开发者通过 SDK 部署。", USE)],
        "解决谁的什么问题": "来源里没写",
        "怎么赚钱": "来源里没写",
        "团队": [point("总部在 Boston。", HQ, field="hq", value="Boston"),
                 point("Mira Example 在 2025 年创立公司。", TEAM,
                       entities=[{"name": "Mira Example", "type": "人"}, {"name": "Sample Robotics", "type": "公司"}])],
        "融资": [point("种子轮融资 $3M，Maple Ventures 领投。", FUNDING, field="round", value="Seed",
                     entities=[{"name": "Maple Ventures", "type": "投资方"}])],
        "竞品": "来源里没写",
        "标签依据": {"ai": {"verdict": "是", "reason": "产品是智能体运行时。", "quote": PRODUCT, "page": 1},
                     "social": {"verdict": "否", "reason": "产品是开发工具，不连接真人。"}},
        "风险": [point("公司自报 3 家试用客户。", RISK)],
    }


def pages(body=None):
    return [Page(1, "https://news.example/cedar", "2026-10-31T08:40:00+08:00",
                 body or " ".join((PRODUCT, USE, FUNDING, HQ, TEAM, RISK)))]


def seed(conn, cid="cedar", *, first="2026-10-31", status="已纳入", source_id="news", structured=None,
         website="https://cedar.example/", body=None, batch=None, round_norm="种子轮", filters=None):
    conn.execute("INSERT OR IGNORE INTO source(id, name, column_name, url, kind) VALUES (?, 'TechCrunch', '', 'https://news.example/', 'rss')", (source_id,))
    conn.execute("INSERT INTO company(id, name_key, name, first_seen, status, website, hq, one_liner, yc_batch) VALUES (?, ?, ?, ?, ?, ?, 'Boston', 'Developer runtime', ?)",
                 (cid, cid, "Cedar Labs" if cid == "cedar" else cid, first, status, website, batch))
    event = conn.execute("INSERT INTO funding_event(company_id, round_norm, amount_text, amount_status, leads_json, event_date, first_seen, first_ingested_at, filter_reasons_json, yc_batch) "
                         "VALUES (?, ?, '$3M', '披露', '[\"Maple Ventures\"]', ?, ?, ?, ?, ?)",
                         (cid, round_norm, first, first + "T08:40:00+08:00", first + " 08:40:00", json.dumps(filters or []), batch)).lastrowid
    raw = conn.execute("INSERT INTO raw_item(source_id, external_id, url, title, first_seen_at, excerpt, structured_json) VALUES (?, ?, ?, '合成融资条目', ?, ?, ?)",
                       (source_id, cid, "https://news.example/" + cid, first + "T08:40:00+08:00",
                        body if body is not None else (FIXTURE / "source.html").read_text(), json.dumps(structured if structured is not None else {"website": website}))).lastrowid
    conn.execute("INSERT INTO event_source(event_id, raw_item_id, url, source_name, extraction_json, credibility) VALUES (?, ?, ?, 'TechCrunch', '{}', 1)",
                 (event, raw, "https://news.example/" + cid))
    conn.commit()
    return event


def database(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    return db.connect(cfg.db_path), cfg
