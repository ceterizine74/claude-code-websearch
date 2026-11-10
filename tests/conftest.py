"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

from websearch_mcp import netguard, server
from websearch_mcp.cache import Cache


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Подменить резолв имён публичным адресом, чтобы тесты не зависели от DNS.

    ``netguard`` проверяет IP, в который разрешается хост: иначе достаточно
    зарегистрировать ``evil.example`` → ``127.0.0.1``, чтобы обойти защиту по
    литералу. Без подмены каждый тестовый запрос уходил бы в настоящий DNS,
    и в офлайне или CI это дало бы ложные блокировки.

    Тесты самой защиты переопределяют ``resolve_addresses`` точечно.
    """
    monkeypatch.setattr(netguard, "resolve_addresses",
                        lambda host: ["93.184.216.34"])


@pytest.fixture
def reset_cache(tmp_path, monkeypatch):
    """Изолированный кэш в tmp_path, чтобы тесты не трогали пользовательский.

    Кэш в сервере создаётся лениво, поэтому подменяется именно привязанное
    состояние: сам объект, признак ошибки и счётчик чистки.
    """
    monkeypatch.setattr(server, "_cache", Cache(tmp_path))
    monkeypatch.setattr(server, "_cache_error", None)
    return server._cache
