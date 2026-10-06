"""Small TTL cache behind a two-method interface (get/set), so a Firestore backend can slot in later."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

DEFAULT_TTL = 24 * 3600.0


class Cache(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl: float) -> None: ...


def cache_key(path: str, params: dict[str, str]) -> str:
    """Endpoint + params sorted by name. The API key travels in a header, so it is never part of this."""
    return path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


class MemoryCache:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= self._clock():
            del self._items[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._items[key] = (self._clock() + ttl, value)

    def keys(self) -> list[str]:
        return list(self._items)


class DiskCache:
    """One JSON file per entry under `root` (e.g. .cache/qloo/), named by a hash of the key."""

    def __init__(self, root: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def _file(self, key: str) -> Path:
        return self.root / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:40] + ".json")

    def get(self, key: str) -> Any | None:
        try:
            entry = json.loads(self._file(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if entry.get("key") != key or entry.get("expires_at", 0) <= self._clock():
            return None
        return entry.get("value")

    def set(self, key: str, value: Any, ttl: float) -> None:
        target = self._file(key)
        tmp = target.with_suffix(".tmp")
        entry = {"key": key, "expires_at": self._clock() + ttl, "value": value}
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
