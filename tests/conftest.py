"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

from websearch_mcp import server
from websearch_mcp.cache import Cache


@pytest.fixture
def reset_cache(tmp_path, monkeypatch):
    """Изолированный кэш в tmp_path, чтобы тесты не трогали пользовательский.

    Кэш в сервере создаётся лениво, поэтому подменяется именно привязанное
    состояние: сам объект, признак ошибки и счётчик чистки.
    """
    monkeypatch.setattr(server, "_cache", Cache(tmp_path))
    monkeypatch.setattr(server, "_cache_error", None)
    return server._cache
