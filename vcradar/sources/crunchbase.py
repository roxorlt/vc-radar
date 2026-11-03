"""Crunchbase News 栏目的 RSS 解析。"""
from .rss import parse as _parse


def parse(response, source_id="cbn_main"):
    """解析 Crunchbase News RSS。"""
    return _parse(response, source_id)
