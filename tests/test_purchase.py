from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import purchase


class FakeDriver:
    def __init__(self) -> None:
        self.quit_called = False
        self.screenshots: list[str] = []
        self.title = ""
        self.current_url = ""
        self.page_source = ""

    def save_screenshot(self, path: str) -> bool:
        Path(path).write_bytes(b"fake-png")
        self.screenshots.append(path)
        return True

    def quit(self) -> None:
        self.quit_called = True


class FakeScrim:
    def __init__(self) -> None:
        self.visible = True

    def is_displayed(self) -> bool:
        return self.visible


class FakeButton:
    text = "关闭"

    def is_displayed(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True


class OverlayDriver:
    def __init__(self) -> None:
        self.scrim = FakeScrim()
        self.click_attempts = 0

    def find_elements(self, by, selector):
        if selector == ".v-overlay__scrim":
            return [self.scrim]
        if by == purchase.By.XPATH and "关闭" in selector:
            return [FakeButton()]
        return []

    def execute_script(self, _script, _element):
        self.click_attempts += 1
        if self.click_attempts == 1:
            raise purchase.WebDriverException("stale overlay node")
        self.scrim.visible = False

    def find_element(self, _by, _selector):
        raise AssertionError("Escape fallback should not be needed")


class PurchaseScreenshotTests(unittest.TestCase):
    def test_base_url_can_change_at_runtime(self) -> None:
        original = purchase.BASE_URL
        try:
            changed = purchase.set_base_url("https://web2.example.com/")
            self.assertEqual(changed, "https://web2.example.com")
            with self.assertRaises(purchase.PurchaseFailure):
                purchase.set_base_url("web2.example.com")
        finally:
            purchase.set_base_url(original)

    def test_plan_route_detection_requires_plan_segment(self) -> None:
        driver = FakeDriver()
        driver.current_url = "https://web1.52pokemon.cc/plan"
        self.assertTrue(purchase.is_plan_route(driver))

        driver.current_url = "https://web1.52pokemon.cc/plan/12"
        self.assertTrue(purchase.is_plan_route(driver))

        driver.current_url = "https://web1.52pokemon.cc/dashboard?next=/plan"
        self.assertFalse(purchase.is_plan_route(driver))

    def test_slider_verification_is_reported_in_headless_mode(self) -> None:
        driver = FakeDriver()
        driver.page_source = '<script src="/_guard/html.js?js=slider_html"></script>'
        with patch.dict(os.environ, {"POKEMON_HEADLESS": "1"}):
            with self.assertRaises(purchase.PurchaseFailure) as raised:
                purchase.wait_for_security_verification(driver)
        self.assertEqual(raised.exception.code, "security_verification_required")

    def test_overlay_click_retries_stale_node(self) -> None:
        driver = OverlayDriver()
        with patch.object(purchase, "STEP_DELAY_SECONDS", 0):
            purchase.dismiss_blocking_overlays(driver)
        self.assertEqual(driver.click_attempts, 2)

    def test_login_retries_after_temporary_timeout(self) -> None:
        driver = FakeDriver()
        driver.current_url = "https://web1.52pokemon.cc/login"
        calls = {"submit": 0}

        def submit(*_args, **_kwargs):
            calls["submit"] += 1

        with patch.dict(
            os.environ,
            {
                "POKEMON_EMAIL": "probe@example.test",
                "POKEMON_PASSWORD": "probe-password",
            },
        ):
            with patch.object(purchase, "open_page"):
                with patch.object(purchase, "submit_login_form", side_effect=submit):
                    with patch.object(
                        purchase,
                        "wait_for_login_result",
                        side_effect=[
                            purchase.TimeoutException("temporary timeout"),
                            None,
                        ],
                    ):
                        with patch.object(purchase, "verify_login"):
                            with patch.object(purchase, "pause"):
                                with patch.object(purchase, "LOGIN_ATTEMPTS", 2):
                                    purchase.login_if_needed(driver, non_interactive=True)
        self.assertEqual(calls["submit"], 2)

    def test_failure_screenshot_is_saved_before_quit(self) -> None:
        driver = FakeDriver()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(purchase, "PROJECT_DIR", Path(directory)):
                with patch.object(purchase, "setup_driver", return_value=driver):
                    with patch.object(
                        purchase,
                        "login_if_needed",
                        side_effect=RuntimeError("network down"),
                    ):
                        with self.assertRaises(RuntimeError):
                            purchase.run(redeem_code="test-code", non_interactive=True)
            files = list((Path(directory) / "screenshots").glob("*.png"))
        self.assertTrue(driver.quit_called)
        self.assertEqual(len(files), 1)
        self.assertIn("failure", files[0].name)

    def test_success_screenshot_is_saved(self) -> None:
        driver = FakeDriver()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(purchase, "PROJECT_DIR", Path(directory)):
                with patch.object(purchase, "setup_driver", return_value=driver):
                    with patch.object(purchase, "login_if_needed"):
                        with patch.object(purchase, "prepare_order", return_value="order-1"):
                            order_id = purchase.run(
                                redeem_code="test-code",
                                non_interactive=True,
                            )
            files = list((Path(directory) / "screenshots").glob("*.png"))
        self.assertEqual(order_id, "order-1")
        self.assertTrue(driver.quit_called)
        self.assertEqual(len(files), 1)
        self.assertIn("success", files[0].name)


if __name__ == "__main__":
    unittest.main()
