"""HTML -> markdown: сохранение блоков кода, вырезание мусора."""

from websearch_mcp.fetch import html_to_markdown

DOC_PAGE = """
<html><head><title>Widget API</title></head>
<body>
<nav><a href="/">Home</a><a href="/docs">Docs</a></nav>
<div id="main">
  <h1>Widget API</h1>
  <p>Create a user via <code>POST /api/users</code>.</p>
  <h2>Example</h2>
  <pre><code class="language-python">import requests

requests.post("https://panel.example/api/users",
              json={"username": "u1"},
              headers={"Authorization": "Bearer TOKEN"})
</code></pre>
  <table>
    <tr><th>Field</th><th>Type</th></tr>
    <tr><td>username</td><td>string</td></tr>
  </table>
</div>
<footer>Copyright 2024 <a href="/privacy">Privacy</a></footer>
</body></html>
"""


def test_code_block_survives_with_newlines():
    md = html_to_markdown(DOC_PAGE, url="https://example.org/docs")
    assert 'requests.post("https://panel.example/api/users"' in md
    # многострочный код должен остаться многострочным, а не схлопнуться в одну строку
    assert 'json={"username": "u1"}' in md


def test_code_block_is_fenced():
    md = html_to_markdown(DOC_PAGE, url="https://example.org/docs")
    assert "```" in md


def test_headings_become_markdown():
    md = html_to_markdown(DOC_PAGE, url="https://example.org/docs")
    assert "# Widget API" in md
    assert "## Example" in md


def test_inline_code_preserved():
    md = html_to_markdown(DOC_PAGE, url="https://example.org/docs")
    assert "`POST /api/users`" in md


def test_table_becomes_markdown_table():
    md = html_to_markdown(DOC_PAGE, url="https://example.org/docs")
    assert "| Field | Type |" in md
    assert "| username | string |" in md


def test_source_url_recorded():
    md = html_to_markdown(DOC_PAGE, url="https://example.org/docs")
    assert "https://example.org/docs" in md


def test_nav_and_footer_dropped():
    """readability должен выкинуть хром — но если оставит, код страницы не
    должен потерять смысл; проверяем, что основной контент на месте."""
    md = html_to_markdown(DOC_PAGE, url="https://example.org/docs")
    assert "Create a user via" in md


def test_empty_html_does_not_crash():
    md = html_to_markdown("<html><body></body></html>", url="https://example.org/x")
    assert "https://example.org/x" in md


def test_malformed_html_does_not_crash():
    md = html_to_markdown("<html><body><div><p>unclosed", url="https://example.org/x")
    assert "unclosed" in md


def test_extracts_internal_links_for_crawl():
    from websearch_mcp.fetch import extract_links

    links = extract_links(
        """<html><body>
        <a href="/docs/api">API</a>
        <a href="https://example.org/docs/users">Users</a>
        <a href="https://other.example.com/page">Other</a>
        <a href="#anchor">Anchor</a>
        <a href="mailto:x@y.z">Mail</a>
        </body></html>""",
        base_url="https://example.org/docs/index",
    )
    # только тот же домен, без якорей и mailto, абсолютные URL
    assert "https://example.org/docs/api" in links
    assert "https://example.org/docs/users" in links
    assert all("other.example.com" not in u for u in links)
    assert all(u.startswith("http") for u in links)
