"""Persistent account-pool state for the GitHub Actions purchase job.

The database intentionally contains no credentials. Account credentials live in
the ``POKEMON_ACCOUNTS_JSON`` GitHub secret and are matched by ``account_id``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable


UTC = dt.timezone.utc
DEFAULT_COOLDOWN_DAYS = 30
DEFAULT_LEASE_MINUTES = 20
SCHEMA_VERSION = "1"
VALID_STATUSES = {"available", "cooling", "in_use", "needs_attention"}


class PoolError(Exception):
    """A pool operation failed with a machine-readable reason."""

    def __init__(self, message: str, code: str = "pool_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def utc_now(value: dt.datetime | None = None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return utc_now(value).isoformat(timespec="seconds")


def from_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utc_now(parsed)


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize(db_path: str | Path) -> None:
    connection = connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK (status IN ('available', 'cooling', 'in_use', 'needs_attention')),
                last_used_at TEXT,
                next_available_at TEXT,
                lease_until TEXT,
                last_result TEXT,
                last_order_id TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                request_id TEXT PRIMARY KEY,
                account_id TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                result TEXT NOT NULL,
                order_id TEXT,
                error_code TEXT
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
    finally:
        connection.close()


def _public_accounts(accounts: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, account in enumerate(accounts, start=1):
        if not isinstance(account, dict):
            raise PoolError(f"账号列表第 {index} 项格式错误", "invalid_accounts")
        account_id = str(account.get("id", "")).strip()
        if not account_id:
            raise PoolError(f"账号列表第 {index} 项缺少 id", "invalid_accounts")
        if account_id in seen:
            raise PoolError("账号列表存在重复 id", "invalid_accounts")
        seen.add(account_id)
        result.append(
            {
                "id": account_id,
                "label": str(account.get("label") or account_id).strip(),
            }
        )
    return result


def seed_accounts(
    connection: sqlite3.Connection,
    accounts: Iterable[dict[str, Any]],
    now: dt.datetime | None = None,
) -> int:
    """Add newly configured account IDs without deleting or resetting old rows."""

    current = to_iso(now or utc_now())
    added = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for account in _public_accounts(accounts):
            existing = connection.execute(
                "SELECT account_id FROM accounts WHERE account_id = ?",
                (account["id"],),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE accounts SET label = ?, updated_at = ? WHERE account_id = ?",
                    (account["label"], current, account["id"]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO accounts
                        (account_id, label, status, updated_at)
                    VALUES (?, ?, 'available', ?)
                    """,
                    (account["id"], account["label"], current),
                )
                added += 1
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return added


def _expire_leases_and_cooldowns(
    connection: sqlite3.Connection,
    now: dt.datetime,
) -> None:
    current = to_iso(now)
    connection.execute(
        """
        UPDATE accounts
           SET status = 'available', lease_until = NULL, updated_at = ?
         WHERE status = 'in_use'
           AND lease_until IS NOT NULL
           AND lease_until <= ?
        """,
        (current, current),
    )
    connection.execute(
        """
        UPDATE accounts
           SET status = 'available', lease_until = NULL, updated_at = ?
         WHERE status = 'cooling'
           AND next_available_at IS NOT NULL
           AND next_available_at <= ?
        """,
        (current, current),
    )


def select_account(
    connection: sqlite3.Connection,
    accounts_by_id: dict[str, dict[str, Any]],
    now: dt.datetime | None = None,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
) -> dict[str, Any] | None:
    """Atomically lease the oldest eligible account."""

    current_dt = utc_now(now)
    current = to_iso(current_dt)
    lease_until = to_iso(current_dt + dt.timedelta(minutes=lease_minutes))
    connection.execute("BEGIN IMMEDIATE")
    try:
        _expire_leases_and_cooldowns(connection, current_dt)
        rows = connection.execute(
            """
            SELECT account_id, label, status, last_used_at, next_available_at
              FROM accounts
             WHERE status = 'available'
             ORDER BY COALESCE(last_used_at, '') ASC, account_id ASC
            """
        ).fetchall()
        for row in rows:
            account_id = row["account_id"]
            if account_id not in accounts_by_id:
                # A DB row without a matching secret must not consume a lease.
                continue
            changed = connection.execute(
                """
                UPDATE accounts
                   SET status = 'in_use',
                       lease_until = ?,
                       last_result = 'dispatching',
                       updated_at = ?
                 WHERE account_id = ?
                   AND status = 'available'
                """,
                (lease_until, current, account_id),
            )
            if changed.rowcount == 1:
                connection.execute("COMMIT")
                result = dict(row)
                result["lease_until"] = lease_until
                return result
        connection.execute("COMMIT")
        return None
    except Exception:
        connection.execute("ROLLBACK")
        raise


