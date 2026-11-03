from dataclasses import replace
from copy import deepcopy
from datetime import date, timedelta
import json
import shutil
import threading
import time

import pytest

from vcradar import cli, db, deep, pipeline
from vcradar.deep_fetch import Budget
from vcradar.http import FixtureTransport
from vcradar.llm import LLMResult, MockLLMClient
from vcradar.search import ENDPOINT
from test_deep_support import FIXTURE, ManualClock, database, pages, payload, point, seed


def run(conn, cfg, client, clock=None, transport=None, **kwargs):
    return deep.run_for_today(conn, cfg, clock or ManualClock(), transport=transport or FixtureTransport(FIXTURE),
                              client=client, link_records=[], **kwargs)


def deep_row(conn, cid="cedar"):
    return dict(conn.execute("SELECT * FROM deepdive WHERE company_id=? ORDER BY run_date DESC LIMIT 1", (cid,)).fetchone())


def record(conn, cid, status, *, running=0, started="2026-10-31T10:59:00+08:00"):
    conn.execute("INSERT INTO deepdive(company_id, run_date, status, running, started_at) VALUES (?, '2026-10-31', ?, ?, ?)", (cid, status, running, started))
    conn.commit()


def test_success_persists_pages_usage_new_evidence_and_leaves_main_fields_unchanged(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("vcradar.config.load_api_key", lambda *_: pytest.fail("不能读取密钥"))
    conn, cfg = database(tmp_path)
    seed(conn)
    before = [tuple(row) for row in conn.execute("SELECT * FROM company UNION ALL SELECT * FROM company WHERE 0")]
    event_before = [tuple(row) for row in conn.execute("SELECT * FROM funding_event")]
    outbox = cfg.state_dir / "outbox" / "2026-10-31.json"
    outbox.parent.mkdir(parents=True)
    outbox.write_bytes(b"existing handoff")
    client = MockLLMClient([{"content": payload(), "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 200}}])
    output = run(conn, cfg, client)[0]
    row = deep_row(conn)
    assert output.status == row["status"] == "完成"
    assert row["running"] == 0 and row["simulated"] == 1
    assert row["prompt_tokens"] == row["input_tokens"] == 1000
    assert row["completion_tokens"] == row["output_tokens"] == 200
    assert row["prompt_cache_hit_tokens"] == 400 and row["model_calls"] == 1
    assert row["estimated_cost"] == pytest.approx(0.006408)
    assert row["online_at"] is None
    assert json.loads(row["new_evidence_json"])[0]["field"] == "AI相关"
    assert row["new_evidence_found_at"] == row["ended_at"]
    assert conn.execute("SELECT COUNT(*) FROM deepdive_pages").fetchone()[0] >= 2
    assert [tuple(row) for row in conn.execute("SELECT * FROM company")] == before
    assert [tuple(row) for row in conn.execute("SELECT * FROM funding_event")] == event_before
    assert outbox.read_bytes() == b"existing handoff" and not cfg.usage_file.exists()
    assert not (cfg.state_dir / "deep-usage.json").exists()
    ledger = json.loads((cfg.state_dir / "test/deep-usage.json").read_text())["2026-10"]
    assert ledger["estimated_cost"] == pytest.approx(row["estimated_cost"])
    assert ledger["prompt_cache_hit_tokens"] == 400
    log = (cfg.log_dir / "deep-2026-10-31.log").read_text()
    assert "prompt_cache_hit_tokens" in log and "offline-fixture" not in log and "Authorization" not in log
    stdout = capsys.readouterr().out
    assert all(word in stdout for word in ("Cedar Labs", "完成", "耗时", "抓取页数", "模型调用", "费用", "删掉要点", "质量待核", "有新证据", "总计"))
    assert not run(conn, cfg, client) and client.calls == 1
    conn.close()


@pytest.mark.parametrize("first", [
    {"error": "http_429", "usage": {"prompt_tokens": 10}},
    {"error": "timeout"}, {"content": "bad json", "usage": {"completion_tokens": 7}},
])
def test_model_failures_share_the_two_call_limit_and_usage_is_charged(tmp_path, first):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([first, payload()])
    result = run(conn, cfg, client)[0]
    assert result.status == "完成" and client.calls == 2 and deep_row(conn)["model_calls"] == 2
    assert result.tokens["prompt_tokens"] == first.get("usage", {}).get("prompt_tokens", 0)
    assert result.tokens["completion_tokens"] == first.get("usage", {}).get("completion_tokens", 0)
    assert all(request["timeout_s"] <= 120 for request in client.requests)
    conn.close()


def damaged():
    value = payload()
    value["竞品"] = [point(quote="This sentence is absent from the source.")] * 20
    return value


def test_removed_more_than_half_retries_whole_company(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([damaged(), payload()])
    result = run(conn, cfg, client)[0]
    assert result.status == "完成" and client.calls == 2
    assert result.removed_points == 20 and not result.quality_pending
    conn.close()


def test_second_validation_failure_shows_surviving_points_and_quality_pending(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([damaged(), damaged()])
    result = run(conn, cfg, client)[0]
    assert result.status == "完成" and result.quality_pending and result.removed_points == 40
    assert deep_row(conn)["quality_pending"] == 1
    assert result.result["竞品"] == deep.MISSING
    assert len(result.result["产品是什么"]) == 2 and client.calls == 2
    conn.close()


def test_failure_retry_and_validation_retry_share_second_call(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([{"error": "timeout"}, damaged(), payload()])
    result = run(conn, cfg, client)[0]
    assert result.status == "完成" and result.quality_pending and client.calls == 2
    conn.close()


@pytest.mark.parametrize("required", ["产品是什么", "融资"])
def test_required_empty_recovers_with_the_second_call(tmp_path, required):
    conn, cfg = database(tmp_path)
    seed(conn)
    value = payload()
    value[required] = []
    client = MockLLMClient([value, payload()])
    result = run(conn, cfg, client)[0]
    assert result.status == "完成" and result.error == "" and client.calls == 2
    assert json.loads(deep_row(conn)["result_json"])[required]
    conn.close()


def test_all_materials_fail_never_calls_model(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn, body="")
    transport = FixtureTransport(FIXTURE)
    transport.manifest = {"https://": {"status": 500}}
    client = MockLLMClient([payload()])
    result = run(conn, cfg, client, transport=transport)[0]
    assert result.status == "失败" and result.error == "all_pages_failed" and client.calls == 0
    conn.close()


def test_search_failure_adds_risk_notice_and_does_not_charge_search(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    transport = FixtureTransport(FIXTURE)
    transport.manifest[ENDPOINT] = {"status": 429}
    result = run(conn, cfg, MockLLMClient([payload()]), transport=transport)[0]
    assert result.status == "完成" and result.cost == 0
    assert result.result["风险"][-1]["text"] == deep.SEARCH_WARNING
    assert deep_row(conn)["search_count"] == 1
    conn.close()


def test_empty_search_results_do_not_add_failure_notice(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    transport = FixtureTransport(FIXTURE)
    transport.manifest[ENDPOINT] = {"body": '{"results":[]}'}
    result = run(conn, cfg, MockLLMClient([payload()]), transport=transport)[0]
    assert result.status == "完成" and result.cost == 0
    assert not any(point.get("text") == deep.SEARCH_WARNING for point in result.result["风险"])
    conn.close()


@pytest.mark.parametrize("first_failure", [True, False])
def test_remaining_less_than_30_seconds_does_not_send_second_model_call(tmp_path, first_failure):
    conn, cfg = database(tmp_path)
    seed(conn)
    clock = ManualClock()

    class SlowFetch(FixtureTransport):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url == "https://cedar.example/":
                clock.advance(40)
            return response

    class SlowModel(MockLLMClient):
        def complete_json(self, system, user, timeout_s):
            response = super().complete_json(system, user, timeout_s)
            clock.advance(120)
            return response

    client = SlowModel([{"error": "timeout"} if first_failure else damaged(), payload()])
    result = run(conn, cfg, client, clock, SlowFetch(FIXTURE))[0]
    assert result.status == "失败" and client.calls == 1
    assert deep_row(conn)["model_calls"] == 1
    conn.close()


def test_180_second_total_budget_caps_second_timeout_and_keeps_failure_state(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    clock = ManualClock()

    class TimedModel(MockLLMClient):
        def complete_json(self, system, user, timeout_s):
            response = super().complete_json(system, user, timeout_s)
            clock.advance(timeout_s)
            return response

    client = TimedModel([{"error": "timeout"}, payload()])
    result = run(conn, cfg, client, clock)[0]
    assert result.status == "失败" and client.calls == 2
    assert client.requests[0]["timeout_s"] == 120
    assert 59 <= client.requests[1]["timeout_s"] <= 60
    assert result.elapsed_s == pytest.approx(180, abs=0.05)
    assert deep_row(conn)["running"] == 0 and deep_row(conn)["model_calls"] == 2
    assert deep_row(conn)["online_at"] is None
    conn.close()


def test_calls_are_priced_by_each_start_time_when_retry_crosses_peak_boundary(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn, first="2026-10-30")
    clock = ManualClock("2026-10-30T08:59:59+08:00")

    class AdvanceModel(MockLLMClient):
        def complete_json(self, *args):
            result = super().complete_json(*args)
            clock.advance(2)
            return result

    usage = {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000}
    client = AdvanceModel([{"error": "timeout", "usage": usage}, {"content": payload(), "usage": usage}])
    result = run(conn, cfg, client, clock)[0]
    assert result.cost == pytest.approx(0.065)
    assert [entry["cost"] for entry in result.usage if entry["kind"] == "model"] == [0.02, 0.04]
    conn.close()


def test_forced_rerun_accumulates_cost_and_preserves_push_snapshot(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    client = MockLLMClient([{"content": payload(), "usage": usage}] * 2)
    first = run(conn, cfg, client)[0]
    snapshot = deep_row(conn)["push_judgement_json"]
    conn.execute("UPDATE company SET hq='New York'")
    conn.commit()
    second = run(conn, cfg, client, company_ids=["cedar"])[0]
    row = deep_row(conn)
    assert row["estimated_cost"] == pytest.approx(first.cost + second.cost)
    assert row["attempt_cost"] == second.cost and row["model_calls"] == 1
    assert row["push_judgement_json"] == snapshot
    assert len(json.loads(row["usage_json"])) == 4
    ledger = json.loads((cfg.state_dir / "test/deep-usage.json").read_text())["2026-10"]
    assert ledger["model_calls"] == 2 and ledger["search_count"] == 2
    assert ledger["estimated_cost"] == pytest.approx(row["estimated_cost"])
    assert ledger["days"]["2026-10-31"]["estimated_cost"] == pytest.approx(row["estimated_cost"])
    conn.close()


def test_default_selection_status_date_and_forced_ids(tmp_path):
    conn, cfg = database(tmp_path)
    for cid, status in (("new", None), ("failed", "失败"), ("stale", "生成中"), ("done", "完成"), ("timeout", "暂时没有"), ("skip", "不做"), ("busy", "生成中")):
        seed(conn, cid)
        if status:
            record(conn, cid, status, running=int(cid in {"stale", "busy"}), started="2026-10-31T10:00:00+08:00" if cid == "stale" else "2026-10-31T10:59:00+08:00")
    seed(conn, "old", first="2026-10-30")
    seed(conn, "hq-pending", status="总部待核")
    seed(conn, "round-pending", round_norm="待核")
    seed(conn, "filtered", filters=["总部非美国"])
    clock = ManualClock()
    chosen = deep.select_companies(conn, date(2026, 10, 31), now=clock.now())
    assert {company["id"] for company in chosen} == {"new", "failed", "stale"}
    chosen = deep.select_companies(conn, date(2026, 10, 31), now=clock.now(), company_ids=["done", "timeout", "skip", "busy", "old", "hq-pending", "filtered"])
    assert {company["id"] for company in chosen} == {"done", "timeout", "skip", "old"}
    with pytest.raises(ValueError):
        deep.select_companies(conn, date(2026, 10, 31), company_ids=["unknown"])
    conn.close()


def test_stale_running_after_exactly_180_seconds_is_reclaimed(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    record(conn, "cedar", "生成中", running=1, started="2026-10-31T10:57:00+08:00")
    assert run(conn, cfg, MockLLMClient([payload()]))[0].status == "完成"
    assert not deep_row(conn)["running"]
    conn.close()


def test_yc_batch_fold_is_not_done_even_with_company_override(tmp_path):
    conn, cfg = database(tmp_path)
    for i in range(31):
        seed(conn, f"yc-{i}", source_id="yc_directory", structured={"yc_batch": "Fall 2026"}, batch="Fall 2026")
    assert deep.select_companies(conn, date(2026, 10, 31)) == []
    client, transport = MockLLMClient([payload()]), FixtureTransport(FIXTURE)
    results = run(conn, cfg, client, transport=transport, company_ids=["yc-0"])
    assert [item.status for item in results] == ["不做"]
    assert not client.calls and not transport.request_log
    assert deep_row(conn, "yc-0")["estimated_cost"] == 0
    conn.close()


def test_deep_date_selects_admission_day_but_run_date_is_current_day(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    clock = ManualClock("2026-11-01T11:00:00+08:00")
    client = MockLLMClient([payload()])
    assert not run(conn, cfg, client, clock)
    result = run(conn, cfg, client, clock, deep_date=date(2026, 10, 31))[0]
    assert result.status == "完成" and deep_row(conn)["run_date"] == "2026-11-01"
    assert "2026-11" in json.loads((cfg.state_dir / "test/deep-usage.json").read_text())
    conn.close()


def test_publish_success_sets_online_time_and_publish_failure_leaves_it_empty(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    clock = ManualClock()

    def publish(conn, cfg, clock, company_id, run_date):
        assert deep_row(conn)["status"] == "完成"
        return clock.now() + timedelta(seconds=7)

    client = MockLLMClient([payload(), payload()])
    run(conn, cfg, client, clock, publish=publish)
    assert deep_row(conn)["online_at"] == "2026-10-31T11:00:07+08:00"

    def failed_publish(*args):
        raise RuntimeError("synthetic publishing failure")

    run(conn, cfg, client, clock, company_ids=["cedar"], publish=failed_publish)
    assert deep_row(conn)["status"] == "完成" and deep_row(conn)["online_at"] is None
    conn.close()


def test_five_companies_run_concurrently_with_shared_robots_cache(tmp_path):
    conn, cfg = database(tmp_path)
    for i in range(10):
        seed(conn, f"company-{i}")
    barrier = threading.Barrier(5)

    class ConcurrentModel(MockLLMClient):
        def __init__(self):
            super().__init__([payload()] * 10)
            self.active, self.peak, self.guard = 0, 0, threading.Lock()

        def complete_json(self, *args):
            with self.guard:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                barrier.wait(timeout=5)
                return super().complete_json(*args)
            finally:
                with self.guard:
                    self.active -= 1

    client, transport = ConcurrentModel(), FixtureTransport(FIXTURE)
    results = run(conn, cfg, client, transport=transport)
    assert client.peak == 5 and client.calls == 10
    assert len(results) == 10 and all(item.status == "完成" for item in results)
    assert sum(item["url"] == "https://cedar.example/robots.txt" for item in transport.request_log) == 1
    assert all(row[0] <= 2 for row in conn.execute("SELECT model_calls FROM deepdive"))
    conn.close()


def test_database_claim_blocks_a_second_connection_before_model_spend(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    company = deep.select_companies(conn, date(2026, 10, 31), now=ManualClock().now())[0]
    other = db.connect(cfg.db_path)
    assert deep._claim(conn, company, "2026-10-31", ManualClock())
    assert not deep._claim(other, company, "2026-10-31", ManualClock())
    assert not run(other, cfg, MockLLMClient([payload()]), company_ids=["cedar"])
    other.close()
    conn.close()


def test_claim_rechecks_terminal_status_after_company_was_selected(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    clock = ManualClock()
    delayed = deep.select_companies(conn, date(2026, 10, 31), now=clock.now())[0]
    assert run(conn, cfg, MockLLMClient([payload()]), clock)[0].status == "完成"
    assert not deep._claim(conn, delayed, "2026-10-31", clock)
    delayed["forced"] = True
    assert deep._claim(conn, delayed, "2026-10-31", clock)
    conn.close()


def test_late_result_only_records_cost_and_cannot_overwrite_new_claim(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    old_clock, clock = ManualClock("2026-10-31T10:56:00+08:00"), ManualClock()
    old = deep.select_companies(conn, date(2026, 10, 31), now=old_clock.now())[0]
    assert deep._claim(conn, old, "2026-10-31", old_clock)
    old_result = deep.DeepResult("cedar", "Cedar Labs", "完成", old_clock.now().isoformat(), clock.now().isoformat(),
                                 usage=[{"kind": "model", "cost": 0.02, "calls": 1, "started_at": old_clock.now().isoformat()}],
                                 result={"old": True}, run_token=old["run_token"])
    current = deep.select_companies(conn, date(2026, 10, 31), now=clock.now())[0]
    assert deep._claim(conn, current, "2026-10-31", clock)
    assert current["run_token"] != old["run_token"]
    assert not deep._save(conn, old_result, "2026-10-31")
    row = deep_row(conn)
    assert row["status"] == "生成中" and row["running"] == 1
    assert json.loads(row["result_json"]) == {} and row["estimated_cost"] == 0.02
    assert not deep._save(conn, old_result, "2026-10-31")
    assert deep_row(conn)["estimated_cost"] == 0.02
    new_result = deep.DeepResult("cedar", "Cedar Labs", "完成", clock.now().isoformat(), clock.now().isoformat(),
                                 usage=[{"kind": "model", "cost": 0.03, "calls": 1, "started_at": clock.now().isoformat()}],
                                 result={"new": True}, run_token=current["run_token"])
    assert deep._save(conn, new_result, "2026-10-31")
    assert deep_row(conn)["estimated_cost"] == pytest.approx(0.05)
    assert json.loads(deep_row(conn)["result_json"]) == {"new": True}
    deep.write_usage(conn, cfg)
    assert json.loads((cfg.state_dir / "test/deep-usage.json").read_text())["2026-10"]["estimated_cost"] == pytest.approx(0.05)
    conn.close()


@pytest.mark.parametrize("argv", [["--deep-date", "2026-10-31"], ["--deep-only", "--deep-date", "2026-02-30"], ["--deep-only", "--deep-final", "09:29"]])
def test_deep_cli_validation_before_writing(argv, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv + ["--config", str(tmp_path / "absent.toml")])
    assert exc.value.code == 2 and not list(tmp_path.iterdir())


def test_cli_runs_deep_at_manual_time_after_final_without_fetching_main(tmp_path, monkeypatch):
    conn, cfg = database(tmp_path)
    seed(conn)
    conn.close()
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    (fixture / "deep_responses.json").write_text(json.dumps([payload()]))
    config = tmp_path / "config.toml"
    config.write_text(f'state_dir = "{cfg.state_dir}"\ntest_mode = true\n')
    monkeypatch.setattr(pipeline.fetcher, "fetch_all", lambda *a, **kw: pytest.fail("不能抓主流程"))
    monkeypatch.setattr("vcradar.config.load_api_key", lambda *_: pytest.fail("不能读密钥"))
    args = ["--deep-only", "--fixture", str(fixture), "--dry-run", "--date", "2026-10-31", "--now", "11:00", "--deep-date", "2026-10-31", "--deep-deadline", "09:30", "--deep-final", "10:30", "--config", str(config)]
    assert cli.main(args) == 0
    conn = db.connect(cfg.db_path)
    assert deep_row(conn)["status"] == "完成"
    assert conn.execute("SELECT COUNT(*) FROM digest").fetchone()[0] == 0
    assert not cfg.outbox_dir.exists()
    conn.close()


def test_dry_run_without_fixture_uses_real_client_on_test_db(tmp_path, monkeypatch):
    """--dry-run 不带 --fixture = 真实取材 + 真实模型，只写测试库（与主流程口径一致）。"""
    conn, cfg = database(tmp_path)
    conn.close()
    calls = []
    monkeypatch.setattr("vcradar.config.load_api_key", lambda *a: calls.append("key") or None)
    client = deep._client(replace(cfg, test_mode=True), None)
    assert calls == ["key"]
    assert client is not None

