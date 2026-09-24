from pathlib import Path

import pytest

from modeltrace_status.config import Monitor, Settings
from modeltrace_status.storage import Store


@pytest.fixture
def settings(tmp_path):
    monitor = Monitor("one", "Example", "gpt-6-astra", "gpt-6-astra", "http://127.0.0.1:9999/v1", "TEST_KEY")
    return Settings(upstream=Path(__file__).resolve().parents[2], data_dir=tmp_path / "data", codex="codex", monitors=(monitor,))


@pytest.fixture
def store(settings):
    store = Store(settings.db_path)
    store.seed(settings, {"one": "secret-sentinel-do-not-publish"})
    return store
