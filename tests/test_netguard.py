"""Тесты SSRF-защиты: какие адреса разрешены, а какие заблокированы.

``conftest.fake_dns`` подменяет резолв публичным адресом, поэтому тесты,
проверяющие поведение по DNS, переопределяют ``resolve_addresses`` явно.
"""

from __future__ import annotations

import pytest

from websearch_mcp import netguard, server
from websearch_mcp.netguard import BlockedAddressError, validate_url

# Адреса, которые обязан блокировать guard. Покрывают cloud metadata,
# локальные сервисы машины пользователя и обходные формы записи.
_BLOCKED = [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://127.0.0.1:8080/admin",
    "http://localhost:3000/",
    "http://192.168.1.1/",
    "http://10.0.0.5/internal",
    "http://172.16.0.1/",
    "http://0.0.0.0/",
    "http://[::1]:6379/",
    "http://[fc00::1]/",
    "http://[fe80::1]/",
    "http://100.64.0.1/",          # CGNAT — не глобальный
    "http://240.0.0.1/",           # зарезервировано
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped обход
    "http://[::ffff:169.254.169.254]/",
]

_ALLOWED = [
    "https://example.org/docs/api",
    "https://httpx.readthedocs.io/en/stable/",
    "http://93.184.216.34/page",
    "https://docs.python.org/3/library/ipaddress.html",
]


@pytest.mark.parametrize("url", _BLOCKED)
def test_blocked_addresses_rejected(url):
    with pytest.raises(BlockedAddressError):
        validate_url(url)


@pytest.mark.parametrize("url", _ALLOWED)
def test_public_addresses_allowed(url):
    assert validate_url(url) == url


def test_blocked_address_error_is_value_error():
    """Наследование от ValueError важно: вызывающий код ловит её как ValueError
    (см. search_and_read) и не роняет вызов на одной плохой ссылке."""
    assert issubclass(BlockedAddressError, ValueError)


@pytest.mark.parametrize("url", ["ftp://example.org/f", "file:///etc/passwd",
                                 "gopher://example.org/", "javascript:alert(1)",
                                 "", "не url"])
def test_non_http_schemes_rejected(url):
    with pytest.raises(BlockedAddressError):
        validate_url(url)


def test_dns_resolving_to_private_ip_is_blocked(monkeypatch):
    """Домен, указывающий на приватный адрес, блокируется — иначе достаточно
    зарегистрировать evil.example → 127.0.0.1, чтобы обойти проверку литерала."""
    monkeypatch.setattr(netguard, "resolve_addresses", lambda host: ["10.0.0.7"])
    with pytest.raises(BlockedAddressError, match="запрещённый"):
        validate_url("https://evil.example/data")


def test_one_private_address_in_multi_record_blocks(monkeypatch):
    """Хост с несколькими записями, где хотя бы одна приватная, блокируется
    целиком: запрос мог бы уйти на неё."""
    monkeypatch.setattr(netguard, "resolve_addresses",
                        lambda host: ["93.184.216.34", "192.168.0.1"])
    with pytest.raises(BlockedAddressError):
        validate_url("https://mixed.example/")


def test_unresolvable_host_fails_closed(monkeypatch):
    """Нерезолвимое имя — блокировка, а не разрешение (fail closed)."""
    def boom(host):
        raise BlockedAddressError(f"не удалось разрешить имя {host!r}")

    monkeypatch.setattr(netguard, "resolve_addresses", boom)
    with pytest.raises(BlockedAddressError):
        validate_url("https://no-such-host.invalid/")


def test_check_dns_false_skips_resolution(monkeypatch):
    """check_dns=False не трогает DNS: дешёвая фильтрация больших списков."""
    def boom(host):
        raise AssertionError("резолв не должен вызываться")

    monkeypatch.setattr(netguard, "resolve_addresses", boom)
    assert validate_url("https://example.org/x", check_dns=False) == "https://example.org/x"
    # литералы и служебные имена проверяются даже без DNS
    with pytest.raises(BlockedAddressError):
        validate_url("http://127.0.0.1/x", check_dns=False)


def test_is_public_url_does_not_raise(monkeypatch):
    monkeypatch.setattr(netguard, "resolve_addresses", lambda host: ["10.0.0.7"])
    assert netguard.is_public_url("https://evil.example/") is False
    assert netguard.is_public_url("ftp://example.org/") is False


# --------------------------------------------------------------------------
# WEBSEARCH_ALLOW_LOCAL — осознанный opt-in для чтения локальной документации
# --------------------------------------------------------------------------

def test_allow_local_env_permits_localhost(monkeypatch):
    """С флагом loopback разрешается: разработчик читает свои доки на :3000."""
    monkeypatch.setenv("WEBSEARCH_ALLOW_LOCAL", "1")
    monkeypatch.setattr(netguard, "resolve_addresses", lambda host: ["127.0.0.1"])
    assert validate_url("http://localhost:3000/docs") == "http://localhost:3000/docs"
    assert validate_url("http://127.0.0.1:8080/x") == "http://127.0.0.1:8080/x"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " True "])
