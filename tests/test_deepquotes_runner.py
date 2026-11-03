"""任务 K：编号材料、带原因重试、审计落盘均使用离线合成返回。"""
import json
from dataclasses import replace

import pytest

from vcradar import db, deep
from vcradar.config import Config
from vcradar.deep_fetch import Materials, split_sentences
from vcradar.http import FixtureTransport
from vcradar.llm import LLMResult, MockLLMClient
from test_deep_support import FIXTURE, ManualClock, database, pages, seed
from test_deepquotes_sentences import numbered_payload


def damaged_payload():
    """保留必填事实，增加超过一半的无效引用。"""
    result = numbered_payload()
    result["竞品"] = [{"text": "DELETED_PRIVATE_POINT_TEXT", "quote_ids": ["p1.s999"]} for _ in range(12)]
    return result


def run(conn, cfg, client, **kwargs):
    return deep.run_for_today(conn, cfg, ManualClock(), client=client,
                              transport=FixtureTransport(FIXTURE), link_records=[], **kwargs)


def test_retry_sends_deleted_points_and_reasons_but_keeps_numbered_materials(tmp_path, monkeypatch):
    monkeypatch.setattr("vcradar.config.load_api_key", lambda *_: pytest.fail("不得读取密钥"))
    conn, cfg = database(tmp_path)
    seed(conn)
    damaged = damaged_payload()
    client = MockLLMClient([damaged, numbered_payload()])
    output = run(conn, cfg, client)[0]
    first, second = [json.loads(item["user"]) for item in client.requests]
    assert output.status == "完成" and not output.quality_pending
    assert client.calls == 2 and output.removed_points == 12
    assert "previous_errors" not in first
    assert first["pages"] == second["pages"]
    assert all("body" not in page and page["sentences"] for page in first["pages"])
    assert all(sum(len(sentence["text"]) for sentence in page["sentences"]) <= 8000 for page in first["pages"])
    errors = second["previous_errors"]
    assert len(errors) == 12
    assert errors[0]["point"] == damaged["竞品"][0]
    assert errors[0]["reason"] == "id p1.s999 不存在"
    assert errors[0]["section"] == "竞品" and errors[0]["code"] == "quote_id_not_found"
    assert "quote_ids" in client.requests[0]["system"] and "previous_errors" in client.requests[1]["system"]
    conn.close()


def test_second_call_still_over_half_keeps_valid_points_and_audits_both_calls(tmp_path, capsys):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([damaged_payload(), damaged_payload(), numbered_payload()])
    output = run(conn, cfg, client)[0]
    row = conn.execute("SELECT * FROM deepdive").fetchone()
    assert output.status == row["status"] == "完成" and output.quality_pending
    assert output.removed_points == row["removed_points"] == 24
    assert client.calls == row["model_calls"] == 2
    assert len(output.result["产品是什么"]) == 2 and output.result["竞品"] == deep.MISSING
    reasons = json.loads(row["removed_reasons_json"])
    assert reasons == output.removed_reasons and len(reasons) == 24
    assert {item["call"] for item in reasons} == {1, 2}
    log = (cfg.log_dir / "deep-2026-10-31.log").read_text()
    removed = [item for line in log.splitlines() if (item := json.loads(line))["event"] == "deep_removed"]
    assert len(removed) == 24 and all(item["company_id"] == "cedar" for item in removed)
    assert all(item["section"] == "竞品" and "p1.s999" in item["reason"] for item in removed)
    assert "DELETED_PRIVATE_POINT_TEXT" not in log and "DELETED_PRIVATE_POINT_TEXT" not in row["removed_reasons_json"]
    assert "删除原因 Top3 引用编号不存在 ×24" in capsys.readouterr().out
    for attempt in (1, 2):
        path = cfg.raw_dir / "2026-10-31" / f"deep-cedar-call{attempt}.json"
        assert json.loads(path.read_text()) == damaged_payload()
    assert not (cfg.raw_dir / "2026-10-31/deep-cedar-call3.json").exists()
    conn.close()


def test_top_three_reasons_group_by_failure_kind_with_counts(tmp_path, capsys):
    conn, cfg = database(tmp_path)
    seed(conn)
    raw = numbered_payload()
    raw["风险"] = [
        {"text": "事实。", "quote_ids": ["p1.s999"]},
        {"text": "有 999 个客户。", "quote_ids": ["p1.s1"]},
        {"text": "Invented Person 是创始人。", "quote_ids": ["p1.s1"]},
        {"text": "引用倒序。", "quote_ids": ["p1.s2", "p1.s1"]},
    ]
    output = run(conn, cfg, MockLLMClient([raw]))[0]
    assert output.status == "完成" and output.model_calls == 1 and output.removed_points == 4
    stdout = capsys.readouterr().out
    assert "引用编号不存在 ×1；数字不在所引原句里 ×1；人名或机构名不在所引原句里 ×1" in stdout
    assert "引用编号不连续或顺序错误 ×1" not in stdout
    conn.close()


