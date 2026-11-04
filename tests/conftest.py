

# 公开仓库：比对设计包字节的用例需要外部设计包 design-pack/（不随仓库发布），缺失时跳过。
def pytest_collection_modifyitems(config, items):
    import inspect
    from pathlib import Path
    import pytest
    if Path("design-pack").exists():
        return
    mark = pytest.mark.skip(reason="需要外部设计包 design-pack/，未随仓库发布")
    for item in items:
        try:
            src = inspect.getsource(item.function)
        except (OSError, TypeError):
            continue
        if "DESIGN" in src or "design-pack" in src:
            item.add_marker(mark)
