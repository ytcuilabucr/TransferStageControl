import pytest


def pytest_addoption(parser):
    parser.addoption("--hardware", action="store_true", default=False, help="run tests marked @pytest.mark.hardware")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--hardware"):
        return
    skip = pytest.mark.skip(reason="needs hardware (run with --hardware)")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)
