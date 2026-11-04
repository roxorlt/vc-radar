"""YC 公司目录的公开配置、批次和查询响应解析；不记录搜索 key。"""
from __future__ import annotations

from datetime import date
import json
import re
from urllib.parse import urlencode

from ..models import ErrorType, RawItem
from .common import Parsed, json_body

_SHORT_SEASONS = {"w": 0, "x": 1, "sp": 1, "s": 2, "f": 3, "a": 3}


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
    """按年份和季节排列现代四批及 W24/X26/S24/F26 简写批次。"""
    text = str(batch).strip()
    match = re.fullmatch(r"(Winter|Spring|Summer|Fall|Autumn)\s+(\d{4})", text, re.I)
    if match:
        return int(match[2]), {"winter": 0, "spring": 1, "summer": 2, "fall": 3, "autumn": 3}[match[1].lower()]
    match = re.fullmatch(r"(SP|[WSXFA])\s*(\d{2}|\d{4})", text, re.I)
    if match:
        year = int(match[2])
        return (2000 + year if year < 100 else year), _SHORT_SEASONS[match[1].lower()]
    return -1, -1


def batch_in_window(batch: str, latest_yc_batches: list[str]) -> bool:
    """按规范化批次（兼容 W26/X26/S26/F26 与全称）判断是否在最近四批内。"""
    text = str(batch).strip()
    if not text or not latest_yc_batches:
        return True
    window = latest_yc_batches[:4]
    if text.casefold() in {name.strip().casefold() for name in window}:
        return True
    order = batch_order(text)
    if order == (-1, -1):
        return False
    return order in {batch_order(name.strip()) for name in window if batch_order(name.strip()) != (-1, -1)}


def canonical_latest_batches(names, *, as_of: date | None = None) -> list[str]:
    """按 (year, season) 去重取最近四批，同季优先保留全称（如 Fall 2026 优先于 F26）。"""
    by_order: dict[tuple[int, int], str] = {}
    for raw in names:
        text = str(raw).strip()
        if not text:
            continue
        order = batch_order(text)
        if order == (-1, -1):
            continue
        existing = by_order.get(order)
        if existing is None or len(text) > len(existing):
            by_order[order] = text
    orders = sorted(by_order, reverse=True)
    if as_of is not None:
        ceiling = (as_of.year, (as_of.month - 1) // 3)
        next_season = (ceiling[0] + 1, 0) if ceiling[1] == 3 else (ceiling[0], ceiling[1] + 1)
        eligible = [o for o in orders if o <= ceiling or o == next_season]
        if eligible:
            orders = eligible
    return [by_order[o] for o in orders[:4]]


def latest_batches(response, *, as_of: date | None = None) -> list[str]:
    """从 facets 查询结果取最近四批；未知批次格式忽略；传入 as_of 时排除未来未开营占位批次。"""
    try:
        batches = json_body(response)["results"][0]["facets"]["batch"]
        valid = [b for b in batches if batch_order(b) != (-1, -1)]
        if as_of is not None:
            ceiling = (as_of.year, (as_of.month - 1) // 3)
            next_season = (ceiling[0] + 1, 0) if ceiling[1] == 3 else (ceiling[0], ceiling[1] + 1)
            eligible = [
                b for b in valid
                if batch_order(b) <= ceiling
                or (batch_order(b) == next_season and isinstance(batches.get(b), int) and batches[b] >= 30)
            ]
            if eligible:
                valid = eligible
        return sorted(valid, key=batch_order, reverse=True)[:4]
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
