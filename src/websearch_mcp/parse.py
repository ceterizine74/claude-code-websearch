"""Парсинг HTML-выдачи бесплатных поисковиков и детект блокировки.

Все парсеры пишутся «терпимо»: поисковики регулярно меняют вёрстку, поэтому
каждый парсер сначала пробует точные селекторы своего движка, а затем
универсальный fallback (заголовок-ссылка + ближайший абзац).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

_WHITESPACE = re.compile(r"\s+")

# lxml заметно быстрее html.parser на больших страницах документации (~-27%)
# и уже установлен как зависимость readability-lxml. Но это компилируемый
# пакет: если на какой-то платформе колёс не нашлось, тихо откатываемся
# на чисто питоновский парсер — функциональность важнее скорости.
try:  # pragma: no cover - зависит от окружения
    import lxml.html as _lxml_html  # noqa: F401

    BS_PARSER = "lxml"
except ImportError:  # pragma: no cover
    BS_PARSER = "html.parser"


def make_soup(markup: str) -> BeautifulSoup:
    """Единая точка создания soup: один и тот же парсер во всём проекте."""
    return BeautifulSoup(markup or "", BS_PARSER)


def _clean(text: str | None) -> str:
    """Схлопнуть переводы строк и лишние пробелы в одну строку."""
    if not text:
        return ""
    return _WHITESPACE.sub(" ", text).strip()


@dataclass(frozen=True)
class SearchResult:
    """Один результат поиска."""

    rank: int
    title: str
    url: str
    snippet: str
    engine: str


class BlockReason(Enum):
    """Почему ответ поисковика считается блокировкой."""

    CHALLENGE = "challenge"      # капча / «решите задачу» / interstitial
    RATE_LIMIT = "rate_limit"    # 429 / 202 — движок просит притормозить
    EMPTY = "empty"              # ответ успешный, но результатов ноль
    HTTP_ERROR = "http_error"    # 4xx/5xx прочие
    NETWORK = "network"          # не удалось достучаться


@dataclass(frozen=True)
class Block:
    """Зафиксированная блокировка с объяснением для человека."""

    kind: BlockReason
    detail: str


# Маркеры interstitial-страниц. Проверяются только когда результатов ноль —
# иначе страница документации, где упоминается Cloudflare, считалась бы блоком.
_CHALLENGE_MARKERS = (
    "solve this challenge",
    "unusual traffic",
    "enable javascript and cookies",
    "attention required",
    "just a moment",
    "captcha",
    "not a robot",
    "access denied",
    "verify you are human",
    "are you a robot",
)

_SNIPPET_LEN = 220


def detect_block(html: str, status_code: int, n_results: int) -> Block | None:
    """Определить блокировку по содержимому, а не только по коду ответа.

    Возвращает ``None``, если ответ выглядит рабочим.
    """
    if status_code in (429, 202):
        return Block(BlockReason.RATE_LIMIT, f"HTTP {status_code}: движок просит притормозить")

    if n_results == 0:
        low = (html or "").lower()
        for marker in _CHALLENGE_MARKERS:
            if marker in low:
                idx = low.index(marker)
                detail = _clean(html[max(0, idx - 60): idx + 160])
                return Block(BlockReason.CHALLENGE, detail[:_SNIPPET_LEN] or marker)
        if status_code >= 400:
            return Block(BlockReason.HTTP_ERROR, f"HTTP {status_code}")
        return Block(
            BlockReason.EMPTY,
            "ответ успешный, но результатов не распарсилось ни одного "
            "(возможно, вёрстка изменилась или запрос действительно пустой)",
        )

    if status_code >= 400:
        return Block(BlockReason.HTTP_ERROR, f"HTTP {status_code}")

    return None


# --------------------------------------------------------------------------
# Универсальные помощники
# --------------------------------------------------------------------------

def _decode_ddg_redirect(href: str, base_url: str) -> str:
    """DDG отдаёт ссылки через редирект /l/?uddg=<закодированный URL>."""
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.path.endswith("/l/") or "duckduckgo.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        target = qs.get("uddg")
        if target:
            return unquote(target[0])
    return absolute


def _snippet_near(node) -> str:
    """Сниппет — ближайший правдоподобный текст рядом со ссылкой результата."""
    for sel in (".result__snippet", ".snippet-description", ".s", "p.s",
                "p.content", "p", ".result-snippet"):
        hit = node.select_one(sel)
        if hit:
            text = _clean(hit.get_text(" ", strip=True))
            if len(text) > 20:
                return text[:_SNIPPET_LEN]
    return ""


def _generic_parse(soup: BeautifulSoup, engine: str, base_url: str,
                   title_selectors: tuple[str, ...]) -> list[SearchResult]:
    """Fallback: заголовок-ссылка + родительский контейнер со сниппетом."""
    out: list[SearchResult] = []
    seen: set[str] = set()

    for sel in title_selectors:
        for a in soup.select(sel):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            url = _decode_ddg_redirect(href, base_url)
            host = urlparse(url).netloc.lower()
            # собственные страницы поисковика (справка, настройки) не являются результатом
            if not host or host.endswith(base_url_host(base_url)):
                continue
            if url in seen:
                continue
            title = _clean(a.get_text(" ", strip=True))
            if not title:
                continue
            container = a.find_parent(["div", "li", "article", "section"]) or a.parent
            snippet = _snippet_near(container) if container else ""
            seen.add(url)
            out.append(SearchResult(
                rank=len(out) + 1, title=title, url=url,
                snippet=snippet, engine=engine,
            ))
        if out:
            break
    return out


def base_url_host(base_url: str) -> str:
    host = urlparse(base_url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------
# DuckDuckGo
# --------------------------------------------------------------------------

def parse_ddg(html: str) -> list[SearchResult]:
    """html.duckduckgo.com: div.result > h2.result__title > a.result__a."""
    soup = make_soup(html)
    base = "https://html.duckduckgo.com/"
    out: list[SearchResult] = []
    seen: set[str] = set()

    for div in soup.select("div.result, div.web-result, div.links_main"):
        a = div.select_one("a.result__a")
        if a is None:
            continue
        href = (a.get("href") or "").strip()
        if not href:
            continue
        url = _decode_ddg_redirect(href, base)
        if not url.startswith("http") or url in seen:
            continue
        snippet_el = div.select_one("a.result__snippet, .result__snippet")
        out.append(SearchResult(
            rank=len(out) + 1,
            title=_clean(a.get_text(" ", strip=True)),
            url=url,
            snippet=_clean(snippet_el.get_text(" ", strip=True))[:_SNIPPET_LEN] if snippet_el else "",
            engine="duckduckgo",
        ))
        seen.add(url)

    if out:
        return out
    return _generic_parse(soup, "duckduckgo", base, ("h2 a", ".result__a", "h3 a"))


# --------------------------------------------------------------------------
# Mojeek
# --------------------------------------------------------------------------

def parse_mojeek(html: str) -> list[SearchResult]:
    """www.mojeek.com: ol.results-* > li > h2 > a.title."""
    soup = make_soup(html)
    base = "https://www.mojeek.com/"
    out: list[SearchResult] = []
    seen: set[str] = set()

    for li in soup.select("li"):
        a = li.select_one("h2 a, a.title, a.ob")
        if a is None:
            continue
        href = (a.get("href") or "").strip()
        if not href.startswith("http") or href in seen:
            continue
        title = _clean(a.get_text(" ", strip=True))
        if not title:
            continue
        snippet = ""
        for sel in ("p.s", ".s", "p.desc", "p"):
            hit = li.select_one(sel)
            if hit:
                text = _clean(hit.get_text(" ", strip=True))
                if len(text) > 20:
                    snippet = text[:_SNIPPET_LEN]
                    break
        out.append(SearchResult(
            rank=len(out) + 1, title=title, url=href,
            snippet=snippet, engine="mojeek",
        ))
        seen.add(href)

    if out:
        return out
    return _generic_parse(soup, "mojeek", base, ("h2 a", "a.title", "h3 a"))


# --------------------------------------------------------------------------
# Brave
# --------------------------------------------------------------------------

def parse_brave(html: str) -> list[SearchResult]:
    """search.brave.com: div.snippet[data-type=web] > a.h."""
    soup = make_soup(html)
    base = "https://search.brave.com/"
    out: list[SearchResult] = []
    seen: set[str] = set()

    for div in soup.select('div[data-type="web"], div.snippet, div[data-pos]'):
        a = div.select_one("a.h, a[href^='http']")
        if a is None:
            continue
        href = (a.get("href") or "").strip()
        if not href.startswith("http") or href in seen:
            continue
        if "search.brave.com" in href:
            continue
        title_el = div.select_one(".title, .heading, h2, h3")
        title = _clean(title_el.get_text(" ", strip=True)) if title_el else ""
        if not title:
            title = _clean(a.get_text(" ", strip=True))
        if not title:
            continue
        out.append(SearchResult(
            rank=len(out) + 1, title=title, url=href,
            snippet=_snippet_near(div), engine="brave",
        ))
        seen.add(href)

    if out:
        return out
    return _generic_parse(soup, "brave", base, ("a.h", ".title a", "h2 a", "h3 a"))


# --------------------------------------------------------------------------
# SearxNG (публичные инстансы)
# --------------------------------------------------------------------------

def parse_searxng(html: str) -> list[SearchResult]:
    """Инстанс SearxNG: article.result > h3 > a. Поддерживает и JSON-ответ."""
    text = (html or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        out: list[SearchResult] = []
        for i, item in enumerate(data.get("results") or [], start=1):
            url = item.get("url")
            if not url:
                continue
            out.append(SearchResult(
                rank=i,
                title=_clean(item.get("title") or url),
                url=url,
                snippet=_clean(item.get("content") or "")[:_SNIPPET_LEN],
                engine="searxng",
            ))
        return out

    soup = make_soup(text)
    base = "https://searx.be/"
    out = []
    seen: set[str] = set()

    for art in soup.select("article.result, article"):
        a = art.select_one("h3 a, h2 a, a.url_header, a")
        if a is None:
            continue
        href = (a.get("href") or "").strip()
        if not href.startswith("http") or href in seen:
            continue
        title = _clean(a.get_text(" ", strip=True))
        if not title:
            continue
        out.append(SearchResult(
            rank=len(out) + 1, title=title, url=href,
            snippet=_snippet_near(art), engine="searxng",
        ))
        seen.add(href)

    if out:
        return out
    return _generic_parse(soup, "searxng", base, ("article h3 a", "h3 a", "h2 a"))
