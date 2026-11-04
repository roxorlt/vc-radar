"""只读导出读未迁移旧库（fetch_run 没有漏抓起止列）时不报错：13-#52⑤ 用只读副本建站。"""
import sqlite3
from datetime import datetime

from vcradar import db, scan
from vcradar.config import BJ
from vcradar.sources.registry import P0_SOURCES


def test_missed_runs_in_window_reads_pre_v17_fetch_run_without_bounds(tmp_path):
    path = tmp_path / "old.db"
    with db.connect(path) as conn:
        db.sync_sources(conn, [s for s in P0_SOURCES if s.id == "strictlyvc"])
        conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,result,missed_range) "
                     "VALUES ('old','strictlyvc','2026-11-06T08:30:00+08:00','2026-11-06T08:31:00+08:00','成功','不详')")
        conn.execute("ALTER TABLE fetch_run DROP COLUMN missed_start")
        conn.execute("ALTER TABLE fetch_run DROP COLUMN missed_end")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = scan.missed_runs_in_window(conn, None, datetime(2026, 11, 6, 9, tzinfo=BJ))
    finally:
        conn.close()
    assert [(r["source_id"], r["missed_range"], r["missed_start"], r["missed_end"]) for r in rows] == [("strictlyvc", "不详", None, None)]
