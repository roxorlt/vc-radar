"""TechCrunch 栏目的 RSS 解析。"""
from .rss import parse as _parse


def parse(response, source_id="tc_venture"):
    """解析 TechCrunch RSS。"""
    return _parse(response, source_id)
