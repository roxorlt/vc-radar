"""SQLite 数据层（任务 B）。PRD 第 8 节。WAL 模式，库在仓库外。

表名、列名用英文，README.md「数据表」一节给出和 PRD 中文字段的对照。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .cal import calendar_rows
from .config import BJ
from .models import SourceDef, SourceState

SCHEMA_VERSION = 13
SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS source (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, column_name TEXT NOT NULL, url TEXT NOT NULL,
    kind TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'P0', stale_days INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1, crawl_status TEXT NOT NULL DEFAULT '正常',
    stopped_at TEXT, stop_reason TEXT, stop_notice_date TEXT
);
CREATE TABLE IF NOT EXISTS fetch_run (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES source(id),
    started_at TEXT NOT NULL, ended_at TEXT NOT NULL, http_code INTEGER,
    count INTEGER NOT NULL DEFAULT 0, new_count INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL, error_type TEXT, error_summary TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '', latest_at TEXT, is_backfill INTEGER NOT NULL DEFAULT 0,
    missed_range TEXT NOT NULL DEFAULT '', simulated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS fetch_run_source_time ON fetch_run(source_id, ended_at, id);
CREATE TABLE IF NOT EXISTS raw_item (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id),
    external_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
    published_at TEXT, first_seen_at TEXT NOT NULL, excerpt TEXT NOT NULL DEFAULT '',
    is_baseline INTEGER NOT NULL DEFAULT 0, skip_reason TEXT NOT NULL DEFAULT '',
    original_date_et TEXT, structured_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_id, external_id)
);
CREATE TABLE IF NOT EXISTS company (
    id TEXT PRIMARY KEY, domain_key TEXT, name_key TEXT NOT NULL, name TEXT NOT NULL,
    website TEXT, summary TEXT NOT NULL DEFAULT '', one_liner TEXT NOT NULL DEFAULT '',
    hq TEXT, hq_basis TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '国家待确认',
    yc_batch TEXT, first_seen TEXT NOT NULL,
    ai_label TEXT NOT NULL DEFAULT '待核', social_label TEXT NOT NULL DEFAULT '待核',
    label_quotes_json TEXT NOT NULL DEFAULT '{}', manual_label TEXT,
    status TEXT NOT NULL DEFAULT '已纳入', pending_reason TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS company_domain ON company(domain_key) WHERE domain_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS company_name ON company(name_key);
CREATE TABLE IF NOT EXISTS funding_event (
    id INTEGER PRIMARY KEY, company_id TEXT NOT NULL REFERENCES company(id),
    round_norm TEXT NOT NULL, round_text TEXT, amount_text TEXT,
    amount_currency TEXT, amount_original REAL, amount_usd REAL,
    fx_rate REAL, fx_date TEXT, fx_source TEXT, fx_stale INTEGER NOT NULL DEFAULT 0,
    amount_status TEXT NOT NULL DEFAULT '未披露', leads_json TEXT NOT NULL DEFAULT '[]',
    follows_json TEXT NOT NULL DEFAULT '[]', event_date TEXT NOT NULL, yc_batch TEXT,
    first_seen TEXT NOT NULL, first_ingested_at TEXT, marks_json TEXT NOT NULL DEFAULT '[]', push_date TEXT,
    baseline_unpushed INTEGER NOT NULL DEFAULT 0,
    filter_reasons_json TEXT NOT NULL DEFAULT '[]', pending INTEGER NOT NULL DEFAULT 0,
    last_pushed_date TEXT
);
CREATE INDEX IF NOT EXISTS funding_event_company_round ON funding_event(company_id, round_norm);
CREATE INDEX IF NOT EXISTS funding_event_pending ON funding_event(pending, baseline_unpushed);
CREATE TABLE IF NOT EXISTS event_source (
    event_id INTEGER NOT NULL REFERENCES funding_event(id) ON DELETE CASCADE,
    raw_item_id INTEGER NOT NULL REFERENCES raw_item(id), url TEXT NOT NULL,
    source_name TEXT NOT NULL, extraction_json TEXT NOT NULL,
    credibility INTEGER NOT NULL, model TEXT,
    PRIMARY KEY(event_id, raw_item_id), UNIQUE(event_id, url)
);
CREATE TABLE IF NOT EXISTS digest (
    date TEXT PRIMARY KEY, push INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '没有新增', error_sources TEXT NOT NULL DEFAULT '[]',
    event_ids_json TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL DEFAULT '',
    send_target TEXT NOT NULL DEFAULT '', generated_at TEXT, sent_at TEXT, sent_result TEXT,
    resend_count INTEGER NOT NULL DEFAULT 0, resend_sent_at TEXT, resend_sent_result TEXT,
    simulated INTEGER NOT NULL DEFAULT 0, window_start TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS deepdive (
    company_id TEXT NOT NULL REFERENCES company(id), run_date TEXT NOT NULL,
    status TEXT NOT NULL, running INTEGER NOT NULL DEFAULT 0,
    push_judgement_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}',
    materials_json TEXT NOT NULL DEFAULT '[]', search_count INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost REAL NOT NULL DEFAULT 0,
    removed_points INTEGER NOT NULL DEFAULT 0, quality_pending INTEGER NOT NULL DEFAULT 0,
    new_evidence_json TEXT NOT NULL DEFAULT '[]', new_evidence_found_at TEXT,
    started_at TEXT, ended_at TEXT, online_at TEXT, simulated INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0, prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0, elapsed_s REAL NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL DEFAULT '[]', attempt_cost REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '', run_token TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(company_id, run_date)
);
CREATE TABLE IF NOT EXISTS deepdive_pages (
    company_id TEXT NOT NULL REFERENCES company(id), run_date TEXT NOT NULL,
    page_no INTEGER NOT NULL, url TEXT NOT NULL, fetched_at TEXT NOT NULL,
    http_code INTEGER, body TEXT NOT NULL DEFAULT '', links_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(company_id, run_date, page_no),
    FOREIGN KEY(company_id, run_date) REFERENCES deepdive(company_id, run_date) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tag_change (
    id INTEGER PRIMARY KEY, company_id TEXT NOT NULL REFERENCES company(id),
    old TEXT NOT NULL, new TEXT NOT NULL, operator TEXT NOT NULL, changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar (
    date TEXT NOT NULL, country TEXT NOT NULL, type TEXT NOT NULL, source_name TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS calendar_identity ON calendar(date, country, type, COALESCE(source_name, ''));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    """打开（不存在就建）库，PRAGMA journal_mode=WAL、foreign_keys=ON，row_factory=sqlite3.Row，建表。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_SQL)
        with conn:
            migrate_backfill(conn)
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            previous_version = int(version[0]) if version else 0
            if "country" not in {column[1] for column in conn.execute("PRAGMA table_info(company)")}:
                # 延迟导入避免 scan/db 的模块依赖成环；只加列、回填，不重判旧状态或事件。
                from .scan import country_from_hq
                conn.execute("ALTER TABLE company ADD COLUMN country TEXT NOT NULL DEFAULT '国家待确认'")
                for row in conn.execute("SELECT id, name, hq, hq_basis FROM company").fetchall():
                    conn.execute("UPDATE company SET country=? WHERE id=?",
                                 (country_from_hq(row["hq"], row["hq_basis"], company_name=row["name"]), row["id"]))
            # 深度任务只增列，保留旧库的结果、费用和页面。
            deep_columns = {column[1] for column in conn.execute("PRAGMA table_info(deepdive)")}
            for name, definition in {
                "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
                "prompt_cache_hit_tokens": "INTEGER NOT NULL DEFAULT 0",
                "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
                "elapsed_s": "REAL NOT NULL DEFAULT 0",
                "usage_json": "TEXT NOT NULL DEFAULT '[]'",
                "attempt_cost": "REAL NOT NULL DEFAULT 0",
                "error": "TEXT NOT NULL DEFAULT ''",
                "run_token": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in deep_columns:
                    conn.execute(f"ALTER TABLE deepdive ADD COLUMN {name} {definition}")
            if "prompt_tokens" not in deep_columns:
                conn.execute("UPDATE deepdive SET prompt_tokens=input_tokens")
            if "completion_tokens" not in deep_columns:
                conn.execute("UPDATE deepdive SET completion_tokens=output_tokens")
            if "push" not in {column[1] for column in conn.execute("PRAGMA table_info(digest)")}:
                conn.execute("ALTER TABLE digest ADD COLUMN push INTEGER NOT NULL DEFAULT 1")
            if "note" not in {column[1] for column in conn.execute("PRAGMA table_info(digest)")}:
                conn.execute("ALTER TABLE digest ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            if "first_ingested_at" not in {column[1] for column in conn.execute("PRAGMA table_info(funding_event)")}:
                conn.execute("ALTER TABLE funding_event ADD COLUMN first_ingested_at TEXT")
            _migrate_amounts(conn)
            if previous_version < 8:
                _align_v16e(conn)
            # 老库没有首次写库日志，以已有 first_seen 补齐；已有新字段值始终保留。
            for row in conn.execute("SELECT id, first_seen FROM funding_event WHERE first_ingested_at IS NULL OR first_ingested_at=''").fetchall():
                stamp = datetime.fromisoformat(row["first_seen"])
                stamp = stamp.replace(tzinfo=BJ) if stamp.tzinfo is None else stamp.astimezone(BJ)
                conn.execute("UPDATE funding_event SET first_ingested_at=? WHERE id=?",
                             (stamp.strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
            # 旧库也只保留 V15e 的四种标记，金额状态与标记统一为「金额待核」。
            conn.execute("UPDATE funding_event SET amount_status='金额待核' WHERE amount_status='有分歧'")
            for row in conn.execute("SELECT id, marks_json FROM funding_event WHERE marks_json LIKE '%假期补发%' OR marks_json LIKE '%金额有分歧%'").fetchall():
                marks = {"金额待核" if mark == "金额有分歧" else mark for mark in json.loads(row["marks_json"])}
                conn.execute("UPDATE funding_event SET marks_json=? WHERE id=?",
                             (json.dumps([mark for mark in ("新一轮", "大额", "补抓", "金额待核") if mark in marks], ensure_ascii=False), row["id"]))
            conn.executemany("INSERT OR IGNORE INTO calendar VALUES (?, ?, ?, ?)", calendar_rows())
            conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
        return conn
    except BaseException:
        conn.close()
        raise


def migrate_backfill(conn: sqlite3.Connection) -> None:
    """增补 V16l 补充和查证列；旧行保留，时间默认回填原运行日期。"""
    for table, additions in {
        "company": {"backfill": "TEXT NOT NULL DEFAULT ''", "backfill_push_date": "TEXT",
                    "backfill_rank": "INTEGER", "product_terms_json": "TEXT NOT NULL DEFAULT '[]'", "city": "TEXT"},
        "funding_event": {"original_first_ingested_at": "TEXT"},
        "digest": {"supplement_event_ids_json": "TEXT NOT NULL DEFAULT '[]'"},
    }.items():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                if table == "company" and name == "backfill_rank":
                    for row in conn.execute("SELECT key,value FROM meta WHERE key LIKE 'backfill:rank:%'").fetchall():
                        data = json.loads(row[1])
                        if isinstance(data.get("rank"), int) and data["rank"] > 0:
                            conn.execute("UPDATE company SET backfill_rank=? WHERE id=?", (data["rank"], row[0].removeprefix("backfill:rank:")))
    conn.execute("""CREATE TABLE IF NOT EXISTS verify_log (
        id INTEGER PRIMARY KEY, company_id TEXT NOT NULL REFERENCES company(id),
        source TEXT NOT NULL DEFAULT '日常', run_date TEXT NOT NULL, run_started_at TEXT NOT NULL,
        field TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
        urls_json TEXT NOT NULL DEFAULT '[]', tiers_json TEXT NOT NULL DEFAULT '[]',
        known_fact_hit INTEGER NOT NULL DEFAULT 0, known_fact_json TEXT NOT NULL DEFAULT '[]',
        quotes_json TEXT NOT NULL DEFAULT '[]', verified_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS verify_search (
        id INTEGER PRIMARY KEY, company_id TEXT NOT NULL REFERENCES company(id),
        run_date TEXT NOT NULL, requested_at TEXT NOT NULL, query TEXT NOT NULL,
        status TEXT, result_count INTEGER NOT NULL DEFAULT 0, billed INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0, model_calls INTEGER NOT NULL DEFAULT 0,
        model_cost REAL NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT '日常',
        results_json TEXT NOT NULL DEFAULT '[]', UNIQUE(company_id, requested_at, query))""")
    # 后续查证任务可在已有表上继续增列；来源缺省日常以兼容旧查证行。
    for table in ("verify_log", "verify_search"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "source" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT '日常'")
        if "search_date" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN search_date TEXT")
            conn.execute(f"UPDATE {table} SET search_date=run_date")
        if "run_started_at" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN run_started_at TEXT")
            conn.execute(f"UPDATE {table} SET run_started_at=requested_at")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(verify_log)")}
    for name, definition in {"value_json": "TEXT NOT NULL DEFAULT 'null'", "event_id": "INTEGER",
                             "old_new_json": "TEXT NOT NULL DEFAULT 'null'",
                             "source_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                             "target_event_id": "INTEGER"}.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE verify_log ADD COLUMN {name} {definition}")
    # 旧表的公司/字段/时间唯一约束会吞掉同秒的另一条金额事件，需在事务中重建。
    table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='verify_log'").fetchone()[0]
    if "UNIQUE(company_id,field,verified_at)" in "".join(table_sql.split()):
        columns = list(conn.execute("PRAGMA table_info(verify_log)"))
        definitions = [f'"{r[1]}" {r[2]}' + (" PRIMARY KEY" if r[5] else "") +
                       (" NOT NULL" if r[3] else "") + (f" DEFAULT {r[4]}" if r[4] is not None else "")
                       for r in columns]
        conn.execute("CREATE TABLE verify_log_v12 (" + ",".join(definitions) + ", FOREIGN KEY(company_id) REFERENCES company(id))")
        names = ",".join('"' + r[1] + '"' for r in columns)
        conn.execute(f"INSERT INTO verify_log_v12 ({names}) SELECT {names} FROM verify_log")
        conn.execute("DROP TABLE verify_log")
        conn.execute("ALTER TABLE verify_log_v12 RENAME TO verify_log")
    conn.execute("UPDATE verify_log SET target_event_id=event_id,event_id=NULL WHERE event_id IS NOT NULL "
                 "AND result NOT IN ('已更正','金额待确认') AND COALESCE(old_new_json,'null')='null'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS verify_log_identity ON verify_log(company_id,COALESCE(event_id,0),field,verified_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS verify_field_latest ON verify_log(company_id,field,verified_at,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS verify_daily ON verify_search(source,run_date)")
    if "purpose" not in {r[1] for r in conn.execute("PRAGMA table_info(verify_search)")}:
        conn.execute("ALTER TABLE verify_search ADD COLUMN purpose TEXT NOT NULL DEFAULT '查证'")
    conn.execute("CREATE INDEX IF NOT EXISTS verify_purpose_daily ON verify_search(purpose,source,run_date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS homepage_check (
        id INTEGER PRIMARY KEY, company_id TEXT NOT NULL REFERENCES company(id),
        source TEXT NOT NULL DEFAULT '日常', run_date TEXT NOT NULL,
        candidate_from TEXT NOT NULL DEFAULT '', candidate_url TEXT NOT NULL DEFAULT '',
        final_url TEXT NOT NULL DEFAULT '', http TEXT, result TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '', matched_facts_json TEXT NOT NULL DEFAULT '[]',
        evidence_strength TEXT NOT NULL DEFAULT '', first_unverified_date TEXT,
        searched INTEGER NOT NULL DEFAULT 0, checked_at TEXT NOT NULL,
        links_written INTEGER NOT NULL DEFAULT 0, candidates_json TEXT NOT NULL DEFAULT '[]',
        search_results_json TEXT NOT NULL DEFAULT '[]', source_ids_json TEXT NOT NULL DEFAULT '[]')""")
    conn.execute("CREATE INDEX IF NOT EXISTS homepage_company_time ON homepage_check(company_id,checked_at,id)")


def _migrate_amounts(conn):
    columns = {column[1] for column in conn.execute("PRAGMA table_info(funding_event)")}
    added = False
    for name, definition in {"amount_currency": "TEXT", "amount_original": "REAL", "amount_usd": "REAL",
                             "fx_rate": "REAL", "fx_date": "TEXT", "fx_source": "TEXT",
                             "fx_stale": "INTEGER NOT NULL DEFAULT 0"}.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE funding_event ADD COLUMN {name} {definition}")
            added = True
    if not added:
        return
    from .fx import convert_amount, parse_amount
    # 迁移绝不联网；旧外币等后续正常运行取到汇率后再自动换算。
    for event in conn.execute("SELECT * FROM funding_event").fetchall():
        currency, value = parse_amount(event["amount_text"])
        if currency == "USD" and value is not None:
            conn.execute("UPDATE funding_event SET amount_currency='USD', amount_original=?, amount_usd=?, "
                         "fx_rate=1, fx_source='原文美元' WHERE id=?", (value, value, event["id"]))
        elif currency is not None and value is not None:
            marks = [mark for mark in json.loads(event["marks_json"]) if mark not in {"金额待核", "大额"}]
            conn.execute("UPDATE funding_event SET amount_status='暂未换算', amount_currency=?, amount_original=?, marks_json=? WHERE id=?",
                         (currency, value, json.dumps(marks, ensure_ascii=False), event["id"]))
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                         (f"fx:event:{event['id']}", json.dumps({"rates": {}, "stale": 0})))
        elif value is not None and currency is None:
            conn.execute("UPDATE funding_event SET amount_status='币种待确认' WHERE id=?", (event["id"],))
        for source in conn.execute("SELECT raw_item_id,extraction_json FROM event_source WHERE event_id=?", (event["id"],)).fetchall():
            data = json.loads(source["extraction_json"])
            data.setdefault("_amount_fx", convert_amount(data.get("amount_text")).to_dict())
            conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=? AND raw_item_id=?",
                         (json.dumps(data, ensure_ascii=False), event["id"], source["raw_item_id"]))


def _align_v16e(conn):
    from .scan import country_from_hq
    from .fx import convert_amount, parse_amount
    for company in conn.execute("SELECT id, name, hq, hq_basis FROM company").fetchall():
        conn.execute("UPDATE company SET country=? WHERE id=?",
                     (country_from_hq(company["hq"], company["hq_basis"], company_name=company["name"]), company["id"]))
    events = conn.execute("SELECT * FROM funding_event WHERE amount_status IN ('披露','金额待核','币种待确认') "
                          "AND amount_usd IS NULL").fetchall()
    for event in events:
        if " 写 " in (event["amount_text"] or ""):
            continue
        currency, value = parse_amount(event["amount_text"])
        if currency in {None, "USD"} or value is None:
            continue
        marks = [mark for mark in json.loads(event["marks_json"]) if mark not in {"金额待核", "大额"}]
        conn.execute("UPDATE funding_event SET amount_status='暂未换算', amount_currency=?, amount_original=?, marks_json=? WHERE id=?",
                     (currency, value, json.dumps(marks, ensure_ascii=False), event["id"]))
        for source in conn.execute("SELECT raw_item_id, extraction_json FROM event_source WHERE event_id=?", (event["id"],)).fetchall():
            data = json.loads(source["extraction_json"])
            if not data.get("_amount_fx", {}).get("amount_usd"):
                data["_amount_fx"] = convert_amount(data.get("amount_text")).to_dict()
                conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=? AND raw_item_id=?",
                             (json.dumps(data, ensure_ascii=False), event["id"], source["raw_item_id"]))


def is_empty(conn: sqlite3.Connection) -> bool:
    """只有 raw_item、company、funding_event 三张业务表都没有数据才算空库。"""
    return all(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None
               for table in ("raw_item", "company", "funding_event"))


def daily_verify_search_count(conn: sqlite3.Connection, day) -> int:
    """日上限只累计日常搜索，首期和补充命令不占次数。"""
    return conn.execute("SELECT COUNT(*) FROM verify_search WHERE source='日常' AND purpose='查证' AND run_date=?", (str(day),)).fetchone()[0]


def backup(conn: sqlite3.Connection, dest: Path) -> None:
    """用 backup() 导出已提交的整库；有未提交事务时拒绝，避免 SQLite 等待自己。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if conn.in_transaction:
        raise ValueError("备份前请先提交或回滚当前事务")
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()


def sync_sources(conn: sqlite3.Connection, sources: list[SourceDef]) -> None:
    """同步地址定义，保留启用开关、停抓状态等运行字段。"""
    conn.executemany(
        "INSERT INTO source(id, name, column_name, url, kind, stale_days) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, column_name=excluded.column_name, "
        "url=excluded.url, kind=excluded.kind, stale_days=excluded.stale_days",
        [(src.id, src.name, src.column, src.url, src.kind, src.stale_days) for src in sources],
    )


def load_source_states(conn: sqlite3.Connection) -> dict[str, SourceState]:
    """读取各地址最近一次结果、已知条目/期网址和 YC 目录快照。"""
    result = {}
    metadata = dict(conn.execute("SELECT key, value FROM meta"))
    for source in conn.execute("SELECT id, crawl_status FROM source"):
        sid = source["id"]
        latest = conn.execute(
            "SELECT result FROM fetch_run WHERE source_id=? ORDER BY ended_at DESC, id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        items = conn.execute("SELECT external_id, structured_json FROM raw_item WHERE source_id=?", (sid,)).fetchall()
        issues = set()
        for item in items:
            issue = json.loads(item["structured_json"]).get("issue_url")
            if issue:
                issues.add(issue)
        total = metadata.get(f"yc_last_batch_total:{sid}")
        last_success = conn.execute(
            "SELECT latest_at FROM fetch_run WHERE source_id=? AND result='成功' AND latest_at IS NOT NULL "
            "ORDER BY ended_at DESC, id DESC LIMIT 1", (sid,),
        ).fetchone()
        result[sid] = SourceState(
            source_id=sid, crawl_status=source["crawl_status"],
            last_ok=latest is None or latest["result"] == "成功",
            known_external_ids={item["external_id"] for item in items}, known_issue_urls=issues,
            yc_last_batch=metadata.get(f"yc_last_batch:{sid}"),
            yc_last_batch_total=int(total) if total is not None else None,
        )
        # 合并 main 前的 dataclass 也允许此属性；无需改共享契约文件。
        stamp = datetime.fromisoformat(last_success[0]) if last_success else None
        result[sid].last_latest_at = (stamp.replace(tzinfo=BJ) if stamp.tzinfo is None else stamp.astimezone(BJ)) if stamp else None
    return result


def manual_stop(conn: sqlite3.Connection, source_id: str, reason: str, now: datetime) -> None:
    """人工置停抓并补一条独立运行记录；已停抓时不重复记账。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("时间必须带时区")
    stamp = now.astimezone(BJ).isoformat()
    with conn:
        changed = conn.execute(
            "UPDATE source SET crawl_status='已停抓', stopped_at=?, stop_reason=?, stop_notice_date=NULL "
            "WHERE id=? AND crawl_status!='已停抓'", (stamp, reason, source_id),
        ).rowcount
        if not changed:
            if conn.execute("SELECT 1 FROM source WHERE id=?", (source_id,)).fetchone() is None:
                raise KeyError(source_id)
            return
        conn.execute(
            "INSERT INTO fetch_run(run_id, source_id, started_at, ended_at, http_code, count, result, note) "
            "VALUES (?, ?, ?, ?, NULL, 0, '已停抓', '人工停抓')",
            (uuid4().hex, source_id, stamp, stamp),
        )


def restore_source(conn: sqlite3.Connection, source_id: str) -> None:
    """人工恢复抓取，清空本次停抓时间、原因和提示日期。"""
    with conn:
        changed = conn.execute(
            "UPDATE source SET crawl_status='正常', stopped_at=NULL, stop_reason=NULL, stop_notice_date=NULL "
            "WHERE id=?", (source_id,),
        ).rowcount
        if not changed:
            raise KeyError(source_id)
