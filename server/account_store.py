"""Read and mutate the non-secret account metadata stored in pool.db."""

from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable


VALID_STATUSES = {"available", "cooling", "needs_attention"}


class StoreError(ValueError):
    """The account pool could not be read or updated."""


def _write_temp(data: bytes, path: Path) -> None:
    path.write_bytes(data)


def read_accounts(data: bytes) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="pokemon-pool-") as directory:
        path = Path(directory) / "pool.db"
        _write_temp(data, path)
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT account_id, label, status, last_used_at,
                       next_available_at, lease_until, last_result,
                       last_order_id, updated_at
                  FROM accounts
                 ORDER BY account_id ASC
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise StoreError("pool.db 不是有效的账号池数据库") from exc
        finally:
            connection.close()
    return [dict(row) for row in rows]


def _mutate(
    data: bytes,
    operation: Callable[[sqlite3.Connection], None],
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="pokemon-pool-") as directory:
        path = Path(directory) / "pool.db"
        _write_temp(data, path)
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("BEGIN IMMEDIATE")
            operation(connection)
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
        return path.read_bytes()


def _now(value: dt.datetime | None = None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return _now(value).isoformat(timespec="seconds")


def add_account(
    data: bytes,
    account_id: str,
    label: str,
    now: dt.datetime | None = None,
) -> bytes:
    account_id = account_id.strip()
    label = label.strip() or account_id
    if not account_id or len(account_id) > 80:
        raise StoreError("账号 ID 不能为空且不能超过 80 个字符")
    current = _iso(now)

    def operation(connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT 1 FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE accounts SET label = ?, updated_at = ? WHERE account_id = ?",
                (label, current, account_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO accounts(account_id, label, status, updated_at)
                VALUES (?, ?, 'available', ?)
                """,
                (account_id, label, current),
            )

    return _mutate(data, operation)


def update_account(
    data: bytes,
    account_id: str,
    status: str,
    next_available_at: dt.datetime | None,
    label: str | None = None,
    now: dt.datetime | None = None,
) -> bytes:
    if status not in VALID_STATUSES:
        raise StoreError("账号状态不合法")
    current_dt = _now(now)
    current = _iso(current_dt)
    if status == "cooling":
        if next_available_at is None:
            raise StoreError("冷却状态必须设置重置日期")
        if next_available_at <= current_dt:
            status = "available"
            next_available_at = None
    else:
        next_available_at = None

    def operation(connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT 1 FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if not existing:
            raise StoreError("账号不存在")
        fields = [
            "status = ?",
            "next_available_at = ?",
            "lease_until = NULL",
            "last_result = 'manual_update'",
            "updated_at = ?",
        ]
        values: list[Any] = [status, _iso(next_available_at), current]
        if label is not None and label.strip():
            fields.append("label = ?")
            values.append(label.strip())
        values.append(account_id)
        connection.execute(
            f"UPDATE accounts SET {', '.join(fields)} WHERE account_id = ?",
            values,
        )

    return _mutate(data, operation)
