import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", default=False, help="разрешить тесты реального LLM API")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="тест API включается ключом --live")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)
