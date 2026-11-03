"""任务 L：必填不足重试及真实执行路径的离线回归。"""
import json

import pytest

from vcradar import deep
from vcradar.http import FixtureTransport
from vcradar.llm import MockLLMClient
from test_deep_support import FIXTURE, ManualClock, database, pages, seed
from test_deepquotes_sentences import numbered_payload


def insufficient(required):
    raw = numbered_payload()
    raw[required][-1]["quote_ids"] = ["p1.s999"]
    return raw


def run(conn, cfg, client, clock=None):
    return deep.run_for_today(conn, cfg, clock or ManualClock(), client=client,
                              transport=FixtureTransport(FIXTURE), link_records=[])[0]


@pytest.mark.parametrize("required,count,minimum", [("产品是什么", 1, 2), ("融资", 0, 1)])
def test_missing_required_points_below_half_retries_with_actionable_feedback(tmp_path, monkeypatch, required, count, minimum):
    monkeypatch.setattr("vcradar.config.load_api_key", lambda *_: pytest.fail("不得读取密钥"))
    conn, cfg = database(tmp_path)
    seed(conn)
    raw = insufficient(required)
    checked = deep.validate_result(raw, pages())
    assert not checked.ok and not checked.quality_pending and checked.removed_points == 1
    client = MockLLMClient([raw, numbered_payload()])
    output = run(conn, cfg, client)
    assert output.status == "完成" and output.model_calls == client.calls == 2
    assert output.removed_points == 1 and not output.quality_pending
    first, second = [json.loads(request["user"]) for request in client.requests]
    assert "previous_errors" not in first and first["pages"] == second["pages"]
    deleted, missing = second["previous_errors"]
    assert deleted["point"] == raw[required][-1]
    assert deleted["code"] == "quote_id_not_found" and deleted["reason"] == "id p1.s999 不存在"
    assert missing == {"section": required, "code": "required_empty",
                       "reason": f"{required}至少需要 {minimum} 条有原句的要点，现在 {count} 条"}
    row = conn.execute("SELECT * FROM deepdive").fetchone()
    assert row["status"] == "完成" and row["model_calls"] == 2
    assert row["removed_points"] == 1 and len(json.loads(row["removed_reasons_json"])) == 1
    conn.close()


@pytest.mark.parametrize("required", ["产品是什么", "融资"])
def test_second_result_missing_required_points_fails_without_third_call(tmp_path, required):
    conn, cfg = database(tmp_path)
    seed(conn)
    raw = insufficient(required)
    client = MockLLMClient([raw, raw, numbered_payload()])
    output = run(conn, cfg, client)
    assert output.status == "失败" and output.error == "required_empty"
    assert output.model_calls == client.calls == 2 and output.result == {}
    assert output.removed_points == 2 and not output.quality_pending
    row = conn.execute("SELECT * FROM deepdive").fetchone()
    assert row["status"] == "失败" and row["result_json"] == "{}" and row["model_calls"] == 2
    assert not (cfg.raw_dir / "2026-10-31/deep-cedar-call3.json").exists()
    conn.close()


def test_missing_both_required_sections_without_deletions_reports_both(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    raw = numbered_payload()
    raw["产品是什么"], raw["融资"] = [], []
    client = MockLLMClient([raw, numbered_payload()])
    output = run(conn, cfg, client)
    assert output.status == "完成" and client.calls == 2 and output.removed_points == 0
    errors = json.loads(client.requests[1]["user"])["previous_errors"]
    assert [entry["section"] for entry in errors] == ["产品是什么", "融资"]
    assert all(entry["code"] == "required_empty" and "现在 0 条" in entry["reason"] for entry in errors)
    conn.close()


def test_invalid_sections_also_get_required_item_feedback_and_retry(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([{"sections": []}, numbered_payload()])
    output = run(conn, cfg, client)
    assert output.status == "完成" and client.calls == 2
    errors = json.loads(client.requests[1]["user"])["previous_errors"]
    assert [entry["section"] for entry in errors] == ["产品是什么", "融资"]
    assert all(entry["code"] == "required_empty" for entry in errors)
    conn.close()


@pytest.mark.parametrize("removed", [0, 1, 7])
def test_satisfied_required_sections_without_deleted_majority_use_one_call(tmp_path, removed):
    conn, cfg = database(tmp_path)
    seed(conn)
    raw = numbered_payload()
    raw["竞品"] = [{"text": "来源说明。", "quote_ids": ["p1.s999"]} for _ in range(removed)]
    checked = deep.validate_result(raw, pages())
    assert checked.ok and checked.removed_points <= checked.total_points / 2
    client = MockLLMClient([raw, numbered_payload()])
    output = run(conn, cfg, client)
    assert output.status == "完成" and not output.quality_pending and client.calls == 1
    assert output.removed_points == removed
    conn.close()


def test_model_failure_and_missing_required_points_share_two_call_limit(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    client = MockLLMClient([{"error": "timeout"}, insufficient("产品是什么"), numbered_payload()])
    output = run(conn, cfg, client)
    assert output.status == "失败" and output.error == "required_empty" and client.calls == 2
    conn.close()


def test_missing_required_points_does_not_retry_with_less_than_thirty_seconds(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    clock = ManualClock()

    class SlowModel(MockLLMClient):
        def complete_json(self, *args):
            response = super().complete_json(*args)
            clock.advance(151)
            return response

    client = SlowModel([insufficient("产品是什么"), numbered_payload()])
    output = run(conn, cfg, client, clock)
    assert output.status == "失败" and output.error == "retry_time_exhausted" and client.calls == 1
    conn.close()


def test_current_company_context_reaches_point_validation_in_runner(tmp_path):
    conn, cfg = database(tmp_path)
    body = ("The startup builds tools for developers. "
            "Teams inspect application logs through a browser. "
            "The startup raised $3 million in a Seed round.")
    seed(conn, body=body)
    conn.execute("UPDATE company SET name='Dulo Labs AI Inc.' WHERE id='cedar'")
    conn.commit()
    raw = {"产品是什么": [
        {"text": "Dulo 提供开发工具。", "quote_ids": ["p1.s1"], "entities": [{"name": "DULO", "type": "公司"}]},
        {"text": "D.u-l o 通过浏览器检查日志。", "quote_ids": ["p1.s2"]},
    ], "融资": [{"text": "Dulo 完成种子轮融资 $3M。", "quote_ids": ["p1.s3"]}]}
    client = MockLLMClient([raw])
    output = run(conn, cfg, client)
    assert output.status == "完成" and client.calls == 1 and output.removed_points == 0
    assert len(output.result["产品是什么"]) == 2
    assert all("Dulo" not in item["quote"] for item in output.result["产品是什么"])
    conn.close()
