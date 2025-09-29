"""Simple in-memory TTL cache with monotonic expiry."""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Generic, MutableMapping, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass
class _Entry(Generic[V]):
    value: V
    expires_at: float


class TTLCache(Generic[K, V]):
    """Thread-safe TTL cache keyed by arbitrary hashable objects."""

    def __init__(self, default_ttl: float) -> None:
        if default_ttl <= 0:
            raise ValueError("default_ttl must be > 0")
        self._default_ttl = default_ttl
        self._items: MutableMapping[K, _Entry[V]] = {}
        self._lock = RLock()

    def get(self, key: K) -> Optional[V]:
        with self._lock:
            entry = self._items.get(key)
            if not entry:
                return None
            if entry.expires_at <= monotonic():
                self._items.pop(key, None)
                return None
            return entry.value

    def set(self, key: K, value: V, ttl: Optional[float] = None) -> None:
        ttl_value = ttl if ttl is not None else self._default_ttl
        if ttl_value <= 0:
            raise ValueError("ttl must be > 0")
        with self._lock:
            self._items[key] = _Entry(value=value, expires_at=monotonic() + ttl_value)

    def invalidate(self, key: Optional[K] = None) -> None:
        with self._lock:
            if key is None:
                self._items.clear()
            else:
                self._items.pop(key, None)

    def __contains__(self, key: K) -> bool:  # type: ignore[override]
        return self.get(key) is not None

