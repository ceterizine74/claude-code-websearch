"""Ограничения нагрузки: дедлайн поиска, потолок загрузки и объёма выдачи.

Эти тесты фиксируют гарантии, из-за которых сервер не висит и не растёт
в памяти: бюджет времени на поиск, лимит размера страницы и суммарный
потолок markdown в crawl_docs.
"""

import httpx
import pytest

from websearch_mcp.engines import AllEnginesBlockedError, EngineChain
from websearch_mcp.parse import BlockReason
from websearch_mcp.server import (PageTooLargeError, _MAX_DOWNLOAD_BYTES,
                                  _MAX_TOTAL_MD_CHARS, _get_html, _page_cache_get,
                                  _page_cache_set, crawl_docs)


# --------------------------------------------------------------------------
# Дедлайн поиска
# --------------------------------------------------------------------------

def _chain_sleeping(client, **kwargs):
    return EngineChain(client=client, engines=("duckduckgo", "mojeek"),
                       sleep=lambda _s: None, **kwargs)


def test_search_respects_deadline():
    """Если движки висят, поиск обязан прерваться по бюджету, а не крутиться
    до per-request таймаута по всем хостам цепочки."""
    def hang(request):
        # Имитация медленного ответа: ошибка сети, чтобы не ждать реального I/O
        raise httpx.ReadTimeout("slow", request=request)

    client = httpx.Client(transport=httpx.MockTransport(hang))
    # Бюджет уже исчерпан — ни один запрос не должен уйти
    chain = _chain_sleeping(client, deadline_s=0.0)

    with pytest.raises(AllEnginesBlockedError) as exc:
        chain.search("python httpx timeout")

    reasons = [b.kind for _e, b in exc.value.failures]
    assert BlockReason.NETWORK in reasons
    # Причина должна объяснять, что кончилось время, а не только «сеть упала»
    assert any("дедлайн" in e for e, _b in exc.value.failures)


