from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import purchase


class FakeDriver:
    def __init__(self) -> None:
        self.quit_called = False
        self.screenshots: list[str] = []

    def save_screenshot(self, path: str) -> bool:
        Path(path).write_bytes(b"fake-png")
        self.screenshots.append(path)
        return True

    def quit(self) -> None:
        self.quit_called = True


class PurchaseScreenshotTests(unittest.TestCase):
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
