"""Small server-side trigger and coupon service for the 52Pokemon workflow."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
UTC = dt.timezone.utc
PREFIX = os.environ.get("URL_PREFIX", "").rstrip("/")
DB_PATH = Path(os.environ.get("APP_DB_PATH", "/app/shared/data/app.sqlite3"))
LOG_PATH = Path(os.environ.get("APP_LOG_PATH", "/app/shared/logs/app.log"))
BASE_URL = os.environ.get("BASE_URL", "https://18364454.xyz/muyu-pokemon").rstrip("/")
TRIGGER_SLUG = os.environ.get("TRIGGER_SLUG", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
COUPON_API_TOKEN = os.environ.get("COUPON_API_TOKEN", "").strip()
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "Augfif").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "pokemon_purchase").strip()
GITHUB_REF = os.environ.get("GITHUB_REF", "main").strip()
GITHUB_WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "pokemon_purchase.yml").strip()
GITHUB_TRIGGER_TOKEN = os.environ.get("GITHUB_TRIGGER_TOKEN", "").strip()
TRIGGER_COOLDOWN_SECONDS = int(os.environ.get("TRIGGER_COOLDOWN_SECONDS", "300"))
RUN_LOCK_SECONDS = int(os.environ.get("RUN_LOCK_SECONDS", "1800"))

DB_LOCK = threading.RLock()


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def to_utc_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_datetime(value: str | None, default: dt.datetime | None = None) -> dt.datetime | None:
    if not value:
        return default
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(UTC)


def local_input_value(value: str | None, fallback: dt.datetime | None = None) -> str:
    parsed = parse_datetime(value, fallback)
    if parsed is None:
        return ""
    return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")


def month_end(value: dt.datetime | None = None) -> dt.datetime:
    current = (value or utc_now()).astimezone(LOCAL_TZ)
    first_next = (current.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    return (first_next - dt.timedelta(seconds=1)).astimezone(UTC)


def db_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_database() -> None:
    with DB_LOCK:
        connection = db_connection()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS coupons (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    code TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_url TEXT,
                    ip TEXT,
                    error_code TEXT
                );

                CREATE TABLE IF NOT EXISTS trigger_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger = logging.getLogger("pokemon_trigger")
    logger.setLevel(logging.INFO)
    for existing in logger.handlers:
        existing.close()
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())


setup_logging()
logger = logging.getLogger("pokemon_trigger")
initialize_database()

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"),
    MAX_CONTENT_LENGTH=16 * 1024,
)


def json_error(message: str, status: int = 400, code: str = "bad_request"):
    return jsonify({"ok": False, "error": message, "code": code}), status


def current_ip() -> str:
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def csrf_ok(value: str | None) -> bool:
    expected = session.get("csrf", "")
    return bool(expected and value and hmac.compare_digest(expected, value))


def get_coupon() -> dict[str, Any]:
    with DB_LOCK:
        connection = db_connection()
        try:
            row = connection.execute(
                "SELECT code, valid_from, valid_until, updated_at FROM coupons WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
    if not row:
        return {"state": "missing", "valid_from": None, "valid_until": None}
    valid_from = parse_datetime(row["valid_from"])
    valid_until = parse_datetime(row["valid_until"])
    current = utc_now()
    if valid_from and current < valid_from:
        state = "not_started"
    elif valid_until and current >= valid_until:
        state = "expired"
    else:
        state = "active"
    return {
        "state": state,
        "valid_from": row["valid_from"],
        "valid_until": row["valid_until"],
        "updated_at": row["updated_at"],
        "valid_from_local": local_input_value(row["valid_from"]),
        "valid_until_local": local_input_value(row["valid_until"]),
        "code_preview": "••••" + row["code"][-4:] if len(row["code"]) > 4 else "••••",
    }


def admin_logged_in() -> bool:
    return bool(session.get("admin_logged_in"))


def admin_redirect():
    return redirect(f"{PREFIX}/admin/")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    return response


@app.get(f"{PREFIX}/api/health")
def health():
    coupon = get_coupon()
    return jsonify(
        {
            "status": "ok",
            "coupon_configured": coupon["state"] != "missing",
            "coupon_state": coupon["state"],
            "github_configured": bool(GITHUB_TRIGGER_TOKEN),
        }
    )


@app.get(f"{PREFIX}/api/coupon")
def coupon_api():
    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {COUPON_API_TOKEN}"
    if not COUPON_API_TOKEN or not hmac.compare_digest(supplied, expected):
        return json_error("unauthorized", 401, "unauthorized")
    coupon = get_coupon()
    if coupon["state"] != "active":
        return json_error(
            "兑换码尚未配置或已经过期",
            409,
            f"coupon_{coupon['state']}",
        )
    with DB_LOCK:
        connection = db_connection()
        try:
            row = connection.execute(
                "SELECT code, valid_from, valid_until FROM coupons WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
    if not row:
        return json_error("兑换码尚未配置", 409, "coupon_missing")
    return jsonify(
        {
            "ok": True,
            "code": row["code"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
        }
    )


@app.get(f"{PREFIX}/r/<slug>/")
def trigger_page(slug: str):
    if not TRIGGER_SLUG or not hmac.compare_digest(slug, TRIGGER_SLUG):
        return "Not found", 404
    session["trigger_slug"] = slug
    return render_template(
        "index.html",
        prefix=PREFIX,
        csrf=csrf_token(),
        coupon=get_coupon(),
        github_configured=bool(GITHUB_TRIGGER_TOKEN),
    )


def rate_limit(ip: str) -> bool:
    cutoff = utc_now() - dt.timedelta(seconds=TRIGGER_COOLDOWN_SECONDS)
    with DB_LOCK:
        connection = db_connection()
        try:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM trigger_events WHERE ip = ? AND created_at > ?",
                (ip, to_utc_iso(cutoff)),
            ).fetchone()["n"]
        finally:
            connection.close()
    return count == 0


def record_trigger(ip: str) -> None:
    with DB_LOCK:
        connection = db_connection()
        try:
            connection.execute(
                "INSERT INTO trigger_events(ip, created_at) VALUES (?, ?)",
                (ip, to_utc_iso(utc_now())),
            )
        finally:
            connection.close()


def active_run_exists() -> bool:
    with DB_LOCK:
        connection = db_connection()
        try:
            row = connection.execute(
                """
                SELECT 1 FROM requests
                 WHERE status IN ('dispatching', 'dispatched', 'running')
                   AND expires_at > ?
                 LIMIT 1
                """,
                (to_utc_iso(utc_now()),),
            ).fetchone()
        finally:
            connection.close()
    return bool(row)


def save_request(
    request_id: str,
    status: str,
    ip: str,
    run_url: str | None = None,
    error_code: str | None = None,
) -> None:
    now = utc_now()
    expires = now + dt.timedelta(seconds=RUN_LOCK_SECONDS)
    with DB_LOCK:
        connection = db_connection()
        try:
            connection.execute(
                """
                INSERT INTO requests
                    (request_id, created_at, expires_at, status, run_url, ip, error_code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status = excluded.status,
                    run_url = excluded.run_url,
                    error_code = excluded.error_code
                """,
                (request_id, to_utc_iso(now), to_utc_iso(expires), status, run_url, ip, error_code),
            )
        finally:
            connection.close()


def dispatch_github(request_id: str) -> str | None:
    if not GITHUB_TRIGGER_TOKEN:
        raise RuntimeError("github_token_missing")
    url = (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/actions/workflows/{GITHUB_WORKFLOW}/dispatches"
    )
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TRIGGER_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "muyu-pokemon-trigger/1.0",
        },
        json={
            "ref": GITHUB_REF,
            "inputs": {"request_id": request_id, "dry_run": "false"},
        },
        timeout=20,
    )
    if response.status_code not in (200, 204):
        raise RuntimeError(f"github_http_{response.status_code}")
    if response.status_code == 200 and response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return payload.get("html_url") or payload.get("run_url")
    return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/actions"


@app.post(f"{PREFIX}/api/trigger")
def trigger():
    if not csrf_ok(request.headers.get("X-CSRF-Token")):
        return json_error("CSRF 校验失败", 403, "csrf_failed")
    if not hmac.compare_digest(session.get("trigger_slug", ""), TRIGGER_SLUG):
        return json_error("触发入口无效", 403, "invalid_trigger_entry")
    if not TRIGGER_SLUG:
        return json_error("触发入口未配置", 503, "trigger_not_configured")
    coupon = get_coupon()
    if coupon["state"] != "active":
        return json_error(
            "兑换码尚未配置或已经过期，请先登录管理页更新",
            409,
            f"coupon_{coupon['state']}",
        )
    ip = current_ip()
    if not rate_limit(ip):
        return json_error("操作过于频繁，请稍后再试", 429, "rate_limited")
    if active_run_exists():
        return json_error("已有任务在运行，请等待当前任务结束", 409, "run_in_progress")

    request_id = uuid.uuid4().hex
    record_trigger(ip)
    save_request(request_id, "dispatching", ip)
    try:
        run_url = dispatch_github(request_id)
    except Exception as exc:
        logger.error("dispatch failed request_id=%s error=%s", request_id, exc.__class__.__name__)
        save_request(request_id, "failed", ip, error_code=str(exc))
        if str(exc) == "github_token_missing":
            return json_error("服务器尚未配置 GitHub 触发 Token", 503, "github_token_missing")
        if str(exc).startswith("github_http_"):
            return json_error("GitHub 拒绝了触发请求，请检查 Token 和仓库设置", 502, "github_rejected")
        return json_error("无法连接 GitHub", 502, "github_unavailable")
    save_request(request_id, "dispatched", ip, run_url=run_url)
    logger.info("dispatched request_id=%s", request_id)
    return jsonify(
        {
            "ok": True,
            "request_id": request_id,
            "run_url": run_url,
            "message": "任务已经提交。GitHub Actions 会在后台开始执行。",
        }
    )


@app.get(f"{PREFIX}/admin/")
def admin_page():
    if not admin_logged_in():
        return render_template("admin_login.html", prefix=PREFIX, csrf=csrf_token())
    coupon = get_coupon()
    now_local = utc_now().astimezone(LOCAL_TZ)
    return render_template(
        "admin.html",
        prefix=PREFIX,
        csrf=csrf_token(),
        coupon=coupon,
        code_preview=coupon.get("code_preview", "••••"),
        default_from=local_input_value(None, now_local),
        default_until=local_input_value(None, month_end()),
        saved=request.args.get("saved") == "1",
    )


@app.post(f"{PREFIX}/admin/login")
def admin_login():
    if not csrf_ok(request.form.get("csrf")):
        return "CSRF 校验失败", 403
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    valid = False
    if ADMIN_PASSWORD_HASH:
        valid = hmac.compare_digest(username, ADMIN_USERNAME) and check_password_hash(
            ADMIN_PASSWORD_HASH, password
        )
    elif ADMIN_PASSWORD:
        valid = hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(
            password, ADMIN_PASSWORD
        )
    if not valid:
        return render_template(
            "admin_login.html",
            prefix=PREFIX,
            csrf=csrf_token(),
            error="用户名或密码不正确",
        ), 401
    session.clear()
    session["admin_logged_in"] = True
    session["csrf"] = secrets.token_urlsafe(32)
    return admin_redirect()


@app.post(f"{PREFIX}/admin/coupon")
def admin_coupon():
    if not admin_logged_in():
        return admin_redirect()
    if not csrf_ok(request.form.get("csrf")):
        return "CSRF 校验失败", 403
    code = request.form.get("code", "").strip()
    if not code or len(code) > 128 or any(char in code for char in "\r\n"):
        return "兑换码格式不正确", 400
    valid_from = parse_datetime(request.form.get("valid_from"))
    valid_until = parse_datetime(request.form.get("valid_until"))
    if valid_from is None:
        valid_from = utc_now()
    if valid_until is None:
        valid_until = month_end(valid_from)
    if valid_until <= valid_from:
        return "失效时间必须晚于生效时间", 400
    with DB_LOCK:
        connection = db_connection()
        try:
            connection.execute(
                """
                INSERT INTO coupons(id, code, valid_from, valid_until, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    code = excluded.code,
                    valid_from = excluded.valid_from,
                    valid_until = excluded.valid_until,
                    updated_at = excluded.updated_at
                """,
                (code, to_utc_iso(valid_from), to_utc_iso(valid_until), to_utc_iso(utc_now())),
            )
        finally:
            connection.close()
    logger.info("coupon updated")
    return redirect(f"{PREFIX}/admin/?saved=1")


@app.post(f"{PREFIX}/admin/logout")
def admin_logout():
    session.clear()
    return admin_redirect()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "18082")),
        debug=False,
    )
