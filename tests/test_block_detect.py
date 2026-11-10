"""Детект блокировки/капчи по содержимому, а не по коду ответа."""

import pytest

from websearch_mcp.parse import BlockReason, detect_block

CHALLENGE_PAGES = [
    ('<html><body><h1>Please solve this challenge to continue</h1></body></html>'),
    '<html><body>An unusual traffic pattern was detected from your network.</body></html>',
    '<html><body>Enable JavaScript and cookies to continue</body></html>',
    '<html><head><title>Attention Required! | Cloudflare</title></head><body>x</body></html>',
]


@pytest.mark.parametrize("html", CHALLENGE_PAGES)
def test_detects_challenge_text(html):
    reason = detect_block(html, status_code=200, n_results=0)
    assert reason is not None
    assert reason.kind == BlockReason.CHALLENGE


def test_detects_http_202_as_block():
    """202 Accepted — стандартный способ DDG отдать «потом»."""
    reason = detect_block("<html><body></body></html>", status_code=202, n_results=0)
    assert reason is not None
    assert reason.kind == BlockReason.RATE_LIMIT


def test_detects_http_429():
    reason = detect_block("", status_code=429, n_results=0)
    assert reason is not None
    assert reason.kind == BlockReason.RATE_LIMIT


def test_detects_zero_results_on_200():
    """Ноль распарсенных результатов при успешном ответе — тоже блок,
    иначе сервер вернёт пустоту как «ничего не найдено»."""
    reason = detect_block("<html><body><div>ok</div></body></html>", status_code=200, n_results=0)
    assert reason is not None
    assert reason.kind == BlockReason.EMPTY


def test_normal_page_is_not_blocked():
    assert detect_block("<html><body>hello</body></html>", status_code=200, n_results=5) is None


def test_challenge_wins_over_empty():
    reason = detect_block(CHALLENGE_PAGES[0], status_code=200, n_results=0)
    assert reason.kind == BlockReason.CHALLENGE


def test_reason_carries_snippet_of_the_page():
    """Причина должна содержать кусок страницы — чтобы doctor показал, что именно
    отдал поисковик, а не просто «что-то пошло не так»."""
    reason = detect_block(CHALLENGE_PAGES[1], status_code=200, n_results=0)
    assert "unusual traffic" in reason.detail.lower()
