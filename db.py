from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg


def clean_dsn(dsn: str) -> str:
    """Neon adds a channel_binding option that asyncpg rejects. Strip it so
    the connection string works as copied from the Neon dashboard."""
    parts = urlsplit(dsn.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       BIGINT PRIMARY KEY,
    balance       BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
    daily_streak  INT NOT NULL DEFAULT 0,
    last_daily    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cooldowns (
    user_id     BIGINT NOT NULL,
    key         TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE INDEX IF NOT EXISTS users_balance_idx ON users (balance DESC);
"""


class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self, dsn: str):
        self.pool = await asyncpg.create_pool(
            clean_dsn(dsn),
            min_size=1,
            max_size=10,
            timeout=30,  # allows for Neon waking up from auto-suspend
            max_inactive_connection_lifetime=120,  # drop idle conns before Neon suspends
            statement_cache_size=0,  # safe with Neon's pooled (-pooler) endpoint too
        )
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self):
        if self.pool:
            await self.pool.close()

    # ---------- users / balance ----------

    async def ensure_user(self, user_id: int):
        await self.pool.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
        )

    async def get_balance(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        return await self.pool.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id)

    async def change_balance(self, user_id: int, delta: int) -> int | None:
        """Atomically add delta (can be negative). Returns the new balance,
        or None if it would go below zero. Use this for all bets/payouts."""
        await self.ensure_user(user_id)
        return await self.pool.fetchval(
            """
            UPDATE users SET balance = balance + $2
            WHERE user_id = $1 AND balance + $2 >= 0
            RETURNING balance
            """,
            user_id,
            delta,
        )

    async def transfer(self, from_id: int, to_id: int, amount: int) -> bool:
        await self.ensure_user(from_id)
        await self.ensure_user(to_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # lock rows in a fixed order to avoid deadlocks
                await conn.fetch(
                    "SELECT user_id FROM users WHERE user_id = ANY($1::bigint[]) ORDER BY user_id FOR UPDATE",
                    [from_id, to_id],
                )
                ok = await conn.fetchval(
                    """
                    UPDATE users SET balance = balance - $2
                    WHERE user_id = $1 AND balance >= $2 RETURNING 1
                    """,
                    from_id,
                    amount,
                )
                if not ok:
                    return False
                await conn.execute(
                    "UPDATE users SET balance = balance + $2 WHERE user_id = $1", to_id, amount
                )
                return True

    async def top_balances(self, limit: int = 10):
        return await self.pool.fetch(
            "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT $1",
            limit,
        )

    async def rank_of(self, user_id: int) -> int:
        return await self.pool.fetchval(
            """
            SELECT 1 + COUNT(*) FROM users
            WHERE balance > (SELECT balance FROM users WHERE user_id = $1)
            """,
            user_id,
        )

    # ---------- cooldowns ----------

    async def claim_cooldown(self, user_id: int, key: str, seconds: int) -> float:
        """Try to start a cooldown. Returns 0 if claimed (command may run),
        otherwise the number of seconds remaining. Atomic, so spamming a
        command can't double-claim."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO cooldowns (user_id, key, expires_at)
            VALUES ($1, $2, now() + make_interval(secs => $3))
            ON CONFLICT (user_id, key) DO UPDATE SET expires_at = EXCLUDED.expires_at
            WHERE cooldowns.expires_at <= now()
            RETURNING 1
            """,
            user_id,
            key,
            float(seconds),
        )
        if row:
            return 0.0
        return await self.pool.fetchval(
            "SELECT EXTRACT(EPOCH FROM (expires_at - now())) FROM cooldowns WHERE user_id = $1 AND key = $2",
            user_id,
            key,
        )

    # ---------- daily streak ----------

    async def bump_daily_streak(self, user_id: int) -> int:
        """Streak continues if last claim was within 48h, otherwise resets to 1."""
        await self.ensure_user(user_id)
        return await self.pool.fetchval(
            """
            UPDATE users SET
                daily_streak = CASE
                    WHEN last_daily IS NOT NULL AND last_daily > now() - interval '48 hours'
                    THEN daily_streak + 1 ELSE 1 END,
                last_daily = now()
            WHERE user_id = $1
            RETURNING daily_streak
            """,
            user_id,
        )
