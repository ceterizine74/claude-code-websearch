"""FastMCP-сервер: инструменты веб-поиска для MCP-клиентов.

Вся логика — в ``engines`` / ``fetch`` / ``cache``; сервер только
превращает её в инструменты и форматирует ответы. Если все движки
отказали, бросается ``AllEnginesBlockedError`` — клиент увидит честную
ошибку, а не выдуманные результаты.
"""

from __future__ import annotations

import logging
from collections import deque

import httpx
from mcp.server.mcpserver import MCPServer

# Критично для stdio-транспорта: логи httpx не должны попадать в stdout,
# иначе они сломают JSON-RPC поток.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from .cache import Cache, TTL_PAGE, TTL_SEARCH
from .engines import DEFAULT_ENGINE_ORDER, EngineChain, ENGINE_REGISTRY
from .fetch import parse_page

mcp = MCPServer(
    "websearch",
    instructions=(
        "Локальный веб-поиск без ключей и оплаты. web_search ищет, fetch_page "
        "читает страницу в markdown с сохранением блоков кода, crawl_docs "
        "обходит документацию в пределах домена, search_and_read делает поиск и "
        "чтение топов одним вызовом. Если все поисковые движки заблокированы, "
        "инструмент вернёт честную ошибку с причинами — не выдумывайте результаты."
    ),
)

# Красивая страница текста весит десятки килобайт, а BeautifulSoup раздувает
# HTML в памяти примерно в 20 раз. Без потолка один тяжёлый URL может увести
# процесс в гигабайты, поэтому читаем поток и обрываемся на лимите.
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024

# Склейка crawl_docs уходит прямо в контекст клиента. Ограничиваем суммарный
# объём, а не только каждую страницу: 50 × 60 000 знаков — это ~750 000 токенов,
# что не влезает ни в одно окно.
_MAX_TOTAL_MD_CHARS = 300_000

# Кэш создаётся при первом обращении, а не при импорте модуля. Если home
# недоступен (контейнер, CI без HOME) или каталог не создаётся, сервер обязан
# остаться рабочим: поиск и чтение страниц от кэша не зависят.
# Это та же инварианта, что в cache.py — «кэш не должен уметь ломать сервер».
_cache: Cache | None = None
_cache_error: str | None = None


def _get_cache() -> Cache | None:
    """Кэш или None, если его не удалось создать. Причина сохраняется."""
    global _cache, _cache_error
    if _cache is not None:
        return _cache
    if _cache_error is not None:
        return None
    try:
        _cache = Cache()
    except (OSError, RuntimeError) as exc:
        _cache_error = f"{type(exc).__name__}: {exc}"
    return _cache


_client = httpx.Client(
    timeout=httpx.Timeout(25.0, connect=10.0),
    follow_redirects=True,
    headers={"User-Agent": ENGINE_REGISTRY["duckduckgo"]["request"]["headers"]["User-Agent"]},
)


def _chain() -> EngineChain:
    """Цепочка с общим клиентом (cookie-банка) и кэшем поиска."""
    return EngineChain(client=_client, engines=DEFAULT_ENGINE_ORDER,
                       cache=_get_cache(), ttl=TTL_SEARCH)


def _get_html(url: str) -> tuple[str, str]:
    """Скачать страницу с потолком по размеру. Вернуть (текст, content-type).

    Ошибка при переполнении — ``PageTooLargeError``; вызывающий решает,
    считать ли это фатальным или просто пропустить страницу.
    """
    with _client.stream("GET", url) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")

        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _MAX_DOWNLOAD_BYTES:
            raise PageTooLargeError(
                f"страница объявляет размер {int(declared) // (1024 * 1024)} MB — "
                f"больше лимита {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB"
            )

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes(_READ_CHUNK):
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise PageTooLargeError(
                    f"страница больше лимита {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB, "
                    "загрузка прервана"
                )
            chunks.append(chunk)

        # resp.encoding у httpx никогда не None: при отсутствии charset в
        # заголовке он подставляет дефолт. LookupError возможен на экзотической
        # кодировке, тогда читаем как UTF-8 с заменой битых байтов.
        try:
            return b"".join(chunks).decode(resp.encoding, errors="replace"), ctype
        except LookupError:
            return b"".join(chunks).decode("utf-8", errors="replace"), ctype


