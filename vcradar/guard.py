"""交付数据的固定禁用名与占位地址检查。"""
from __future__ import annotations


def check_no_fictional(text: str) -> list[str]:
    """返回命中的禁用名，忽略大小写，同一名字只返回一次。"""
    lowered = text.casefold()
    return [name for name in ("Quorvia", "Tessaline") if name.casefold() in lowered]


def check_no_placeholder(text: str) -> list[str]:
    """返回命中的占位地址标记，忽略大小写，同一标记只返回一次。"""
    lowered = text.casefold()
    return [marker for marker in ("vc.example.invalid", "REPLACE_RANDOM_PATH")
            if marker.casefold() in lowered]