def test_deadline_mentioned_in_error_message():
    def hang(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = httpx.Client(transport=httpx.MockTransport(hang))
    chain = _chain_sleeping(client, deadline_s=0.0)

    with pytest.raises(AllEnginesBlockedError) as exc:
        chain.search("x")
    assert "бюджет времени" in str(exc.value)


def test_clamp_timeout_never_exceeds_remaining():
    """Таймаут запроса урезается до остатка бюджета: иначе один медленный
    движок живёт свои 20 с поверх дедлайна всей цепочки."""
    base = httpx.Timeout(20.0, connect=10.0)
    t = EngineChain._clamp_timeout(base, 3.0)
    assert t.read == 3.0
    assert t.connect == 3.0


def test_clamp_timeout_keeps_smaller_base():
    base = httpx.Timeout(5.0, connect=2.0)
    t = EngineChain._clamp_timeout(base, 30.0)
    assert t.read == 5.0
    assert t.connect == 2.0


def test_clamp_timeout_stays_positive():
    """httpx не принимает неположительные таймауты — при нулевом остатке
    нужен маленький, но валидный интервал."""
    t = EngineChain._clamp_timeout(httpx.Timeout(20.0), 0.0)
    assert t.read > 0


def test_backoff_sleep_is_bounded_by_deadline():
    """Пауза между движками не может вывести за пределы бюджета."""
    slept = []

    def always_blocked(request):
        return httpx.Response(429, text="")

    client = httpx.Client(transport=httpx.MockTransport(always_blocked))
    chain = EngineChain(
        client=client,
        engines=("duckduckgo",),
        sleep=slept.append,
        deadline_s=0.5,
    )
    with pytest.raises(AllEnginesBlockedError):
        chain.search("x")

    assert all(s <= 0.5 + 1e-9 for s in slept), \
        f"пауза превысила бюджет: {slept}"


# --------------------------------------------------------------------------
# Потолок размера загрузки
# --------------------------------------------------------------------------

def test_too_large_page_raises(monkeypatch):
    """Страница больше лимита обрывается, а не выедает гигабайты памяти."""
    oversized = b"x" * (_MAX_DOWNLOAD_BYTES + 1)

    def big_body(request):
        return httpx.Response(200, content=oversized,
                              headers={"content-type": "text/html"})

    from websearch_mcp import server
    monkeypatch.setattr(server, "_client",
                        httpx.Client(transport=httpx.MockTransport(big_body)))

    with pytest.raises(PageTooLargeError):
        _get_html("https://example.org/huge")


def test_declared_content_length_rejected_early(monkeypatch):
    """Если сервер сразу объявил огромный content-length, качать не нужно."""
    def lying_headers(request):
        return httpx.Response(
            200, content=b"<html><body>tiny</body></html>",
            headers={"content-type": "text/html",
                     "content-length": str(_MAX_DOWNLOAD_BYTES * 2)},
        )

    from websearch_mcp import server
    monkeypatch.setattr(server, "_client",
                        httpx.Client(transport=httpx.MockTransport(lying_headers)))

    with pytest.raises(PageTooLargeError):
        _get_html("https://example.org/huge")


def test_normal_page_downloads(monkeypatch):
    body = "<html><body><p>hello</p></body></html>"

    def ok(request):
        return httpx.Response(200, text=body,
                              headers={"content-type": "text/html"})

    from websearch_mcp import server
    monkeypatch.setattr(server, "_client",
                        httpx.Client(transport=httpx.MockTransport(ok)))

    html, ctype = _get_html("https://example.org/x")
    assert "hello" in html
    assert "html" in ctype


# --------------------------------------------------------------------------
# Потолок суммарного объёма crawl_docs
# --------------------------------------------------------------------------

def test_crawl_docs_caps_total_output(monkeypatch):
    """Склейка ограничена по суммарному объёму: 50 × 60 000 знаков не влезет
    в контекст клиента."""
    from websearch_mcp import server

    # Две страницы, каждая чуть больше половины общего потолка
    half = "A" * (_MAX_TOTAL_MD_CHARS // 2 + 1000)

    def fake_parse_page(html, url):
        return f"# page {url}\n\n{half}", []

    def fake_get_html(url):
        return ("<html><body><p>x</p></body></html>", "text/html")

    monkeypatch.setattr(server, "parse_page", fake_parse_page)
    monkeypatch.setattr(server, "_get_html", fake_get_html)
    monkeypatch.setattr(server, "_page_cache_get", lambda url: (None, []))
    monkeypatch.setattr(server, "_page_cache_set", lambda *a: None)

    out = crawl_docs("https://example.org/docs", depth=0, max_pages=10)
    assert len(out) < _MAX_TOTAL_MD_CHARS * 2, \
        "объём ответа не ограничен"


def test_crawl_docs_reports_truncation(monkeypatch):
    from websearch_mcp import server

    half = "A" * (_MAX_TOTAL_MD_CHARS // 2 + 1000)
    counter = {"n": 0}

    def fake_parse_page(html, url):
        counter["n"] += 1
        return f"# page {url}\n\n{half}", [
            f"https://example.org/docs/p{counter['n']}"
        ]

    monkeypatch.setattr(server, "parse_page", fake_parse_page)
    monkeypatch.setattr(server, "_get_html",
                        lambda url: ("<html><body>x</body></html>", "text/html"))
    monkeypatch.setattr(server, "_page_cache_get", lambda url: (None, []))
    monkeypatch.setattr(server, "_page_cache_set", lambda *a: None)

    out = crawl_docs("https://example.org/docs", depth=2, max_pages=20)
    assert "Обход прерван" in out


# --------------------------------------------------------------------------
# Формат кэша страниц
# --------------------------------------------------------------------------

def test_page_cache_roundtrip(reset_cache):
    _page_cache_set("https://example.org/a", "# Markdown", ["https://example.org/b"])
    md, links = _page_cache_get("https://example.org/a")
    assert md == "# Markdown"
    assert links == ["https://example.org/b"]


def test_page_cache_miss_returns_none(reset_cache):
    md, links = _page_cache_get("https://example.org/never-stored")
    assert md is None
    assert links == []


def test_legacy_string_cache_entry_is_a_miss(reset_cache):
    """Записи старого формата (голая строка) не должны ломать чтение:
    они считаются промахом и страница перекачивается один раз."""
    from websearch_mcp import server
    server._cache.set("page:https://example.org/legacy", "# old string value")

    md, links = _page_cache_get("https://example.org/legacy")
    assert md is None
    assert links == []


def test_cache_entry_without_links_still_usable(reset_cache):
    from websearch_mcp import server
    server._cache.set("page:https://example.org/nolinks", {"md": "# x"})
    md, links = _page_cache_get("https://example.org/nolinks")
    assert md == "# x"
    assert links == []


# --------------------------------------------------------------------------
# crawl_docs использует кэш и не теряет ссылки
# --------------------------------------------------------------------------

def test_crawl_docs_uses_cached_page(monkeypatch):
    """Страница из кэша не скачивается повторно."""
    from websearch_mcp import server

    fetched = []

    def fake_get_html(url):
        fetched.append(url)
        return ("<html><body><p>x</p></body></html>", "text/html")

    monkeypatch.setattr(server, "_get_html", fake_get_html)
    monkeypatch.setattr(server, "parse_page", lambda html, url: (f"# {url}", []))
    monkeypatch.setattr(server, "_page_cache_get",
                        lambda url: ("# cached", []) if "cached" in url else (None, []))
    monkeypatch.setattr(server, "_page_cache_set", lambda *a: None)

    crawl_docs("https://example.org/cached", depth=0, max_pages=5)
    assert fetched == [], "кэшированная страница не должна скачиваться"
