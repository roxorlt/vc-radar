"""历史提示为空时应交给页面使用当前计数和错误提示。"""
import pytest

from vcradar import wording


@pytest.mark.parametrize("text", ["", " \t", "另有 3 家总部待核 · "])
def test_absent_legacy_copy_is_not_an_undisclosed_amount(text):
    """空白或取消的国家提示不应生成金额占位文案。"""
    assert wording.legacy_to_display(text) == ""
