"""Дисковый кэш с TTL.

Один JSON-файл на ключ, имя — sha256 (безопасно для Windows-путей).
Повреждённая запись считается промахом, а не падением: кэш не должен
уметь ломать сервер.

Расположение выбирается по платформе, потому что сервер должен одинаково
работать на Windows, macOS и Linux:

* Windows — ``%LOCALAPPDATA%\\websearch-mcp\\cache``
* macOS   — ``~/Library/Caches/websearch-mcp``
* Linux   — ``$XDG_CACHE_HOME/websearch-mcp`` или ``~/.cache/websearch-mcp``
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# TTL по умолчанию
TTL_SEARCH = 6 * 60 * 60      # 6 часов
TTL_PAGE = 24 * 60 * 60       # 24 часа

# Протухшие записи вычищаются сами, иначе кэш растёт бесконечно. Чистка
# дорогая (обход дерева), поэтому выполняется не на каждую запись.
_PRUNE_EVERY = 32
_MAX_TTL = max(TTL_SEARCH, TTL_PAGE)


def default_cache_dir() -> Path:
    """Каталог кэша по соглашениям текущей ОС."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "websearch-mcp" / "cache"
        return Path.home() / "AppData" / "Local" / "websearch-mcp" / "cache"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "websearch-mcp"

    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "websearch-mcp"
    return Path.home() / ".cache" / "websearch-mcp"


class Cache:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_cache_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self._writes_since_prune = 0

    # ------------------------------------------------------------------

    def path_for(self, key: str) -> Path:
        """Стабильное имя файла для ключа (sha256, безопасно для Windows)."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        # первый уровень — 2 символа, чтобы не складывать всё в одну папку
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, key: str, ttl: int) -> Any | None:
        """Вернуть значение или None (промах / протухло / повреждено)."""
        path = self.path_for(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        try:
            envelope = json.loads(raw)
            stored_at = envelope["t"]
            value = envelope["v"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

        if time.time() - float(stored_at) > ttl:
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"t": time.time(), "v": value}, ensure_ascii=False)

        # атомарная запись: сначала рядом, потом rename
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

        self._maybe_prune()

    def _maybe_prune(self) -> None:
        """Периодически вычищать протухшее, чтобы кэш не рос бесконечно.

        Обход дерева недешёв, поэтому чистим не на каждую запись, а раз в
        ``_PRUNE_EVERY``. Сбой чистки не должен ломать сохранение результата.
        """
        self._writes_since_prune += 1
        if self._writes_since_prune < _PRUNE_EVERY:
            return
        self._writes_since_prune = 0
        try:
            self.prune(_MAX_TTL)
        except OSError:
            pass

    # ------------------------------------------------------------------

    def _entries(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [p for p in self.root.rglob("*.json") if p.is_file()]

    def stats(self) -> dict[str, Any]:
        entries = self._entries()
        total = 0
        for p in entries:
            try:
                total += p.stat().st_size
            except OSError:
                continue
        return {"entries": len(entries), "bytes": total, "path": str(self.root)}

    def clear(self) -> int:
        """Удалить все записи, вернуть их количество."""
        entries = self._entries()
        removed = 0
        for p in entries:
            try:
                p.unlink()
                removed += 1
            except OSError:
                continue
        # подчистить пустые подпапки
        for d in sorted(self.root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
        return removed

    def prune(self, ttl: int) -> int:
        """Удалить протухшие записи, вернуть их количество."""
        removed = 0
        for p in self._entries():
            try:
                envelope = json.loads(p.read_text(encoding="utf-8"))
                if time.time() - float(envelope["t"]) > ttl:
                    p.unlink()
                    removed += 1
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                # нечитаемую запись тоже убираем
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed
