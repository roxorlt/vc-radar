"""P0 共 8 个信源、11 个地址（PRD 第 3 节）。任务 A 实现。"""
from __future__ import annotations

from urllib.parse import urlsplit

from ..models import SourceDef

P0_SOURCES: list[SourceDef] = [
    SourceDef("ts_web", "Term Sheet", "网页版", "https://content.fortune.com/wp-json/wp/v2/posts?newsletter=7249411", "api", 2, 0, True),
    SourceDef("tc_venture", "TechCrunch", "venture", "https://techcrunch.com/category/venture/feed/", "rss", 2, 1),
    SourceDef("tc_startups", "TechCrunch", "startups", "https://techcrunch.com/category/startups/feed/", "rss", 2, 2),
    SourceDef("cbn_main", "Crunchbase News", "main", "https://news.crunchbase.com/feed/", "rss", 4, 3),
    SourceDef("cbn_seed", "Crunchbase News", "seed", "https://news.crunchbase.com/sections/seed/feed/", "rss", 4, 4),
    SourceDef("cbn_venture", "Crunchbase News", "venture", "https://news.crunchbase.com/sections/venture/feed/", "rss", 4, 5),
    SourceDef("yc_launches", "YC Launches", "launches", "https://www.ycombinator.com/launches", "api", 4, 6),
    SourceDef("yc_directory", "YC 公司目录", "companies", "https://www.ycombinator.com/companies", "page", None, 7),
    SourceDef("launch_hn", "Launch HN", "launch", "https://hn.algolia.com/api/v1/search_by_date?query=%22Launch%20HN%22&tags=story&hitsPerPage=50", "api", 30, 8),
    SourceDef("strictlyvc", "StrictlyVC", "newsletter", "https://newsletter.strictlyvc.com/sitemap.xml", "page", 2, 9),
    SourceDef("alleywatch", "AlleyWatch", "funding", "https://www.alleywatch.com/category/funding/feed/", "rss", 2, 10),
]

# 展示顺序不使用抓取优先级；Term Sheet 仍然最先抓、最后显示。
DISPLAY_ORDER = ("TechCrunch", "Crunchbase News", "YC Launches", "YC 公司目录",
                 "Launch HN", "StrictlyVC", "AlleyWatch", "Term Sheet")
_DISPLAY_NAMES = {source.id: source.name for source in P0_SOURCES}
_DISPLAY_RANKS = {source_id: DISPLAY_ORDER.index(name) for source_id, name in _DISPLAY_NAMES.items()}


def display_rank(source_id: str) -> int:
    """返回条目所属信源的展示序号；未知信源排在最后。"""
    return _DISPLAY_RANKS.get(source_id, len(DISPLAY_ORDER))


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_URL_NAMES = sorted(((_host(s.url), urlsplit(s.url).path.rstrip("/"), s.name) for s in P0_SOURCES),
                    key=lambda item: -len(item[1]))


def name_for_url(url: str) -> str | None:
    """按信源表地址认出页面所属信源：先按站点加路径前缀，再按只属于一个信源的站点；认不出返回 None。"""
    try:
        host, path = _host(url), urlsplit(url).path
    except ValueError:
        return None
    for source_host, prefix, name in _URL_NAMES:
        if prefix and host == source_host and (path == prefix or path.startswith(prefix + "/")):
            return name
    names = {name for source_host, _, name in _URL_NAMES if source_host == host}
    return names.pop() if len(names) == 1 else None


def display_name(source_id: str) -> str:
    """返回信源名，未知信源保留其编号。"""
    return _DISPLAY_NAMES.get(source_id, source_id)


def sources_by_name() -> dict[str, list[SourceDef]]:
    """信源名 -> 地址列表（保持 order 顺序）。"""
    grouped: dict[str, list[SourceDef]] = {}
    for src in sorted(P0_SOURCES, key=lambda s: s.order):
        grouped.setdefault(src.name, []).append(src)
    return grouped
