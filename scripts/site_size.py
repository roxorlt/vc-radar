"""离线统计站点 JSON 的 UTF-8 字节；只输出字段名、数量和大小。"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def json_size(value) -> int:
    """返回紧凑 JSON 编码后的 UTF-8 字节数。"""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def report(path: Path) -> None:
    """按顶层键和对象类别统计大小，不输出业务正文。"""
    data = json.loads(path.read_bytes())
    total = json_size(data)
    print(f"文件：{path}\n文件字节：{path.stat().st_size:,}；紧凑 JSON 字节：{total:,}")
    print("顶层键（含键名和冒号，另有外层括号和逗号）：")
    for key, value in sorted(data.items(), key=lambda item: json_size(item[1]), reverse=True):
        size = json_size(key) + 1 + json_size(value)
        print(f"  {key}: {size:,} 字节，{size / total:.2%}")

    events = data.get("events", [])
    companies = data.get("companies", [])
    deepdives = [c["deepdive"] for c in companies if "deepdive" in c]
    categories = {
        "events": events,
        "companies": companies,
        "events.sources": [s for e in events for s in e.get("sources", [])],
        "companies.sources": [s for c in companies for s in c.get("sources", [])],
        "companies.deepdive": deepdives,
        "deepdive.items": [item for d in deepdives for item in d.get("items", [])],
    }
    print("对象类别（嵌套类别会重复计入，仅用于定位）：")
    for name, objects in categories.items():
        size = sum(json_size(obj) for obj in objects)
        print(f"  {name}: {len(objects)} 个，{size:,} 字节，{size / total:.2%}")
        fields = Counter()
        for obj in objects:
            for key, value in obj.items():
                fields[key] += json_size(key) + 1 + json_size(value)
        print("    " + "；".join(f"{key} {size:,}" for key, size in fields.most_common(5)))
    details = sorted((path.parent / "c").glob("*.json"))
    if details:
        sizes = [p.stat().st_size for p in details]
        print(f"按需详情：{len(sizes)} 个文件，合计 {sum(sizes):,} 字节，最大 {max(sizes):,} 字节")


def main() -> None:
    """从命令行读取一个或多个待统计的站点快照。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path,
                        default=[Path("/srv/vc-radar/state/site/data.json")])
    for path in parser.parse_args().paths:
        report(path)


if __name__ == "__main__":
    main()
