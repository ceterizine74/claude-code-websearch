"""Парсинг HTML-выдачи DuckDuckGo."""

from websearch_mcp.parse import parse_ddg

# Реальная структура html.duckduckgo.com: ссылки-редиректы через uddg,
# сниппет в <a class="result__snippet">. uddg-значение — это URL-кодированный
# https://github.com/acme/widgets
DDG_RESULTS = """
<html><body>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Facme%2Fwidgets&amp;rut=5f2a">
        acme / <b>widgets</b> · GitHub</a>
    </h2>
    <div class="result__extras__url">https://github.com/acme/widgets</div>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fx">
      Widgets is a <b>toolkit</b> library. API documented under /api/v2.</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="https://direct.example.org/docs">Direct link</a>
    </h2>
  </div>
</div>
<div class="nav-link">Next page</div>
</body></html>
"""


def test_parses_two_results():
    out = parse_ddg(DDG_RESULTS)
    assert len(out) == 2


def test_decodes_redirect_url():
    out = parse_ddg(DDG_RESULTS)
    assert out[0].url == "https://github.com/acme/widgets"


def test_keeps_direct_url():
    out = parse_ddg(DDG_RESULTS)
    assert out[1].url == "https://direct.example.org/docs"


def test_strips_html_from_title_and_snippet():
    out = parse_ddg(DDG_RESULTS)
    assert "<b>" not in out[0].title
    assert out[0].title == "acme / widgets · GitHub"
    assert "<b>" not in out[0].snippet
    assert out[0].snippet.startswith("Widgets is a toolkit library.")


def test_assigns_engine_and_rank():
    out = parse_ddg(DDG_RESULTS)
    assert [r.rank for r in out] == [1, 2]
    assert all(r.engine == "duckduckgo" for r in out)


def test_missing_snippet_yields_empty_string():
    out = parse_ddg(DDG_RESULTS)
    assert out[1].snippet == ""


DDG_EMPTY = """
<html><body><div class="no-results">No results found</div></body></html>
"""


def test_empty_page_returns_empty_list():
    assert parse_ddg(DDG_EMPTY) == []
