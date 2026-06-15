"""SQLite storage. Synchronous sqlite3 calls are wrapped with asyncio.to_thread
so they don't block the bot's event loop."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT,
    balance       INTEGER NOT NULL DEFAULT 0,
    trial_used    INTEGER NOT NULL DEFAULT 0,
    referred_by   INTEGER,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    vpn_name    TEXT NOT NULL UNIQUE,
    uuid        TEXT,
    plan_id     TEXT,
    expires_at  INTEGER NOT NULL,
    traffic_gb  INTEGER NOT NULL DEFAULT 0,
    devices     INTEGER NOT NULL DEFAULT 0,
    is_trial    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    plan_id     TEXT,
    amount      INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'purchase',
    status      TEXT NOT NULL DEFAULT 'completed',
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(telegram_id);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(telegram_id);

CREATE TABLE IF NOT EXISTS payments (
    uuid        TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL UNIQUE,
    telegram_id INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    credited    INTEGER NOT NULL DEFAULT 0,
    pay_url     TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(telegram_id);
"""


@dataclass
class User:
    telegram_id: int
    username: str | None
    balance: int
    trial_used: bool
    referred_by: int | None
    created_at: int


@dataclass
class Subscription:
    id: int
    telegram_id: int
    vpn_name: str
    uuid: str | None
    plan_id: str | None
    expires_at: int
    traffic_gb: int
    devices: int
    is_trial: bool
    created_at: int


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            await asyncio.to_thread(conn.close)

    async def _run(self, fn):
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        async with self._lock:
            return await asyncio.to_thread(fn)

    # --- users ---

    async def get_or_create_user(
        self, telegram_id: int, username: str | None, referred_by: int | None = None
    ) -> User:
        def _op() -> sqlite3.Row:
            conn = self._conn
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (telegram_id, username, referred_by, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (telegram_id, username, referred_by, int(time.time())),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
                ).fetchone()
            elif username and row["username"] != username:
                conn.execute(
                    "UPDATE users SET username = ? WHERE telegram_id = ?",
                    (username, telegram_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
                ).fetchone()
            return row

        row = await self._run(_op)
        return _row_to_user(row)

    async def get_user(self, telegram_id: int) -> User | None:
        def _op():
            return self._conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()

        row = await self._run(_op)
        return _row_to_user(row) if row else None

    async def adjust_balance(self, telegram_id: int, delta: int) -> int:
        """Add delta (can be negative) to balance, return the new balance."""

        def _op() -> int:
            conn = self._conn
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                (delta, telegram_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return int(row["balance"]) if row else 0

        return await self._run(_op)

    async def try_debit(self, telegram_id: int, amount: int) -> int | None:
        """Atomically debit `amount` only if the balance covers it.

        Returns the new balance on success, or None if funds are insufficient.
        The check-and-debit happens in one statement under the DB lock, so two
        concurrent purchases can never both pass the balance check.
        """

        def _op() -> int | None:
            conn = self._conn
            cur = conn.execute(
                "UPDATE users SET balance = balance - ? "
                "WHERE telegram_id = ? AND balance >= ?",
                (amount, telegram_id, amount),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return None
            conn.commit()
            row = conn.execute(
                "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return int(row["balance"]) if row else 0

        return await self._run(_op)

    async def claim_trial(self, telegram_id: int) -> bool:
        """Atomically mark the trial used. Returns True only for the first caller."""

        def _op() -> bool:
            conn = self._conn
            cur = conn.execute(
                "UPDATE users SET trial_used = 1 "
                "WHERE telegram_id = ? AND trial_used = 0",
                (telegram_id,),
            )
            conn.commit()
            return cur.rowcount > 0

        return await self._run(_op)

    async def mark_trial_used(self, telegram_id: int) -> None:
        def _op():
            self._conn.execute(
                "UPDATE users SET trial_used = 1 WHERE telegram_id = ?", (telegram_id,)
            )
            self._conn.commit()

        await self._run(_op)

    async def reset_trial(self, telegram_id: int) -> None:
        """Release a trial claim (used to roll back when provisioning fails)."""

        def _op():
            self._conn.execute(
                "UPDATE users SET trial_used = 0 WHERE telegram_id = ?", (telegram_id,)
            )
            self._conn.commit()

        await self._run(_op)

    # --- subscriptions ---

    async def add_subscription(self, sub: Subscription) -> int:
        def _op() -> int:
            cur = self._conn.execute(
                "INSERT INTO subscriptions "
                "(telegram_id, vpn_name, uuid, plan_id, expires_at, traffic_gb, devices, is_trial, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sub.telegram_id,
                    sub.vpn_name,
                    sub.uuid,
                    sub.plan_id,
                    sub.expires_at,
                    sub.traffic_gb,
                    sub.devices,
                    int(sub.is_trial),
                    sub.created_at,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

        return await self._run(_op)

    async def update_subscription(
        self, sub_id: int, *, expires_at: int, uuid: str | None = None
    ) -> None:
        def _op():
            if uuid is not None:
                self._conn.execute(
                    "UPDATE subscriptions SET expires_at = ?, uuid = ? WHERE id = ?",
                    (expires_at, uuid, sub_id),
                )
            else:
                self._conn.execute(
                    "UPDATE subscriptions SET expires_at = ? WHERE id = ?",
                    (expires_at, sub_id),
                )
            self._conn.commit()

        await self._run(_op)

    async def get_subscription(self, sub_id: int) -> Subscription | None:
        def _op():
            return self._conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
            ).fetchone()

        row = await self._run(_op)
        return _row_to_sub(row) if row else None

    async def get_subscription_by_name(self, vpn_name: str) -> Subscription | None:
        def _op():
            return self._conn.execute(
                "SELECT * FROM subscriptions WHERE vpn_name = ?", (vpn_name,)
            ).fetchone()

        row = await self._run(_op)
        return _row_to_sub(row) if row else None

    async def list_subscriptions(self, telegram_id: int) -> list[Subscription]:
        def _op():
            return self._conn.execute(
                "SELECT * FROM subscriptions WHERE telegram_id = ? ORDER BY expires_at DESC",
                (telegram_id,),
            ).fetchall()

        rows = await self._run(_op)
        return [_row_to_sub(r) for r in rows]

    async def record_order(
        self, telegram_id: int, plan_id: str | None, amount: int, kind: str = "purchase"
    ) -> None:
        def _op():
            self._conn.execute(
                "INSERT INTO orders (telegram_id, plan_id, amount, kind, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (telegram_id, plan_id, amount, kind, int(time.time())),
            )
            self._conn.commit()

        await self._run(_op)

    # --- payments (2328.io top-ups) ---

    async def create_payment(
        self,
        uuid: str,
        order_id: str,
        telegram_id: int,
        amount: int,
        currency: str,
        pay_url: str | None,
    ) -> None:
        def _op():
            now = int(time.time())
            self._conn.execute(
                "INSERT INTO payments "
                "(uuid, order_id, telegram_id, amount, currency, status, pay_url, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (uuid, order_id, telegram_id, amount, currency, pay_url, now, now),
            )
            self._conn.commit()

        await self._run(_op)

    async def get_payment(self, uuid: str) -> dict | None:
        def _op():
            return self._conn.execute(
                "SELECT * FROM payments WHERE uuid = ?", (uuid,)
            ).fetchone()

        row = await self._run(_op)
        return dict(row) if row else None

    async def credit_payment_if_new(self, uuid: str, status: str) -> dict | None:
        """Mark a payment paid and credit the user's balance exactly once.

        Returns {telegram_id, amount, currency} if this call performed the
        credit, or None if the payment was already credited / not found.
        The whole check-and-credit runs in one transaction under the DB lock,
        so duplicate checks (poller + manual button) can't double-credit.
        """

        def _op() -> dict | None:
            conn = self._conn
            row = conn.execute(
                "SELECT * FROM payments WHERE uuid = ?", (uuid,)
            ).fetchone()
            now = int(time.time())
            if row is None:
                return None
            if row["credited"]:
                conn.execute(
                    "UPDATE payments SET status = ?, updated_at = ? WHERE uuid = ?",
                    (status, now, uuid),
                )
                conn.commit()
                return None
            conn.execute(
                "UPDATE payments SET status = ?, credited = 1, updated_at = ? WHERE uuid = ?",
                (status, now, uuid),
            )
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                (row["amount"], row["telegram_id"]),
            )
            conn.execute(
                "INSERT INTO orders (telegram_id, plan_id, amount, kind, created_at) "
                "VALUES (?, NULL, ?, 'topup', ?)",
                (row["telegram_id"], row["amount"], now),
            )
            conn.commit()
            return {
                "telegram_id": row["telegram_id"],
                "amount": int(row["amount"]),
                "currency": row["currency"],
            }

        return await self._run(_op)

    async def set_payment_status(self, uuid: str, status: str) -> None:
        def _op():
            self._conn.execute(
                "UPDATE payments SET status = ?, updated_at = ? WHERE uuid = ?",
                (status, int(time.time()), uuid),
            )
            self._conn.commit()

        await self._run(_op)

    async def list_pending_payments(self, max_age_seconds: int = 0) -> list[dict]:
        """Uncredited payments still worth polling.

        A payment is pending while it hasn't been credited and isn't in a
        terminal non-paid state. If max_age_seconds is given, rows older than
        that are skipped (they're handled by expire_stale_payments).
        """

        def _op():
            sql = (
                "SELECT * FROM payments WHERE credited = 0 "
                "AND status NOT IN ('cancel', 'expired')"
            )
            params: tuple = ()
            if max_age_seconds > 0:
                sql += " AND created_at >= ?"
                params = (int(time.time()) - max_age_seconds,)
            return self._conn.execute(sql, params).fetchall()

        rows = await self._run(_op)
        return [dict(r) for r in rows]

    async def expire_stale_payments(self, max_age_seconds: int) -> int:
        """Mark old uncredited payments as expired so we stop polling them."""

        def _op() -> int:
            cutoff = int(time.time()) - max_age_seconds
            cur = self._conn.execute(
                "UPDATE payments SET status = 'expired', updated_at = ? "
                "WHERE credited = 0 AND status NOT IN ('cancel', 'expired') "
                "AND created_at < ?",
                (int(time.time()), cutoff),
            )
            self._conn.commit()
            return cur.rowcount

        return await self._run(_op)

    # --- admin stats ---

    async def stats(self) -> dict:
        def _op() -> dict:
            conn = self._conn
            users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            active = conn.execute(
                "SELECT COUNT(*) c FROM subscriptions WHERE expires_at > ?",
                (int(time.time()),),
            ).fetchone()["c"]
            revenue = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) s FROM orders WHERE kind = 'purchase'"
            ).fetchone()["s"]
            return {"users": users, "active_subs": active, "revenue": revenue}

        return await self._run(_op)


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        balance=int(row["balance"]),
        trial_used=bool(row["trial_used"]),
        referred_by=row["referred_by"],
        created_at=row["created_at"],
    )


def _row_to_sub(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"],
        telegram_id=row["telegram_id"],
        vpn_name=row["vpn_name"],
        uuid=row["uuid"],
        plan_id=row["plan_id"],
        expires_at=row["expires_at"],
        traffic_gb=int(row["traffic_gb"]),
        devices=int(row["devices"]),
        is_trial=bool(row["is_trial"]),
        created_at=row["created_at"],
    )
