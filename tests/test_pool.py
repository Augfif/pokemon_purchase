from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pool


SECRET_ACCOUNTS = [
    {"id": "acct-01", "label": "账号一", "email": "a@example.test", "password": "p1"},
    {"id": "acct-02", "label": "账号二", "email": "b@example.test", "password": "p2"},
]


class PoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "pool.db"
        pool.initialize(self.db)
        self.connection = pool.connect(self.db)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def accounts(self) -> dict[str, dict]:
        return {item["id"]: item for item in SECRET_ACCOUNTS}

    def test_selects_oldest_account_and_leases_it(self) -> None:
        one = SECRET_ACCOUNTS[:1]
        pool.seed_accounts(self.connection, one)
        selected = pool.select_account(self.connection, {"acct-01": one[0]})
        self.assertIsNotNone(selected)
        self.assertEqual(selected["account_id"], "acct-01")
        row = self.connection.execute(
            "SELECT status, lease_until FROM accounts WHERE account_id = 'acct-01'"
        ).fetchone()
        self.assertEqual(row["status"], "in_use")
        self.assertIsNotNone(row["lease_until"])
        self.assertIsNone(pool.select_account(self.connection, {"acct-01": one[0]}))

    def test_expired_lease_becomes_available(self) -> None:
        now = pool.utc_now()
        pool.seed_accounts(self.connection, SECRET_ACCOUNTS, now)
        pool.select_account(self.connection, self.accounts(), now=now, lease_minutes=1)
        selected = pool.select_account(
            self.connection,
            self.accounts(),
            now=now + dt.timedelta(minutes=2),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["account_id"], "acct-01")

    def test_success_creates_thirty_day_cooldown(self) -> None:
        now = pool.utc_now()
        one = SECRET_ACCOUNTS[:1]
        accounts = {"acct-01": one[0]}
        pool.seed_accounts(self.connection, one, now)
        selected = pool.select_account(self.connection, accounts, now=now)
        self.assertIsNotNone(selected)
        self.assertTrue(pool.start_run(self.connection, "r1", selected["account_id"], now))
        next_available = pool.mark_success(
            self.connection, selected["account_id"], "r1", "order-1", now=now
        )
        self.assertEqual(next_available, now + dt.timedelta(days=30))
        self.assertIsNone(
            pool.select_account(
                self.connection,
                accounts,
                now=now + dt.timedelta(days=29),
            )
        )
        self.assertEqual(
            pool.select_account(
                self.connection,
                accounts,
                now=now + dt.timedelta(days=31),
            )["account_id"],
            "acct-01",
        )

    def test_retryable_failure_releases_account(self) -> None:
        now = pool.utc_now()
        pool.seed_accounts(self.connection, SECRET_ACCOUNTS, now)
        selected = pool.select_account(self.connection, self.accounts(), now=now)
        self.assertTrue(pool.start_run(self.connection, "r2", selected["account_id"], now))
        pool.mark_failure(
            self.connection,
            selected["account_id"],
            "r2",
            "site_unavailable",
            retryable=True,
            now=now,
        )
        row = self.connection.execute(
            "SELECT status FROM accounts WHERE account_id = ?",
            (selected["account_id"],),
        ).fetchone()
        self.assertEqual(row["status"], "available")

    def test_non_retryable_failure_needs_attention(self) -> None:
        now = pool.utc_now()
        pool.seed_accounts(self.connection, SECRET_ACCOUNTS, now)
        selected = pool.select_account(self.connection, self.accounts(), now=now)
        self.assertTrue(pool.start_run(self.connection, "r3", selected["account_id"], now))
        pool.mark_failure(
            self.connection,
            selected["account_id"],
            "r3",
            "authentication_or_captcha",
            retryable=False,
            now=now,
        )
        row = self.connection.execute(
            "SELECT status FROM accounts WHERE account_id = ?",
            (selected["account_id"],),
        ).fetchone()
        self.assertEqual(row["status"], "needs_attention")

    def test_duplicate_request_is_rejected(self) -> None:
        pool.seed_accounts(self.connection, SECRET_ACCOUNTS)
        selected = pool.select_account(self.connection, self.accounts())
        self.assertTrue(pool.start_run(self.connection, "same", selected["account_id"]))
        self.assertFalse(pool.start_run(self.connection, "same", selected["account_id"]))

    def test_database_has_no_credential_columns(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(accounts)").fetchall()
        }
        self.assertNotIn("email", columns)
        self.assertNotIn("password", columns)
        self.assertNotIn("cookie", columns)


if __name__ == "__main__":
    unittest.main()
