"""YC 公司目录的公开配置、批次和查询响应解析；不记录搜索 key。"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from ..models import ErrorType, RawItem
from .common import Parsed, json_body


def options(response):
    """读取页面公开 Algolia 配置；读不到返回 None。"""
    match = re.search(r"window\.AlgoliaOpts\s*=\s*(\{.*?\});", response.body, re.S)
    if not match:
        return None
    try:
        value = json.loads(match[1])
        if all(isinstance(value.get(k), str) and value[k] for k in ("app", "key")) and re.fullmatch(r"[A-Za-z0-9]+", value["app"]):
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def batch_order(batch: str) -> tuple[int, int]:
    """按年份和季节排列现代及 W24/S24 批次。"""
    match = re.fullmatch(r"(Winter|Spring|Summer|Fall|Autumn)\s+(\d{4})", batch, re.I)
    if match:
        return int(match[2]), {"winter": 0, "spring": 1, "summer": 2, "fall": 3, "autumn": 3}[match[1].lower()]
    match = re.fullmatch(r"([WS])(\d{2}|\d{4})", batch, re.I)
    if match:
        year = int(match[2])
        return (2000 + year if year < 100 else year), (0 if match[1].lower() == "w" else 2)
    return -1, -1


def latest_batches(response) -> list[str]:
    """从 facets 查询结果取最近四批；未知批次格式忽略。"""
    try:
        batches = json_body(response)["results"][0]["facets"]["batch"]
        return sorted((b for b in batches if batch_order(b) != (-1, -1)), key=batch_order, reverse=True)[:4]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError("批次列表无法读取") from exc


def query_body(batch: str | None = None) -> bytes:
    """构造 facets 或单批查询的 POST JSON。"""
    params = {"query": "", "hitsPerPage": 0 if batch is None else 1000}
    if batch is None:
        params["facets"] = json.dumps(["batch"], separators=(",", ":"))
    else:
        params["facetFilters"] = json.dumps([["batch:" + batch]], separators=(",", ":"))
    return json.dumps({"requests": [{"indexName": "YCCompany_production", "params": urlencode(params)}]}).encode()


def parse(response, source_id="yc_directory") -> Parsed:
    """解析一个批次查询；没有 hits 或 nbHits 为零按读不到处理。"""
    try:
        result = json_body(response)["results"][0]
        if int(result["nbHits"]) <= 0 or not result["hits"]:
            raise ValueError
        items = []
        for hit in result["hits"]:
            items.append(RawItem(source_id, str(hit["objectID"]), "https://www.ycombinator.com/companies/" + hit["slug"],
                                 hit.get("name", ""), None, hit.get("one_liner", "")[:1000],
                                 structured={"company_name": hit.get("name"), "website": hit.get("website"),
                                             "location": hit.get("all_locations"), "yc_batch": hit.get("batch"),
                                             "one_liner": hit.get("one_liner"), "launched_at": hit.get("launched_at"),
                                             "tags": hit.get("tags", []), "team_size": hit.get("team_size")}))
        return Parsed(items)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return Parsed(error_type=ErrorType.YC_UNREADABLE, error_summary="公司目录查询无法读取")


def total(response) -> int:
    """读取批次的 nbHits 总数。"""
    return int(json_body(response)["results"][0]["nbHits"])
