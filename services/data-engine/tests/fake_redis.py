"""
Minimal in-process async Redis stand-in for endpoint tests.

Not a mock — a tiny real implementation of the handful of commands
cache.py uses (get / set / ttl / delete), so the real cache code path
executes in tests. `fail_on` names commands that raise, to drive the
"Redis down mid-request" branches.
"""

import time as _time


class FakeRedis:
    def __init__(self, fail_on: set[str] | None = None):
        self._store: dict[str, tuple[str, float | None]] = {}
        self.fail_on = set(fail_on or ())

    def _check(self, command: str):
        if command in self.fail_on:
            raise ConnectionError(f"fake redis: {command} unavailable")

    async def set(self, key, value, ex=None, nx=False):
        """`nx=True`: only when the key is absent (or expired); True if set,
        None if not — redis-py's answers."""
        self._check("set")
        if nx:
            entry = self._store.get(key)
            if entry is not None and (entry[1] is None or _time.time() <= entry[1]):
                return None
        expires_at = _time.time() + ex if ex else None
        self._store[key] = (value, expires_at)
        return True

    async def get(self, key):
        self._check("get")
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and _time.time() > expires_at:
            del self._store[key]
            return None
        return value

    async def ttl(self, key):
        self._check("ttl")
        entry = self._store.get(key)
        if entry is None:
            return -2
        _value, expires_at = entry
        if expires_at is None:
            return -1
        remaining = expires_at - _time.time()
        return int(remaining) if remaining > 0 else -2

    async def delete(self, *keys):
        self._check("delete")
        removed = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                removed += 1
        return removed

    def keys(self) -> list[str]:
        """Test helper (not a Redis command): live keys."""
        return list(self._store)
