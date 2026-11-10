"""Ротация движков: при блоке первого пробуем следующий."""

import httpx
import pytest

from websearch_mcp.engines import EngineChain, ENGINE_REGISTRY

OK_HTML = """
<html><body>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://example.org/one">One</a></h2>
  <a class="result__snippet">First snippet.</a>
</div>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://example.org/two">Two</a></h2>
  <a class="result__snippet">Second snippet.</a>
</div>
</body></html>
"""

CHALLENGE_HTML = "<html><body><h1>Please solve this challenge</h1></body></html>"


def _handler(routes):
    """routes: dict engine_name -> (status, html). Пустой html -> отдаём OK."""
    def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        name = None
        for eng, cfg in ENGINE_REGISTRY.items():
            if cfg["host"] in host:
                name = eng
                break
        status, html = routes.get(name, (200, OK_HTML))
        return httpx.Response(status, text=html)
    return handle


@pytest.fixture
def chain_factory():
    def make(routes=None, engines=("duckduckgo", "mojeek")):
        routes = routes or {}
        transport = httpx.MockTransport(_handler(routes))
        client = httpx.Client(transport=transport)
        return EngineChain(client=client, engines=engines, sleep=lambda _s: None)
    return make


def test_first_engine_wins(chain_factory):
    chain = chain_factory()
    res = chain.search("httpx timeout config")
    assert len(res.results) == 2
    assert res.engine_used == "duckduckgo"


def test_falls_through_on_challenge(chain_factory):
    chain = chain_factory(routes={"duckduckgo": (200, CHALLENGE_HTML)})
    res = chain.search("httpx timeout config")
    assert res.engine_used == "mojeek"
    assert len(res.results) == 2


def test_falls_through_on_202(chain_factory):
    chain = chain_factory(routes={"duckduckgo": (202, "")})
    res = chain.search("httpx timeout config")
    assert res.engine_used == "mojeek"


def test_falls_through_on_network_error(chain_factory):
    def broken(request):
        host = request.url.host
        if ENGINE_REGISTRY["duckduckgo"]["host"] in host:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=OK_HTML)

    client = httpx.Client(transport=httpx.MockTransport(broken))
    chain = EngineChain(client=client, engines=("duckduckgo", "mojeek"), sleep=lambda _s: None)
    res = chain.search("x")
    assert res.engine_used == "mojeek"


def test_all_engines_blocked_raises_with_reasons(chain_factory):
    chain = chain_factory(routes={
        "duckduckgo": (200, CHALLENGE_HTML),
        "mojeek": (429, ""),
    })
    with pytest.raises(AllEnginesBlockedError) as exc:
        chain.search("x")
    assert len(exc.value.failures) == 2


def test_error_message_names_every_engine(chain_factory):
    chain = chain_factory(routes={
        "duckduckgo": (200, CHALLENGE_HTML),
        "mojeek": (429, ""),
    })
    with pytest.raises(AllEnginesBlockedError) as exc:
        chain.search("x")
    msg = str(exc.value)
    assert "duckduckgo" in msg and "mojeek" in msg


def test_max_results_truncates(chain_factory):
    chain = chain_factory()
    res = chain.search("x", max_results=1)
    assert len(res.results) == 1


def test_results_are_never_fabricated(chain_factory):
    """Принципиальная инварианта: если движок отдал мусор, мы НЕ возвращаем
    пустой список как успех — мы идём дальше и в итоге бросаем ошибку."""
    chain = chain_factory(routes={
        "duckduckgo": (200, "<html><body>nothing here</body></html>"),
        "mojeek": (200, "<html><body>nothing here</body></html>"),
    })
    with pytest.raises(AllEnginesBlockedError):
        chain.search("x")


def test_sleep_called_between_retries(chain_factory):
    slept = []
    routes = {"duckduckgo": (429, "")}
    transport = httpx.MockTransport(_handler(routes))
    client = httpx.Client(transport=transport)
    chain = EngineChain(
        client=client,
        engines=("duckduckgo", "mojeek"),
        sleep=slept.append,
    )
    chain.search("x")
    assert slept, "между движками должна быть пауза"


# импорт здесь, чтобы тест собрался после объявления модуля
from websearch_mcp.engines import AllEnginesBlockedError  # noqa: E402
