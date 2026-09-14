"""
In-process fake Redis for risk-shield tests (Part 3.1).

The Part 1.7 pattern from data-engine, copied: enough of the async client
surface for cache.py to run its real code path — no socket, no server.
Failure modes are opt-in flags so a test can prove the fail-open branches
without patching cache.py itself.
"""


class FakeRedis:
    def __init__(self, *, fail_get=False, fail_set=False, fail_ttl=False, fail_publish=False,
                 fail_delete=False):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.fail_ttl = fail_ttl
        self.fail_publish = fail_publish
        self.fail_delete = fail_delete    # Part 3.4c: the situation route's DELETE
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str, int]] = []
        self.published: list[tuple[str, str]] = []    # (channel, message), Part 3.4

    async def publish(self, channel, message):
        """Records the message; returns 0 receivers (nobody subscribes)."""
        if self.fail_publish:
            raise RuntimeError("boom: redis publish")
        self.published.append((channel, message))
        return 0

    async def get(self, key):
        self.get_calls.append(key)
        if self.fail_get:
            raise RuntimeError("boom: redis get")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))
        if self.fail_set:
            raise RuntimeError("boom: redis set")
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def ttl(self, key):
        """Redis semantics: -2 absent, -1 no expiry, else the stored TTL
        (the fake clock never advances; tests set `ttls` directly)."""
        if self.fail_ttl:
            raise RuntimeError("boom: redis ttl")
        if key not in self.store:
            return -2
        ex = self.ttls.get(key)
        return -1 if ex is None else ex

    async def delete(self, *keys):
        """Redis semantics: the number of keys that existed (Part 3.4c)."""
        if self.fail_delete:
            raise RuntimeError("boom: redis delete")
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                self.ttls.pop(key, None)
                removed += 1
        return removed

    async def ping(self):
        return True

    async def close(self):
        return None
