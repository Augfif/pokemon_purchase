"""One-shot GitHub Actions runner for the 52Pokemon account pool."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pool
import purchase


def _clean_request_id(value: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "", value or "")
    return cleaned[:80] or f"manual-{int(time.time())}"


def _redact(text: str, values: list[str]) -> str:
    result = text
    for value in values:
        if value and len(value) >= 3:
            result = result.replace(value, "[redacted]")
    return result


def _fetch_coupon(url: str, token: str) -> dict[str, Any]:
    if not url or not token:
        raise purchase.PurchaseFailure(
            "兑换码接口未配置", code="coupon_config_missing", retryable=False
        )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "pokemon-purchase-runner/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise purchase.PurchaseFailure(
            f"兑换码接口返回 HTTP {exc.code}", code="coupon_http_error", retryable=True
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise purchase.PurchaseFailure(
            "兑换码接口暂时不可用", code="coupon_unavailable", retryable=True
        ) from exc
    code = str(payload.get("code", "")).strip()
    if not code:
        raise purchase.PurchaseFailure(
            "兑换码接口没有返回兑换码", code="coupon_empty", retryable=False
        )
    return payload


def _write_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _classify_failure(exc: BaseException, values: list[str]) -> tuple[str, bool]:
    if isinstance(exc, purchase.PurchaseFailure):
        return exc.code, exc.retryable
    message = _redact(str(exc), values)
    lowered = message.lower()
    if any(marker in lowered for marker in ("密码", "验证码", "登录校验", "登录失败")):
        return "authentication_or_captcha", False
    if any(
        marker in lowered
        for marker in ("超时", "timeout", "连接关闭", "站点暂时", "网络", "502")
    ):
        return "site_unavailable", True
    return "purchase_failed", False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one 52Pokemon purchase")
    parser.add_argument("--db", default=os.environ.get("POKEMON_POOL_DB", "pool.db"))
    parser.add_argument(
        "--request-id",
        default=os.environ.get("POKEMON_REQUEST_ID", ""),
        help="Request ID created by the server trigger page",
    )
    parser.add_argument("--coupon-url", default=os.environ.get("POKEMON_COUPON_URL", ""))
    parser.add_argument(
        "--coupon-token", default=os.environ.get("POKEMON_COUPON_TOKEN", "")
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--step-delay", type=float, default=None)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("POKEMON_BASE_URL", ""),
        help="购买网站基础地址；服务器配置或 Actions 输入会覆盖默认值",
    )
    parser.add_argument("--result-file", default=os.environ.get("POKEMON_RESULT_FILE", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    request_id = _clean_request_id(args.request_id)
    now = pool.utc_now()
    account: dict[str, Any] | None = None
    coupon: dict[str, Any] | None = None
    secrets_to_redact: list[str] = []
    connection = None

    try:
        coupon = _fetch_coupon(args.coupon_url, args.coupon_token)
        redeem_code = str(coupon["code"]).strip()
        secrets_to_redact.append(redeem_code)
        base_url = (
            args.base_url
            or str(coupon.get("base_url", "")).strip()
            or "https://web1.52pokemon.cc"
        )
        os.environ["POKEMON_BASE_URL"] = base_url
        purchase.set_base_url(base_url)

        raw_accounts = pool.parse_accounts_json(
            os.environ.get("POKEMON_ACCOUNTS_JSON", "")
        )
        secrets_to_redact.extend(
            value
            for item in raw_accounts
            for value in (
                str(item.get("email", "")),
                str(item.get("password", "")),
            )
        )
        accounts_by_id = {
            str(item["id"]): item for item in raw_accounts if item.get("id")
        }

        pool.initialize(args.db)
        connection = pool.connect(args.db)
        pool.seed_accounts(connection, raw_accounts, now)
        account = pool.select_account(connection, accounts_by_id, now)
        if not account:
            message = "没有可用账号：账号池为空、全部处于 30 天冷却期或需要处理。"
            _write_summary(["## 52Pokemon", "", message])
            print(message)
            return 3

        if not pool.start_run(connection, request_id, account["account_id"], now):
            message = "本次 request_id 已经执行过，已停止以避免重复下单。"
            pool.mark_failure(
                connection,
                account["account_id"],
                request_id,
                "duplicate_request",
                retryable=True,
                now=now,
            )
            _write_summary(["## 52Pokemon", "", message])
            print(message)
            return 4

        selected = accounts_by_id[account["account_id"]]
        os.environ["POKEMON_EMAIL"] = str(selected["email"]).strip()
        os.environ["POKEMON_PASSWORD"] = str(selected["password"])
        os.environ["POKEMON_REDEEM_CODE"] = redeem_code
        os.environ["POKEMON_NON_INTERACTIVE"] = "1"
        os.environ["POKEMON_DRY_RUN"] = "1" if args.dry_run else "0"

        order_id = purchase.run(
            redeem_code=redeem_code,
            dry_run=args.dry_run,
            step_delay=args.step_delay,
            non_interactive=True,
            base_url=base_url,
        )
        if args.dry_run:
            pool.mark_dry_run(connection, account["account_id"], request_id)
            summary = [
                "## 52Pokemon 检查完成",
                "",
                f"- 账号：`{account['label']}`",
                "- 模式：dry-run，未点击“立即订购”",
                f"- 网站：`{base_url}`",
                "- 账号状态：保持可用",
            ]
        else:
            next_available = pool.mark_success(
                connection,
                account["account_id"],
                request_id,
                order_id,
            )
            summary = [
                "## 52Pokemon 完成",
                "",
                f"- 账号：`{account['label']}`",
                f"- 网站：`{base_url}`",
                f"- 订单号：`{order_id or '未提取到'}`",
                f"- 下次可用：`{pool.to_iso(next_available)}`",
            ]
        _write_summary(summary)
        print("\n".join(summary))
        if args.result_file:
            Path(args.result_file).write_text(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": args.dry_run,
                        "account": account["label"],
                        "base_url": base_url,
                        "order_id": order_id,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return 0
    except BaseException as exc:
        code, retryable = _classify_failure(exc, secrets_to_redact)
        message = _redact(str(exc), secrets_to_redact)[:500]
        if connection is not None and account is not None:
            try:
                pool.mark_failure(
                    connection,
                    account["account_id"],
                    request_id,
                    code,
                    retryable=retryable,
                    now=pool.utc_now(),
                )
            except Exception:
                pass
        summary = [
            "## 52Pokemon 执行失败",
            "",
            f"- 错误类型：`{code}`",
            f"- 是否可重试：`{'是' if retryable else '否'}`",
            f"- 说明：{message or '未提供错误信息'}",
        ]
        _write_summary(summary)
        print("\n".join(summary))
        if args.result_file:
            Path(args.result_file).write_text(
                json.dumps(
                    {"ok": False, "dry_run": args.dry_run, "error": code},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return 2 if code in {"coupon_config_missing", "coupon_http_error", "coupon_unavailable", "coupon_empty"} else 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
