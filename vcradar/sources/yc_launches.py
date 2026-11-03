"""YC Launches JSON 解析。"""
from ..models import ErrorType, RawItem
from .common import Parsed, json_body, newest, parse_time, strip_html


def parse(response, source_id="yc_launches"):
    """将 launches hits 转换为条目，不依赖 search_path 的写法。"""
    try:
        payload = json_body(response)
        hits = payload["hits"]
        if not isinstance(hits, list):
            raise ValueError
        items = []
        for hit in hits:
            company = hit.get("company") or {}
            item = RawItem(source_id, str(hit["id"]), "https://www.ycombinator.com/launches/" + hit["slug"],
                           hit.get("title", ""), parse_time(hit.get("created_at")),
                           strip_html(hit.get("tagline", ""))[:1000], strip_html(hit.get("tagline", ""))[:8000],
                           structured={"company_name": company.get("name"), "website": company.get("url"),
                                       "yc_batch": company.get("batch"), "tags": company.get("tags", []),
                                       "one_liner": hit.get("tagline", "")})
            items.append(item)
        return Parsed(items, newest(items))
    except (KeyError, TypeError, ValueError, AttributeError):
        return Parsed(error_type=ErrorType.INTERCEPTED, error_summary="Launches JSON 无法解析")
