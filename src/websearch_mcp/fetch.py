"""Скачивание страницы и конвертация в markdown.

Главная задача — сохранить блоки кода и таблицы, потому что основной кейс
это чтение API-документации. ``readability-lxml`` иногда возвращает пустой
``<body>`` на коротких страницах (проверено), поэтому есть fallback на весь
``<body>`` целиком.

HTML парсится **один раз на страницу**: soup переиспользуется и для выбора
основного контента, и для конвертации в markdown, и для сбора ссылок. Раньше
здесь было до трёх независимых парсингов одного и того же текста, а BeautifulSoup
раздувает его в памяти примерно в 20 раз — на больших страницах это и медленно,
и дорого по памяти.
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter
from readability import Document

from .parse import BS_PARSER, base_url_host, make_soup

# Мусор, который вырезаем до извлечения основного контента.
_JUNK_TAGS = ("script", "style", "noscript", "template", "svg", "iframe",
              "form", "button", "select", "textarea", "nav", "footer", "header")
_JUNK_SELECTORS = (
    '[role="banner"]', '[role="navigation"]', '[role="contentinfo"]',
    ".cookie-banner", ".cookie-notice", ".newsletter", ".ads", ".advertisement",
    "#comments", ".comments", ".social-share", ".sidebar", "#sidebar",
    ".breadcrumb", ".breadcrumbs", ".toc", "#toc",
)

# Приоритетные контейнеры основного контента (документация, статьи).
_MAIN_SELECTORS = ("article", "main", '[role="main"]', "#content", ".content",
                   "#main", ".main", "#doc", ".docs", ".documentation",
                   "#readme", "article.markdown-body")

_MAX_MD_CHARS = 60_000
# Порог «здесь есть осмысленный текст»: контейнер короче считается хромом.
_MIN_TEXT_CHARS = 200

_WS = re.compile(r"\n{3,}")

# markdownify умеет принимать уже разобранный soup через convert_soup,
# поэтому bs4_options достаточно задать один раз.
_MD_OPTIONS = {
    "heading_style": "ATX",
    "bullets": "-",
    "code_language": "",
    "autolinks": True,
    "strip": ["img"],        # картинки в текстовой выдаче только мешают
    "bs4_options": {"features": BS_PARSER},
}


def _strip_junk(soup: BeautifulSoup) -> None:
    for tag in soup(_JUNK_TAGS):
        tag.decompose()
    for sel in _JUNK_SELECTORS:
        for el in soup.select(sel):
            el.decompose()


def _text_len(node) -> int:
    return len(node.get_text(strip=True))


def _extract_main(soup: BeautifulSoup, html: str, url: str) -> tuple[str, object]:
    """Вернуть (заголовок, узел основного контента) из уже разобранного soup.

    Возвращается именно узел дерева, а не его сериализация: markdownify
    принимает soup напрямую, так что повторно парсить ничего не нужно.

    Ожидает soup, из которого уже вырезан мусор (см. ``parse_page``).
    """
    title = _clean_title(soup.title.get_text()) if soup.title else ""

    # Приоритетные контейнеры основного контента (документация, статьи).
    for sel in _MAIN_SELECTORS:
        hit = soup.select_one(sel)
        if hit and _text_len(hit) > _MIN_TEXT_CHARS:
            return title, hit

    # readability как запасной вариант извлечения. Он работает со строкой,
    # поэтому Document создаётся один раз и переиспользуется для заголовка.
    doc = None
    try:
        doc = Document(html)
        summary = doc.summary()
        if summary:
            summary_soup = make_soup(summary)
            if _text_len(summary_soup) > _MIN_TEXT_CHARS:
                # summary уже разобран — отдаём узел его body, а не строку
                node = summary_soup.body or summary_soup
                return title or _clean_title(doc.short_title()), node
    except Exception:
        doc = None

    fallback_title = title
    if not fallback_title:
        fallback_title = _readability_title(doc, url)
    return fallback_title, (soup.body or soup)


def _readability_title(doc: Document | None, url: str) -> str:
    """Заголовок из уже созданного Document; домен — если его нет или он упал."""
    if doc is None:
        return urlparse(url).netloc
    try:
        return _clean_title(doc.short_title())
    except Exception:
        return urlparse(url).netloc


def _clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    # readability возвращает "[no-title]" когда заголовка нет
    return "" if t in ("[no-title]", "[No-title]") else t


def parse_page(html: str, url: str) -> tuple[str, list[str]]:
    """Разобрать страницу один раз и вернуть (markdown, ссылки того же домена).

    Это единственная точка, где HTML превращается в soup: обход документации
    вызывает её вместо отдельных ``html_to_markdown`` и ``extract_links``,
    которые раньше разбирали одну и ту же страницу дважды.
    """
    soup = make_soup(html)
    # Ссылки собираются до вырезания мусора: nav/footer тоже часть структуры
    # документации, а сам soup при этом разбирается один раз на страницу.
    links = _links_from_soup(soup, url)
    _strip_junk(soup)

    title, node = _extract_main(soup, html, url)
    md = MarkdownConverter(**_MD_OPTIONS).convert_soup(node)

    md = _WS.sub("\n\n", md).strip()
    if len(md) > _MAX_MD_CHARS:
        dropped = len(md) - _MAX_MD_CHARS
        md = md[:_MAX_MD_CHARS].rstrip() + f"\n\n…[обрезано, ещё {dropped} симв.]"

    header = f"# {title}\n\n" if title else ""
    return f"{header}> Источник: {url}\n\n{md}".strip() + "\n", links


def html_to_markdown(html: str, url: str) -> str:
    """Конвертировать HTML страницы в markdown, сохраняя код и таблицы."""
    md, _links = parse_page(html, url)
    return md


def _links_from_soup(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Ссылки того же домена в абсолютном виде, без якорей и служебных схем.

    Порядок — как в документе (это обычно и есть порядок чтения документации).

    Важно: вызывается **до** ``_strip_junk``, иначе ссылки из nav/footer
    потеряются — они тоже часть структуры документации.
    """
    base_host = base_url_host(base_url)
    out: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute, _frag = urldefrag(urljoin(base_url, href))   # убрать #якорь
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if base_url_host(absolute) != base_host:
            continue
        # не ползаем по картинкам и бинарникам
        if parsed.path.lower().endswith(_BINARY_SUFFIXES):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)

    return out


_BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".mp4", ".webm", ".css", ".js",
)


def extract_links(html: str, base_url: str) -> list[str]:
    """Публичная оболочка для обхода по готовому HTML.

    Внутри ``crawl_docs`` используется ``parse_page`` — она собирает ссылки
    из того же soup, что и markdown, не разбирая страницу второй раз.
    """
    return _links_from_soup(make_soup(html), base_url)
