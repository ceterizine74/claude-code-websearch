"""Дисковый кэш с TTL."""

from websearch_mcp.cache import Cache


def test_miss_returns_none(tmp_path):
    c = Cache(tmp_path)
    assert c.get("nope", ttl=60) is None


def test_roundtrip(tmp_path):
    c = Cache(tmp_path)
    payload = {"results": [{"title": "a", "url": "https://x.y"}]}
    c.set("query:widgets", payload)
    assert c.get("query:widgets", ttl=60) == payload


def test_persists_across_instances(tmp_path):
    Cache(tmp_path).set("k", {"v": 1})
    assert Cache(tmp_path).get("k", ttl=60) == {"v": 1}


def test_expired_entry_is_miss(tmp_path):
    c = Cache(tmp_path)
    c.set("k", {"v": 1})
    assert c.get("k", ttl=-1) is None


def test_stats_counts_entries(tmp_path):
    c = Cache(tmp_path)
    c.set("a", {"1": 1})
    c.set("b", {"2": 2})
    stats = c.stats()
    assert stats["entries"] == 2
    assert stats["bytes"] > 0


def test_clear_removes_everything(tmp_path):
    c = Cache(tmp_path)
    c.set("a", {"1": 1})
    removed = c.clear()
    assert removed == 1
    assert c.get("a", ttl=60) is None


def test_corrupted_entry_is_miss_not_crash(tmp_path):
    c = Cache(tmp_path)
    c.set("k", {"v": 1})
    path = c.path_for("k")
    path.write_text("{ not json", encoding="utf-8")
    assert c.get("k", ttl=60) is None


def test_keys_are_hashed_to_safe_filenames(tmp_path):
    c = Cache(tmp_path)
    c.set("https://example.org/a?b=c&d=e#f", {"v": 1})
    names = [p.name for p in tmp_path.rglob("*") if p.is_file()]
    assert names
    for n in names:
        # никаких недопустимых в Windows символов
        assert not any(ch in n for ch in '<>:"/\\|?*')


def test_stats_bytes_on_empty_cache(tmp_path):
    assert Cache(tmp_path).stats() == {"entries": 0, "bytes": 0, "path": str(tmp_path)}
