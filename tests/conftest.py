"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

from websearch_mcp import server
from websearch_mcp.cache import Cache


@pytest.fixture
def reset_cache(tmp_path, monkeypatch):
    """Изолированный кэш в tmp_path, чтобы тесты не трогали пользовательский.

    Заменяет кэш сервера на пустой и возвращает его прежним после теста.
    """
    original = server._cache
    monkeypatch.setattr(server, "_cache", Cache(tmp_path))
    yield server._cache
    monkeypatch.setattr(server, "_cache", original)
