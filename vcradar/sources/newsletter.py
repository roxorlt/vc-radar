"""通讯融资段的字段提取，不推断原文没有的值。"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from ..models import RawItem
from .common import HTMLLine


def displayed_date(lines: list[HTMLLine]) -> str | None:
    """读取页面可见日期，和机器发布时间独立存储。"""
    for line in lines:
        if len(line.text) > 75:
            continue
        match = re.search(r"\b([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\b", line.text)
        if match:
            value = re.sub(r",?\s+(\d{4})$", r", \1", match[1])
            for fmt in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    return datetime.strptime(value, fmt).date().isoformat()
                except ValueError:
                    pass
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line.text):
            try:
                return datetime.fromisoformat(line.text).date().isoformat()
            except ValueError:
                pass
    return None


def _names(text):
    out = []
    for name in re.split(r",\s*|\s+and\s+", text):
        name = re.sub(r"^(?:and|with|previous investors?)\s+", "", name.strip(" ,.;"), flags=re.I).strip(" ,.;")
        name = re.sub(r"\s+(?:also\s+.*|as well as.*)$", "", name).strip(" ,.;")
        if name:
            out.append(name)
    return out


def deal(line: HTMLLine, source_id: str, issue_url: str, published_at,
         original_date_et: str | None = None, require_based=False) -> RawItem | None:
    """从公司开头且含 raised/raising 的段落中抽取一笔交易。"""
    text = re.sub(r"^[\s•●\-–—]+", "", line.text).strip()
    match = re.match(r"(?P<company>[^,]{1,120}),\s+(?:a|an)\s+(?P<description>.+)", text, re.I)
    if not match or not re.search(r"\b(?:raised|raising)\b", match["description"], re.I):
        return None
    company = match["company"].strip()
    description = match["description"]
    hq = None
    based = re.match(r"(?:(?:[\w-]+|\d+)-year-old\s+)?(?P<city>[^,]{1,70}?(?:,\s*[A-Z][\w.]*(?:\s[A-Z][\w.]*)?)?)-based\b", description, re.I)
    based_in = re.match(r"(?:(?:[\w-]+|\d+)-year-old\s+)?(?:startup|company)\s+based\s+in\s+(?P<city>[A-Z][^,]{1,40}(?:,\s*[A-Z][\w.]*)?)", description)
    if based:
        hq = based["city"].strip()
    elif based_in:
        hq = based_in["city"].strip()
    else:
        located = re.match(r"(?:(?:[\w-]+|\d+)-year-old\s+)?(.+?)\s+(?:startup|company|business|firm)\b", description, re.I)
        if located:
            hq = located[1].strip()
    if require_based and (not based or not re.search(r"\braised\s+\$", text, re.I)):
        return None
    amount = re.search(r"\b(?:raised|raising)\s+(?:an?\s+)?(?:(?:about|approximately|another|more than|at least)\s+)?([$€£]\s?\d[\d,.]*(?:\s?(?:billion|million|thousand|[BMK])\b)?)", text, re.I)
    financing_text = re.split(r"\b(?:raised|raising)\b", text, maxsplit=1, flags=re.I)[-1]
    round_match = re.search(r"\b(pre[- ]seed|seed|angel|Series\s+[A-Z](?:\d)?(?:\s*(?:extension|round))?)\b", financing_text, re.I)
    lead_match = re.search(r"\bled\s+by\s+(.+?)(?=\s+(?:and\s+(?:was\s+)?joined|with\s+participation|with\s+additional)|,\s+with\b|,\s+alongside\b|[.;]|$)", text, re.I)
    if not lead_match:
        lead_match = re.search(r"(?:^|[.!?]\s+)([^.!?]+?)\s+(?:co-)?led the (?:round|deal|investment)\b", text, re.I)
    follows_match = re.search(r"\b(?:joined by|participation from)\s+(.+?)(?=[.;]|$)", text, re.I)
    structured = {"company_name": company, "issue_url": issue_url, "hq_text": hq,
                  "round_text": round_match[1] if round_match else None,
                  "amount_text": amount[1] if amount else None,
                  "leads": _names(lead_match[1]) if lead_match else [],
                  "follows": _names(follows_match[1]) if follows_match else []}
    for href, label in reversed(line.links):
        if re.search(r"\b(?:more\s+)?here\b", label, re.I):
            link = urljoin(issue_url, href)
            if urlsplit(link).scheme in {"http", "https"}:
                structured["original_link"] = link
            break
    return RawItem(source_id, issue_url + "#" + company, issue_url, company, published_at,
                   text[:1000], text[:8000], original_date_et, structured)


def unique_deals(lines, source_id, issue_url, published_at, original_date_et=None, require_based=False):
    """一家公司在同一期只取一次；同段多笔时按下一句的公司开头拆分。"""
    items = []
    seen = set()
    for line in lines:
        pieces = re.split(r"(?<=[.!?])\s+(?=[A-Z][^,.!?]{0,100},\s+(?:a|an)\s+)", line.text)
        for piece in pieces:
            item = deal(HTMLLine(piece, line.links, line.heading), source_id, issue_url, published_at,
                        original_date_et, require_based)
            if item and item.structured["company_name"].casefold() not in seen:
                seen.add(item.structured["company_name"].casefold())
                items.append(item)
    return items