def start_run(
    connection: sqlite3.Connection,
    request_id: str,
    account_id: str,
    now: dt.datetime | None = None,
) -> bool:
    current = to_iso(now or utc_now())
    try:
        connection.execute(
            """
            INSERT INTO runs(request_id, account_id, started_at, result)
            VALUES (?, ?, ?, 'dispatching')
            """,
            (request_id, account_id, current),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _finish_run(
    connection: sqlite3.Connection,
    request_id: str,
    result: str,
    order_id: str | None,
    error_code: str | None,
    now: dt.datetime,
) -> None:
    connection.execute(
        """
        UPDATE runs
           SET finished_at = ?, result = ?, order_id = ?, error_code = ?
         WHERE request_id = ?
        """,
        (to_iso(now), result, order_id, error_code, request_id),
    )


def mark_success(
    connection: sqlite3.Connection,
    account_id: str,
    request_id: str,
    order_id: str | None,
    now: dt.datetime | None = None,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
) -> dt.datetime:
    current_dt = utc_now(now)
    current = to_iso(current_dt)
    next_available = current_dt + dt.timedelta(days=cooldown_days)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            UPDATE accounts
               SET status = 'cooling',
                   last_used_at = ?,
                   next_available_at = ?,
                   lease_until = NULL,
                   last_result = 'success',
                   last_order_id = ?,
                   updated_at = ?
             WHERE account_id = ?
            """,
            (current, to_iso(next_available), order_id, current, account_id),
        )
        _finish_run(connection, request_id, "success", order_id, None, current_dt)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return next_available


def mark_dry_run(
    connection: sqlite3.Connection,
    account_id: str,
    request_id: str,
    now: dt.datetime | None = None,
) -> None:
    current_dt = utc_now(now)
    current = to_iso(current_dt)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            UPDATE accounts
               SET status = 'available',
                   lease_until = NULL,
                   last_result = 'dry_run',
                   updated_at = ?
             WHERE account_id = ?
            """,
            (current, account_id),
        )
        _finish_run(connection, request_id, "dry_run", None, None, current_dt)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def mark_failure(
    connection: sqlite3.Connection,
    account_id: str,
    request_id: str,
    error_code: str,
    retryable: bool,
    now: dt.datetime | None = None,
) -> None:
    current_dt = utc_now(now)
    current = to_iso(current_dt)
    status = "available" if retryable else "needs_attention"
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            UPDATE accounts
               SET status = ?,
                   lease_until = NULL,
                   last_result = ?,
                   updated_at = ?
             WHERE account_id = ?
            """,
            (status, error_code, current, account_id),
        )
        _finish_run(connection, request_id, "failed", None, error_code, current_dt)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def stats(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS count FROM accounts GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    return {
        "total": sum(counts.values()),
        "available": counts.get("available", 0),
        "cooling": counts.get("cooling", 0),
        "in_use": counts.get("in_use", 0),
        "needs_attention": counts.get("needs_attention", 0),
    }


def parse_accounts_json(raw: str) -> list[dict[str, Any]]:
    if not raw or not raw.strip():
        raise PoolError("POKEMON_ACCOUNTS_JSON 未配置", "missing_accounts")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PoolError("POKEMON_ACCOUNTS_JSON 不是有效 JSON", "invalid_accounts") from exc
    if not isinstance(parsed, list):
        raise PoolError("POKEMON_ACCOUNTS_JSON 必须是数组", "invalid_accounts")
    for index, account in enumerate(parsed, start=1):
        if not isinstance(account, dict):
            raise PoolError(f"账号列表第 {index} 项格式错误", "invalid_accounts")
        for field in ("id", "email", "password"):
            if not str(account.get(field, "")).strip():
                raise PoolError(
                    f"账号列表第 {index} 项缺少 {field}", "invalid_accounts"
                )
    _public_accounts(parsed)
    return parsed


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Manage the 52Pokemon account pool")
    parser.add_argument("--db", default="pool.db")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    initialize(args.db)
    if args.stats:
        connection = connect(args.db)
        try:
            print(json.dumps(stats(connection), ensure_ascii=False, indent=2))
        finally:
            connection.close()
    elif args.init:
        print(f"Initialized {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