def test_allow_local_accepts_truthy_values(monkeypatch, value):
    monkeypatch.setenv("WEBSEARCH_ALLOW_LOCAL", value)
    assert netguard._allow_local() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "2"])
def test_allow_local_rejects_other_values(monkeypatch, value):
    monkeypatch.setenv("WEBSEARCH_ALLOW_LOCAL", value)
    assert netguard._allow_local() is False


def test_allow_local_not_set_by_default(monkeypatch):
    monkeypatch.delenv("WEBSEARCH_ALLOW_LOCAL", raising=False)
    assert netguard._allow_local() is False
    with pytest.raises(BlockedAddressError):
        validate_url("http://localhost:3000/")


def test_cloud_metadata_blocked_even_with_allow_local(monkeypatch):
    """Главное: opt-in НЕ открывает IAM-креденшелы облака. Легитимной причины
    читать 169.254.169.254 у инструмента для документации нет."""
    monkeypatch.setenv("WEBSEARCH_ALLOW_LOCAL", "1")
    for url in ("http://169.254.169.254/latest/meta-data/",
                "http://metadata.google.internal/computeMetadata/v1/",
                "http://metadata/", "http://instance-data/"):
        with pytest.raises(BlockedAddressError):
            validate_url(url)


def test_allow_local_still_blocks_other_link_local(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_ALLOW_LOCAL", "1")
    monkeypatch.setattr(netguard, "resolve_addresses", lambda host: ["169.254.1.1"])
    with pytest.raises(BlockedAddressError):
        validate_url("http://169.254.1.1/x")


def test_always_blocked_hosts_are_subset_of_blocked():
    """Гарантия непротиворечивости: то, что блокируется при allow_local,
    блокируется и в обычном режиме."""
    assert netguard._ALWAYS_BLOCKED_HOSTS <= netguard._BLOCKED_HOSTNAMES


def test_reserved_and_multicast_blocked_with_allow_local(monkeypatch):
    """allow_local касается только loopback/link-local/private."""
    monkeypatch.setenv("WEBSEARCH_ALLOW_LOCAL", "1")
    for url in ("http://240.0.0.1/", "http://0.0.0.0/", "http://224.0.0.1/"):
        with pytest.raises(BlockedAddressError):
            validate_url(url)


# --------------------------------------------------------------------------
# Интеграция с инструментами сервера
# --------------------------------------------------------------------------

def test_fetch_page_blocks_metadata_endpoint():
    """Регрессия: fetch_page охотно отдавал содержимое 169.254.169.254."""
    with pytest.raises(BlockedAddressError):
        server.fetch_page("http://169.254.169.254/latest/meta-data/")


def test_crawl_docs_blocks_private_start_url():
    with pytest.raises(BlockedAddressError):
        server.crawl_docs("http://192.168.1.1/docs", depth=1, max_pages=5)


def test_crawl_docs_skips_blocked_link_instead_of_failing(monkeypatch):
    """Запрещённая ссылка ВНУТРИ обхода пропускается, а не роняет вызов."""
    monkeypatch.setattr(server, "_page_cache_get", lambda url: (None, []))
    monkeypatch.setattr(server, "_page_cache_set", lambda *a: None)

    def fake_get_html(url):
        # Повторяет поведение настоящего _get_html: guard первым делом.
        # Именно перехват этого исключения в цикле обхода здесь и проверяется.
        netguard.validate_url(url)
        if "169.254" in url or "10.0.0" in url:
            raise AssertionError("guard должен был заблокировать раньше")
        return ("<html><body><article>ok " + "text " * 60 + "</article></body></html>",
                "text/html")

    monkeypatch.setattr(server, "_get_html", fake_get_html)
    monkeypatch.setattr(server, "parse_page",
                        lambda html, url: ("# start\n\ngood page",
                                           ["http://169.254.169.254/meta",
                                            "http://10.0.0.1/admin",
                                            "https://example.org/safe"]))

    out = server.crawl_docs("https://example.org/start", depth=1, max_pages=10)
    # Обход не упал и собрал стартовую страницу
    assert "good page" in out
    assert "169.254" not in out
    assert "10.0.0.1" not in out


def test_search_and_read_reports_blocked_page_without_crashing(monkeypatch):
    """search_and_read ловит BlockedAddressError как ValueError: одна плохая
    ссылка из выдачи не должна ронять весь вызов."""
    monkeypatch.setattr(server, "_page_cache_get", lambda url: (None, []))

    class _R:
        rank = 1
        title = "Metadata"
        url = "http://169.254.169.254/latest/meta-data/"
        snippet = ""
        engine = "duckduckgo"

    class _Res:
        query = "test"
        results = [_R()]
        engine_used = "duckduckgo"
        from_cache = False
        attempts = []

    monkeypatch.setattr(server.EngineChain, "search", lambda self, *a, **k: _Res())
    out = server.search_and_read("test", max_pages=1)
    assert "не удалось прочитать" in out
