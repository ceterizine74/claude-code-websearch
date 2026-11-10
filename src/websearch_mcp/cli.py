"""CLI для ручного использования: те же инструменты, что видит MCP-клиент.

    websearch search "httpx timeout config"
    websearch fetch  https://example.org/docs
    websearch crawl  https://example.org/docs --depth 2
    websearch read   "httpx timeout config" --pages 3
    websearch doctor
    websearch cache stats|clear|prune
"""

from __future__ import annotations

import logging
import sys

import click

# httpx логирует каждый запрос на INFO и перебивает наш вывод
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from .cache import Cache, TTL_PAGE
from .engines import AllEnginesBlockedError, DEFAULT_ENGINE_ORDER, EngineChain
from .server import cache_clear, cache_stats, crawl_docs, fetch_page, search_and_read, web_search


def _echo(text: str) -> None:
    """Печатать в stdout, переживая консоль с не-UTF-8 кодировкой.

    На macOS и Linux UTF-8 работает из коробки, а старая Windows-консоль
    (cp866/cp1251) может не суметь напечатать часть символов — тогда пишем
    байты с заменой неподдерживаемых.
    """
    try:
        click.echo(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write(text.encode(enc, errors="replace"))
        sys.stdout.buffer.write(b"\n")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Локальный веб-поиск: DuckDuckGo → Mojeek → Brave → SearxNG, без ключей."""


@main.command()
@click.argument("query")
@click.option("-n", "--max-results", default=8, show_default=True, help="Сколько результатов показать.")
def search(query: str, max_results: int) -> None:
    """Искать в вебе."""
    try:
        _echo(web_search(query, max_results=max_results))
    except AllEnginesBlockedError as exc:
        _echo(str(exc))
        raise SystemExit(1)


@main.command()
@click.argument("url")
def fetch(url: str) -> None:
    """Скачать страницу и показать markdown."""
    _echo(fetch_page(url))


@main.command()
@click.argument("start_url")
@click.option("-d", "--depth", default=1, show_default=True, help="Глубина обхода ссылок (0–3).")
@click.option("-m", "--max-pages", default=15, show_default=True, help="Максимум страниц (1–50).")
def crawl(start_url: str, depth: int, max_pages: int) -> None:
    """Обойти документацию в пределах домена."""
    _echo(crawl_docs(start_url, depth=depth, max_pages=max_pages))


@main.command()
@click.argument("query")
@click.option("-p", "--pages", default=3, show_default=True, help="Сколько топов прочитать (1–5).")
def read(query: str, pages: int) -> None:
    """Поиск + чтение топ-страниц одним вызовом."""
    try:
        _echo(search_and_read(query, max_pages=pages))
    except AllEnginesBlockedError as exc:
        _echo(str(exc))
        raise SystemExit(1)


@main.command()
@click.option("-q", "--query", default="test", show_default=True, help="Пробный запрос.")
def doctor(query: str) -> None:
    """Проверить, какие движки живы прямо сейчас."""
    click.echo("Проверка движков (это реальные сетевые запросы)...\n")
    chain = EngineChain(engines=DEFAULT_ENGINE_ORDER)
    alive = 0
    try:
        for attempt in chain.diagnose(query):
            if attempt.ok:
                alive += 1
                click.secho(
                    f"  ✓ {attempt.engine:<12} результатов: {attempt.n_results}  "
                    f"({attempt.elapsed_ms} ms)",
                    fg="green",
                )
            else:
                reason = attempt.block
                kind = reason.kind.value if reason else "unknown"
                detail = reason.detail if reason else ""
                click.secho(f"  ✗ {attempt.engine:<12} {kind}: {detail[:100]}", fg="red")
    finally:
        chain.close()

    click.echo()
    if alive:
        click.secho(f"Живых движков: {alive}/{len(DEFAULT_ENGINE_ORDER)} — поиск работоспособен.",
                    fg="green")
    else:
        click.secho("Ни один движок не ответил. Проверьте интернет или попробуйте позже.",
                    fg="red")
        raise SystemExit(1)


@main.group()
def cache() -> None:
    """Управление дисковым кэшем."""


@cache.command("stats")
def cache_stats_cmd() -> None:
    """Показать статистику кэша."""
    _echo(cache_stats())


@cache.command("clear")
def cache_clear_cmd() -> None:
    """Очистить кэш."""
    _echo(cache_clear())


@cache.command("prune")
def cache_prune_cmd() -> None:
    """Удалить только протухшие записи (свежие оставить)."""
    # Сервер чистит кэш сам раз в 32 записи; команда нужна, чтобы убрать
    # накопившееся, не запуская сервер.
    n = Cache().prune(TTL_PAGE)
    _echo(f"Удалено протухших записей: {n}")


if __name__ == "__main__":
    main()