@pytest.mark.parametrize("required", ["产品是什么", "融资"])
def test_empty_required_section_fails_after_second_validation_even_if_quality_is_pending(tmp_path, required):
    conn, cfg = database(tmp_path)
    seed(conn)
    first, second = damaged_payload(), damaged_payload()
    second[required] = []
    client = MockLLMClient([first, second, numbered_payload()])
    output = run(conn, cfg, client)[0]
    assert output.status == "失败" and output.error == "required_empty" and client.calls == 2
    assert output.quality_pending and output.result == {}
    assert len(json.loads(conn.execute("SELECT removed_reasons_json FROM deepdive").fetchone()[0])) == 24
    conn.close()


@pytest.mark.parametrize("required", ["产品是什么", "融资"])
def test_empty_required_section_without_deleted_majority_fails_after_second_call(tmp_path, required):
    conn, cfg = database(tmp_path)
    seed(conn)
    raw = numbered_payload()
    raw[required] = []
    client = MockLLMClient([raw, raw, numbered_payload()])
    output = run(conn, cfg, client)[0]
    assert output.status == "失败" and output.error == "required_empty" and client.calls == 2
    conn.close()


def test_failure_retry_consumes_second_call_without_a_third_validation_retry(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([{"error": "http_429"}, damaged_payload(), numbered_payload()])
    output = run(conn, cfg, client)[0]
    assert output.status == "完成" and output.quality_pending and client.calls == 2
    assert all(item["call"] == 2 for item in output.removed_reasons)
    assert "previous_errors" not in json.loads(client.requests[1]["user"])
    conn.close()


@pytest.mark.parametrize("test_mode", [True, False])
@pytest.mark.parametrize("company_id", ["cedar", "合成公司"])
def test_raw_output_keeps_exact_text_in_configured_raw_directory(tmp_path, test_mode, company_id):
    cfg = Config(state_dir=tmp_path / "state", test_mode=test_mode)
    conn = db.connect(cfg.db_path)
    seed(conn, company_id)
    malformed = '{"unfinished":\n  '
    valid = json.dumps(numbered_payload(), ensure_ascii=False, indent=3) + "\n"
    client = MockLLMClient([LLMResult(False, content=malformed, error="bad_json"), LLMResult(True, content=valid)])
    output = run(conn, cfg, client)[0]
    assert output.status == "完成" and output.model_calls == 2
    raw = cfg.raw_dir / "2026-10-31"
    assert (raw / f"deep-{company_id}-call1.json").read_text() == malformed
    assert (raw / f"deep-{company_id}-call2.json").read_text() == valid
    other = cfg.state_dir / ("raw" if test_mode else "test/raw")
    assert not other.exists()
    conn.close()


def test_deep_entry_migrates_legacy_reasons_column_idempotently_and_preserves_rows(tmp_path, monkeypatch):
    conn, cfg = database(tmp_path)
    seed(conn)
    original = json.dumps({"retained": "legacy result"})
    conn.execute("INSERT INTO deepdive(company_id, run_date, status, removed_points, result_json) "
                 "VALUES ('cedar', '2026-10-30', '完成', 9, ?)", (original,))
    conn.commit()
    monkeypatch.setattr(deep, "_client", lambda *args: pytest.fail("无待处理公司时不能构造客户端"))
    before = dict(conn.execute("SELECT * FROM deepdive").fetchone())
    assert "removed_reasons_json" not in before
    for _ in range(2):
        assert deep.run_for_today(conn, cfg, ManualClock()) == []
        after = dict(conn.execute("SELECT * FROM deepdive").fetchone())
        assert after.pop("removed_reasons_json") == "[]" and after == before
    conn.close()
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT removed_reasons_json FROM deepdive").fetchone()[0] == "[]"
    conn.close()


def test_rerun_replaces_attempt_reasons(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([damaged_payload(), damaged_payload(), numbered_payload()])
    run(conn, cfg, client)
    run(conn, cfg, client, company_ids=["cedar"])
    row = conn.execute("SELECT * FROM deepdive").fetchone()
    assert row["removed_points"] == 0 and row["removed_reasons_json"] == "[]"
    conn.close()


def test_storage_deduplication_remaps_sentence_ids_with_the_page_number(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    clock = ManualClock()
    company = deep.select_companies(conn, clock.now().date(), now=clock.now())[0]
    assert deep._claim(conn, company, "2026-10-31", clock)
    first = pages()[0]
    duplicate = replace(first, page_no=2, url="https://www.news.example/cedar/?utm_source=test")
    raw = numbered_payload()
    raw["产品是什么"][0]["quote_ids"] = ["p2.s1", "p2.s2"]
    checked = deep.validate_result(raw, [first, duplicate])
    assert checked.ok
    output = deep.DeepResult("cedar", "Cedar Labs", "完成", clock.now().isoformat(), clock.now().isoformat(),
                             materials=Materials(pages=[first, duplicate]), result=checked.result,
                             run_token=company["run_token"])
    assert deep._save(conn, output, "2026-10-31")
    point = json.loads(conn.execute("SELECT result_json FROM deepdive").fetchone()[0])["产品是什么"][0]
    assert point["page"] == 1 and point["quote_ids"] == ["p1.s1", "p1.s2"]
    sentences = {item["id"]: item["text"] for item in split_sentences(first.body, 1)}
    assert point["quote"] == " ".join(sentences[ident] for ident in point["quote_ids"])
    assert conn.execute("SELECT COUNT(*) FROM deepdive_pages").fetchone()[0] == 1
    conn.close()
