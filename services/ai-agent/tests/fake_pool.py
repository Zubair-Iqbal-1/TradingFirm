"""A fake asyncpg pool shared by the Part 4.4 tests. Answers by SQL shape,
records every statement, opens nothing."""

from contextlib import asynccontextmanager


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, *args):
        return self.pool._run("fetchrow", sql, args)

    async def fetch(self, sql, *args):
        return self.pool._run("fetch", sql, args)

    async def execute(self, sql, *args):
        return self.pool._run("execute", sql, args)

    @asynccontextmanager
    async def transaction(self):
        self.pool.tx_open += 1
        mark = len(self.pool.calls)
        try:
            yield
        except BaseException:
            # A rolled-back transaction leaves nothing behind.
            self.pool.rolled_back.extend(self.pool.calls[mark:])
            del self.pool.calls[mark:]
            raise


class FakePool:
    """`answers` maps a substring of the SQL to a value, or to a callable
    taking the args. `raise_on` is a substring of the SQL that blows up."""

    def __init__(self, answers=None, raise_on=None, error=None):
        self.answers = answers or {}
        self.raise_on = raise_on
        self.error = error
        self.calls: list[tuple[str, str, tuple]] = []
        self.rolled_back: list = []
        self.tx_open = 0

    def _run(self, kind, sql, args):
        self.calls.append((kind, sql, args))
        if self.raise_on and self.raise_on in sql:
            import asyncpg
            raise self.error or asyncpg.PostgresError(f"fake failure on {self.raise_on}")
        for needle, value in self.answers.items():
            if needle in sql:
                return value(args) if callable(value) else value
        return [] if kind == "fetch" else None

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)

    def statements(self, needle):
        return [c for c in self.calls if needle in c[1]]
