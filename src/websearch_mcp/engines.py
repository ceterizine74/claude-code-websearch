"""Реестр бесплатных движков и цепочка ротации.

Ни один движок не требует ключа. При блокировке одного — молча пробуем
следующий, с экспоненциальной паузой. Если легли все — бросаем
``AllEnginesBlockedError`` со списком причин; выдуманных результатов
сервер не возвращает никогда.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import httpx

from .parse import (Block, BlockReason, SearchResult, detect_block,
                    parse_brave, parse_ddg, parse_mojeek, parse_searxng)

# Реалистичный десктопный UA: без него часть движков отдаёт урезанную страницу.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# host используется для диагностики и сопоставления в тестах.
ENGINE_REGISTRY: dict[str, dict[str, Any]] = {
    "duckduckgo": {
        "host": "duckduckgo.com",
        "parser": parse_ddg,
        "request": {
            "method": "POST",
            "url": "https://html.duckduckgo.com/html/",
            "data": {"q": "{query}", "kl": "wt-wt", "df": ""},
            "headers": {**_DEFAULT_HEADERS, "Referer": "https://html.duckduckgo.com/"},
        },
        "note": "HTML-эндпоинт, POST снимает большинство блоков",
    },
    "mojeek": {
        "host": "mojeek.com",
        "parser": parse_mojeek,
        "request": {
            "method": "GET",
            "url": "https://www.mojeek.com/search?q={query}&fmt=results",
            "headers": _DEFAULT_HEADERS,
        },
        "note": "независимый индекс, обычно не капчится",
    },
    "brave": {
        "host": "search.brave.com",
        "parser": parse_brave,
        "request": {
            "method": "GET",
            "url": "https://search.brave.com/search?q={query}&source=web",
            "headers": _DEFAULT_HEADERS,
        },
        "note": "иногда отдаёт JS-заглушку, тогда ротируемся дальше",
    },
    "searxng": {
        "host": "searx.be",
        "parser": parse_searxng,
        "request": {
            "method": "GET",
            "url": "https://searx.be/search?q={query}&format=json",
            "headers": {**_DEFAULT_HEADERS, "Accept": "application/json"},
        },
        "note": "публичный мета-поисковик",
        "fallback_hosts": ["searx.tiekoetter.com", "search.bus-hit.me", "paulgo.io"],
    },
}

DEFAULT_ENGINE_ORDER: tuple[str, ...] = ("duckduckgo", "mojeek", "brave", "searxng")

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_MAX_RETRIES_PER_ENGINE = 1
_BASE_BACKOFF = 1.2
_MAX_BACKOFF = 6.0

# Общий бюджет на одну попытку поиска. Без него цепочка из 7 целей
# (searxng даёт 4 запасных хоста) × 2 попытки × 20 с таймаут + backoff
# может крутиться ~6.6 минуты, прежде чем честно признаться в отказе.
# Клиент MCP столько не ждёт, поэтому прерываемся по дедлайну — но всё
# равно бросаем AllEnginesBlockedError с собранными причинами.
_SEARCH_DEADLINE_S = 30.0


class AllEnginesBlockedError(RuntimeError):
    """Все движки отказали. Содержит причину по каждому — для честной ошибки."""

    def __init__(self, query: str, failures: Sequence[tuple[str, Block]]) -> None:
        self.query = query
        self.failures = list(failures)
        lines = [f"Веб-поиск недоступен: все движки отказали (запрос «{query}»)."]
        for engine, block in self.failures:
            lines.append(f"  - {engine}: {block.kind.value} — {block.detail}")
        lines.append(
            "Это настоящая ошибка, а не пустой результат. Попробуйте позже, "
            "смените формулировку или проверьте `websearch doctor`."
        )
        super().__init__("\n".join(lines))


@dataclass
class ChainResult:
    """Успешный результат цепочки."""

    query: str
    results: list[SearchResult]
    engine_used: str
    attempts: list[str] = field(default_factory=list)
    from_cache: bool = False


@dataclass
class EngineAttempt:
    engine: str
    ok: bool
    n_results: int = 0
    block: Block | None = None
    elapsed_ms: int = 0
    results: list[SearchResult] = field(default_factory=list)


class EngineChain:
    """Последовательно пробует движки до первого рабочего ответа."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        engines: Sequence[str] = DEFAULT_ENGINE_ORDER,
        sleep: Callable[[float], None] = time.sleep,
        cache=None,
        ttl: int | None = None,
        timeout: httpx.Timeout = _TIMEOUT,
        deadline_s: float = _SEARCH_DEADLINE_S,
    ) -> None:
        # Один клиент с общей cookie-банкой: httpx сам разносит cookie по доменам,
        # так что каждый движок накапливает свой постоянный набор.
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        self._engines = tuple(engines)
        self._sleep = sleep
        self._cache = cache
        self._ttl = ttl
        self._timeout = timeout
        self._deadline_s = deadline_s

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------

    def search(self, query: str, max_results: int = 8,
               use_cache: bool = True) -> ChainResult:
        query = query.strip()
        if not query:
            raise ValueError("пустой поисковый запрос")

        cache_key = f"search:{query.lower()}:{max_results}"
        if use_cache and self._cache is not None and self._ttl:
            hit = self._cache.get(cache_key, self._ttl)
            if hit is not None:
                return ChainResult(
                    query=query,
                    results=[SearchResult(**r) for r in hit["results"]],
                    engine_used=hit["engine_used"],
                    attempts=hit.get("attempts", []),
                    from_cache=True,
                )

        failures: list[tuple[str, Block]] = []
        attempts: list[str] = []
        backoff = _BASE_BACKOFF
        # monotonic, а не time(): перевод часов не должен менять бюджет поиска
        deadline = time.monotonic() + self._deadline_s
        out_of_time = False

        for engine in self._engines:
            cfg = ENGINE_REGISTRY.get(engine)
            if cfg is None:
                failures.append((engine, Block(BlockReason.NETWORK, "неизвестный движок")))
                continue

            hosts = [cfg["request"]["url"]]
            for alt in cfg.get("fallback_hosts", []):
                hosts.append(cfg["request"]["url"].replace("searx.be", alt))

            for target in hosts:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    out_of_time = True
                    break

                result = self._try(engine, cfg, query, target, max_results,
                                   deadline=deadline)
                attempts.append(
                    f"{engine}:{'ok' if result.ok else (result.block.kind.value if result.block else 'err')}"
                    f"({result.n_results})"
                )
                if result.ok:
                    chain = ChainResult(
                        query=query,
                        results=result.results[:max_results],
                        engine_used=engine,
                        attempts=attempts,
                    )
                    self._store(cache_key, chain)
                    return chain

                assert result.block is not None
                failures.append((engine, result.block))

                # Пауза тоже укладывается в бюджет: спать дольше смысла нет,
                # запрос после этого всё равно не успеет уйти.
                pause = min(backoff + random.uniform(0, 0.4), _MAX_BACKOFF)
                if not self._backoff_sleep(pause, deadline):
                    out_of_time = True
                    break
                backoff = min(backoff * 2, _MAX_BACKOFF)

            if out_of_time:
                break

        if out_of_time:
            failures.append((
                "дедлайн",
                Block(BlockReason.NETWORK,
                      f"исчерпан бюджет времени {self._deadline_s:.0f} с на поиск"),
            ))

        raise AllEnginesBlockedError(query, failures)

    # ------------------------------------------------------------------

    def _try(self, engine: str, cfg: dict, query: str, url: str,
             max_results: int, deadline: float | None = None) -> EngineAttempt:
        req = cfg["request"]
        # GET: шаблон {query} в URL кодируется. POST: {query} в теле — как есть.
        encoded = httpx.QueryParams({"q": query})["q"]
        full_url = url.replace("{query}", encoded)
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        for attempt in range(_MAX_RETRIES_PER_ENGINE + 1):
            # Таймаут запроса не может превышать остаток общего бюджета,
            # иначе один медленный движок съест дедлайн всей цепочки.
            kwargs: dict[str, Any] = {"headers": req["headers"]}
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return EngineAttempt(
                        engine, False, 0,
                        Block(BlockReason.NETWORK, "истёк бюджет времени"), elapsed_ms())
                kwargs["timeout"] = self._clamp_timeout(self._timeout, remaining)

            try:
                if req["method"] == "POST":
                    kwargs["data"] = {
                        k: v.replace("{query}", query) for k, v in req["data"].items()
                    }
                    resp = self._client.post(full_url, **kwargs)
                else:
                    resp = self._client.get(full_url, **kwargs)
            except (httpx.HTTPError, OSError) as exc:
                block = Block(BlockReason.NETWORK, f"{type(exc).__name__}: {exc}")
                if (attempt < _MAX_RETRIES_PER_ENGINE
                        and self._backoff_sleep(_BASE_BACKOFF * (2 ** attempt), deadline)):
                    continue
                return EngineAttempt(engine, False, 0, block, elapsed_ms())

            html = resp.text
            try:
                results = cfg["parser"](html)
            except Exception as exc:   # парсер упал — считаем движок нерабочим
                return EngineAttempt(
                    engine, False, 0,
                    Block(BlockReason.NETWORK, f"сбой парсера: {type(exc).__name__}: {exc}"),
                    elapsed_ms(),
                )

            block = detect_block(html, resp.status_code, len(results))
            if block is None:
                return EngineAttempt(
                    engine, True, len(results), None,
                    elapsed_ms(),
                    results=results,
                )

            # Повтор имеет смысл только при rate limit — капчу долбёжкой не снять.
            if (block.kind == BlockReason.RATE_LIMIT
                    and attempt < _MAX_RETRIES_PER_ENGINE
                    and self._backoff_sleep(_BASE_BACKOFF * (2 ** attempt), deadline)):
                continue
            return EngineAttempt(engine, False, 0, block, elapsed_ms())

        return EngineAttempt(engine, False, 0,
                             Block(BlockReason.NETWORK, "исчерпаны попытки"))

    @staticmethod
    def _out_of_budget(deadline: float | None) -> bool:
        """Повторять запрос бессмысленно, если бюджет уже исчерпан."""
        return deadline is not None and time.monotonic() >= deadline

    def _backoff_sleep(self, pause: float, deadline: float | None) -> bool:
        """Поспать перед повтором, не выходя за общий бюджет поиска.

        Вернуть ``True`` — повтор имеет смысл, ``False`` — бюджет исчерпан,
        спать и повторять уже нечего. Без урезания паузы дедлайном цепочка
        способна проспать ``_BASE_BACKOFF`` (1.2 с) поверх бюджета в 0.5 с.
        """
        if deadline is None:
            self._sleep(pause)
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        self._sleep(min(pause, remaining))
        return True

    @staticmethod
    def _clamp_timeout(base: httpx.Timeout, remaining: float) -> httpx.Timeout:
        """Таймаут запроса, урезанный до остатка общего бюджета.

        Без этого одиночный запрос живёт свои 20 с поверх дедлайна цепочки:
        проверка остатка времени идёт до отправки, а не во время чтения ответа.
        """
        left = max(remaining, 0.05)   # httpx не принимает неположительные таймауты
        return httpx.Timeout(
            connect=min(base.connect, left),
            read=min(base.read, left),
            write=min(base.write, left),
            pool=min(base.pool, left),
        )

    def _store(self, cache_key: str, chain: ChainResult) -> None:
        if self._cache is None or not self._ttl:
            return
        try:
            self._cache.set(cache_key, {
                "results": [r.__dict__ for r in chain.results],
                "engine_used": chain.engine_used,
                "attempts": chain.attempts,
            })
        except OSError:
            pass   # кэш не должен ломать поиск

    # ------------------------------------------------------------------

    def diagnose(self, query: str = "test") -> list[EngineAttempt]:
        """Прогнать каждый движок и вернуть отчёт — для `websearch doctor`."""
        report: list[EngineAttempt] = []
        for engine in self._engines:
            cfg = ENGINE_REGISTRY.get(engine)
            if cfg is None:
                report.append(EngineAttempt(engine, False, 0,
                                            Block(BlockReason.NETWORK, "неизвестный движок")))
                continue
            report.append(self._try(engine, cfg, query, cfg["request"]["url"], 5))
        return report
