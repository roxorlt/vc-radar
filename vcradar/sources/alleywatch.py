"""AlleyWatch 仅收单家公司融资帖，汇总帖保留计数。"""
import re

from .rss import parse as _parse


def parse(response, source_id="alleywatch"):
    """解析 RSS 并为日报、周报、月报和其他帖标记 skip_reason。"""
    result = _parse(response, source_id)
    for item in result.items:
        title = item.title
        if re.search(r"\bMonthly\b|Funding Report for\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\b", title, re.I):
            kind = "monthly"
        elif re.search(r"\bWeekly\b", title, re.I):
            kind = "weekly"
        elif re.search(r"\bFunding Report\b|\bDaily\b", title, re.I):
            kind = "daily"
        elif len(re.findall(r"\bRaises?\s+\$", title, re.I)) == 1 and re.match(r"^.+?\s+Raises?\s+\$", title, re.I):
            company = re.split(r"\s+Raises?\s+\$", title, flags=re.I)[0]
            kind = "daily" if re.search(r"\s(?:and|&)\s", company, re.I) else "single"
        else:
            kind = "other"
        item.structured["kind"] = kind
        if kind != "single":
            item.structured["skip_reason"] = "非单家融资帖"
    return result
