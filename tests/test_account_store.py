from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pool
from server import account_store


class AccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "pool.db"
        pool.initialize(self.db)
        self.data = self.db.read_bytes()
        self.now = dt.datetime(2026, 10, 19, 2, 0, tzinfo=dt.timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_add_and_update_account(self) -> None:
        changed = account_store.add_account(self.data, "acct-01", "账号一", self.now)
        accounts = account_store.read_accounts(changed)
        self.assertEqual(accounts[0]["account_id"], "acct-01")
        self.assertEqual(accounts[0]["status"], "available")

        changed = account_store.update_account(
            changed,
            "acct-01",
            "cooling",
            self.now + dt.timedelta(days=30),
            label="账号一改名",
            now=self.now,
        )
        account = account_store.read_accounts(changed)[0]
        self.assertEqual(account["label"], "账号一改名")
        self.assertEqual(account["status"], "cooling")
        self.assertIn("2026-11-18", account["next_available_at"])

    def test_past_reset_date_normalizes_to_available(self) -> None:
        changed = account_store.add_account(self.data, "acct-01", "", self.now)
        changed = account_store.update_account(
            changed,
            "acct-01",
            "cooling",
            self.now - dt.timedelta(days=1),
            now=self.now,
        )
        account = account_store.read_accounts(changed)[0]
        self.assertEqual(account["status"], "available")
        self.assertIsNone(account["next_available_at"])

    def test_unknown_account_is_rejected(self) -> None:
        with self.assertRaises(account_store.StoreError):
            account_store.update_account(
                self.data,
                "missing",
                "available",
                None,
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
