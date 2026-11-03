"""P0 共 8 个信源、11 个地址（PRD 第 3 节）。任务 A 实现。"""
from __future__ import annotations

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


def sources_by_name() -> dict[str, list[SourceDef]]:
    """信源名 -> 地址列表（保持 order 顺序）。"""
    grouped: dict[str, list[SourceDef]] = {}
    for src in sorted(P0_SOURCES, key=lambda s: s.order):
        grouped.setdefault(src.name, []).append(src)
    return grouped
