from __future__ import annotations

import datetime as dt
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        os.environ.update(
            {
                "URL_PREFIX": "",
                "APP_DB_PATH": str(root / "app.sqlite3"),
                "APP_LOG_PATH": str(root / "app.log"),
                "BASE_URL": "http://testserver",
                "TRIGGER_SLUG": "test-slug",
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD": "admin-password",
                "COUPON_API_TOKEN": "coupon-token",
                "GITHUB_TRIGGER_TOKEN": "",
                "APP_SECRET_KEY": "test-secret",
            }
        )
        if "server.app" in sys.modules:
            self.app_module = importlib.reload(sys.modules["server.app"])
        else:
            self.app_module = importlib.import_module("server.app")
        self.app_module.initialize_database()
        self.app = self.app_module.app
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        for handler in self.app_module.logger.handlers:
            handler.close()
        self.temp_dir.cleanup()

    def login(self) -> None:
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            session["csrf"] = "login-csrf"
        response = self.client.post(
            "/admin/login",
            data={
                "csrf": "login-csrf",
                "username": "admin",
                "password": "admin-password",
            },
        )
        self.assertEqual(response.status_code, 302)

    def set_coupon(self, until: dt.datetime | None = None) -> None:
        self.login()
        with self.client.session_transaction() as session:
            session["csrf"] = "coupon-csrf"
        until = until or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2))
        response = self.client.post(
            "/admin/coupon",
            data={
                "csrf": "coupon-csrf",
                "code": "TEST-CODE",
                "valid_from": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
                "valid_until": until.strftime("%Y-%m-%dT%H:%M"),
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_health_and_coupon_auth(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/coupon").status_code, 401)
        self.set_coupon()
        response = self.client.get(
            "/api/coupon", headers={"Authorization": "Bearer coupon-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["code"], "TEST-CODE")

    def test_expired_coupon_is_rejected(self) -> None:
        self.set_coupon(
            until=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        )
        response = self.client.get(
            "/api/coupon", headers={"Authorization": "Bearer coupon-token"}
        )
        self.assertEqual(response.status_code, 409)

    def test_trigger_requires_slug_and_csrf(self) -> None:
        self.set_coupon()
        self.assertEqual(self.client.get("/r/wrong/").status_code, 404)
        page = self.client.get("/r/test-slug/")
        self.assertEqual(page.status_code, 200)
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        response = self.client.post(
            "/api/trigger", headers={"X-CSRF-Token": "wrong"}, json={}
        )
        self.assertEqual(response.status_code, 403)
        with patch.object(
            self.app_module, "dispatch_github", return_value="https://example.test/run/1"
        ):
            response = self.client.post(
                "/api/trigger", headers={"X-CSRF-Token": csrf}, json={}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["run_url"], "https://example.test/run/1")

    def test_missing_github_token_returns_configuration_error(self) -> None:
        self.set_coupon()
        self.client.get("/r/test-slug/")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        response = self.client.post(
            "/api/trigger", headers={"X-CSRF-Token": csrf}, json={}
        )
        self.assertEqual(response.status_code, 503)

    def test_accounts_page_lists_pool(self) -> None:
        self.login()
        with patch.object(
            self.app_module.pool_admin,
            "list_accounts",
            return_value=[
                {
                    "account_id": "acct-01",
                    "label": "账号一",
                    "status": "available",
                    "last_used_at": None,
                    "next_available_at": None,
                    "lease_until": None,
                    "last_result": None,
                    "last_order_id": None,
                    "updated_at": "2026-10-19T02:00:00+00:00",
                }
            ],
        ):
            response = self.client.get("/admin/accounts/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("acct-01", response.get_data(as_text=True))

    def test_accounts_update_uses_manual_reset(self) -> None:
        self.login()
        with self.client.session_transaction() as session:
            session["csrf"] = "account-csrf"
        with patch.object(
            self.app_module.pool_admin, "update_account", return_value={}
        ) as update:
            response = self.client.post(
                "/admin/accounts/acct-01/update",
                data={
                    "csrf": "account-csrf",
                    "label": "账号一",
                    "status": "cooling",
                    "next_available_at": "2026-11-19T08:00",
                },
            )
        self.assertEqual(response.status_code, 302)
        update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