class PageTooLargeError(RuntimeError):
    """Страница превысила лимит загрузки и была отброшена."""


def _fmt_results(query: str, results, engine: str, from_cache: bool,
                 attempts) -> str:
    """Форматировать результаты поиска в markdown-список."""
    lines = [f"**Поиск:** {query}", f"_Движок: {engine}_"]
    if from_cache:
        lines.append("_(из кэша)_")
    if not results:
        return "\n".join(lines + ["", "Результатов нет."])
    for r in results:
        lines.append(f"\n{r.rank}. **{r.title}**")
        lines.append(f"   {r.url}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
    if attempts:
        lines.append("\n_" + " · ".join(attempts) + "_")
    return "\n".join(lines)


@mcp.tool()
def web_search(query: str, max_results: int = 8) -> str:
    """Искать в вебе через бесплатные движки (DuckDuckGo → Mojeek → Brave → SearxNG).

    Возвращает список: заголовок, URL, сниппет. При блокировке всех движков
    бросает ошибку с причиной по каждому движку — результаты не выдумываются.

    Args:
        query: поисковый запрос.
        max_results: максимум результатов (1–30).
    """
    max_results = max(1, min(int(max_results), 30))
    chain = _chain()
    result = chain.search(query, max_results=max_results)
    return _fmt_results(result.query, result.results, result.engine_used,
                        result.from_cache, result.attempts)


def _page_cache_get(url: str) -> tuple[str | None, list[str]]:
    """(markdown, ссылки) из кэша страниц; ``(None, [])`` при промахе.

    Запись — словарь, а не голая строка, потому что обходу документации нужны
    ещё и ссылки: иначе повторный обход кэшированной страницы не смог бы пойти
    дальше неё. Записи старого формата (просто строка) считаются промахом и
    перекачиваются один раз — молча и без ошибок.
    """
    cache = _get_cache()
    if cache is None:
        return None, []
    hit = cache.get(f"page:{url}", TTL_PAGE)
    if isinstance(hit, dict) and isinstance(hit.get("md"), str):
        links = hit.get("links")
        return hit["md"], links if isinstance(links, list) else []
    return None, []


def _page_cache_set(url: str, md: str, links: list[str]) -> None:
    cache = _get_cache()
    if cache is None:
        return
    try:
        cache.set(f"page:{url}", {"md": md, "links": links})
    except OSError:
        pass   # кэш не должен ломать чтение страницы


def _load_markdown(url: str) -> str:
    """Скачать страницу с кэшем и вернуть markdown. Общая для всех инструментов.

    Бинарные типы и превысившие лимит страницы дают человекочитаемую строку,
    а не исключение: в обходе документации одна тяжёлая ссылка не должна
    ронять весь вызов.
    """
    md, _links = _page_cache_get(url)
    if md is not None:
        return md + "\n\n_(из кэша)_"

    try:
        html, ctype = _get_html(url)
    except PageTooLargeError as exc:
        return f"Страница {url} пропущена: {exc}."

    if ctype and "html" not in ctype and "xml" not in ctype:
        return (f"Не-HTML контент ({ctype}) по адресу {url}. "
                "Скачивание бинарных типов не поддерживается.")

    # parse_page, а не html_to_markdown: ссылки достаются из того же soup
    # бесплатно и позволяют обходу не перекачивать страницу второй раз.
    md, links = parse_page(html, url)
    _page_cache_set(url, md, links)
    return md


@mcp.tool()
def fetch_page(url: str) -> str:
    """Скачать страницу и вернуть чистым markdown, сохраняя блоки кода и таблицы.

    Идеально для API-документации: ``<pre><code>`` остаются многострочными,
    таблицы превращаются в markdown-таблицы. Мусор (nav, footer, реклама)
    вырезается.

    Args:
        url: полный URL страницы (http/https).
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("url должен начинаться с http:// или https://")

    try:
        return _load_markdown(url)
    except httpx.HTTPError as exc:
        # Сетевая ошибка остаётся исключением — клиент должен её увидеть,
        # а не принять за содержимое страницы.
        raise type(exc)(f"не удалось скачать {url}: {exc}") from exc


@mcp.tool()
def crawl_docs(start_url: str, depth: int = 1, max_pages: int = 15) -> str:
    """Обойти документацию в пределах одного домена и вернуть склейку страниц.

    BFS от ``start_url`` по внутренним ссылкам до глубины ``depth``. Полезно,
    когда документация разбросана по нескольким страницам. Каждая страница —
    в markdown с сохранением кода.

    Args:
        start_url: стартовая страница документации.
        depth: глубина обхода по ссылкам (0 = только стартовая, 1 = + прямые ссылки).
        max_pages: максимум страниц (защита от бесконечного обхода).
    """
    if not start_url.startswith(("http://", "https://")):
        raise ValueError("start_url должен начинаться с http:// или https://")
    depth = max(0, min(int(depth), 3))
    max_pages = max(1, min(int(max_pages), 50))

    visited: dict[str, str] = {}
    # deque, а не list.pop(0): BFS на списке квадратичен по числу ссылок
    frontier: deque[tuple[str, int]] = deque([(start_url, 0)])
    seen = {start_url}
    total_chars = 0
    truncated = False

    while frontier and len(visited) < max_pages:
        url, lvl = frontier.popleft()

        md, links = _page_cache_get(url)
        if md is None:
            try:
                html, ctype = _get_html(url)
            except (httpx.HTTPError, PageTooLargeError):
                continue
            if ctype and "html" not in ctype and "xml" not in ctype:
                continue

            # Один разбор HTML на страницу: parse_page возвращает и markdown,
            # и ссылки из того же soup. Раньше страница парсилась дважды.
            md, links = parse_page(html, url)
            _page_cache_set(url, md, links)

        if total_chars + len(md) > _MAX_TOTAL_MD_CHARS and visited:
            # Потолок суммарного объёма достигнут: дальше ответ не влезет
            # в контекст клиента, обход прекращаем.
            truncated = True
            break

        visited[url] = md
        total_chars += len(md)

        if lvl < depth:
            for link in links:
                if link not in seen:
                    seen.add(link)
                    frontier.append((link, lvl + 1))

    if not visited:
        return f"Не удалось скачать ни одной страницы, начиная с {start_url}."

    parts = [f"Обход документации с {start_url}: собрано {len(visited)} страниц(ы)."]
    for url, md in visited.items():
        parts.append(f"\n\n{'=' * 70}\n{md}")
    if truncated:
        parts.append(
            f"\n\nОбход прерван: достигнут предел объёма "
            f"{_MAX_TOTAL_MD_CHARS // 1000} тыс. знаков. "
            f"Собрано {len(visited)} из {max_pages} запрошенных страниц."
        )
    return "".join(parts)


@mcp.tool()
def search_and_read(query: str, max_pages: int = 3) -> str:
    """Найти по запросу и сразу прочитать топ-страницы — одним вызовом.

    Сначала ``web_search``, затем ``fetch_page`` для первых ``max_pages``
    результатов. Удобно для быстрого ресёрча темы без ручных двух шагов.

    Args:
        query: поисковый запрос.
        max_pages: сколько топ-результатов прочитать (1–5).
    """
    max_pages = max(1, min(int(max_pages), 5))
    chain = _chain()
    result = chain.search(query, max_results=max_pages)

    out = [_fmt_results(result.query, result.results, result.engine_used,
                        result.from_cache, result.attempts)]
    for r in result.results[:max_pages]:
        try:
            md = _load_markdown(r.url)
        except (httpx.HTTPError, ValueError) as exc:
            md = f"_(не удалось прочитать: {exc})_"
        out.append(f"\n\n{'=' * 70}\n{md}")
    return "".join(out)


@mcp.tool()
def cache_stats() -> str:
    """Статистика дискового кэша (число записей, размер, путь)."""
    cache = _get_cache()
    if cache is None:
        return (f"Кэш недоступен: {_cache_error}\n"
                "Поиск и чтение страниц работают, но результаты не сохраняются.")
    s = cache.stats()
    return (f"Записей: {s['entries']}\n"
            f"Размер: {s['bytes'] / 1024:.1f} KiB\n"
            f"Путь: {s['path']}")


@mcp.tool()
def cache_clear() -> str:
    """Очистить дисковый кэш поиска и страниц."""
    cache = _get_cache()
    if cache is None:
        return f"Кэш недоступен: {_cache_error}"
    return f"Удалено записей: {cache.clear()}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
