"""GitHub Contents API access for the account-pool database."""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any, Callable

import requests

try:
    from .account_store import add_account, read_accounts, update_account
except ImportError:  # Docker runs github_pool.py as a top-level module.
    from account_store import add_account, read_accounts, update_account


class PoolAdminError(RuntimeError):
    def __init__(self, message: str, code: str = "pool_admin_error"):
        super().__init__(message)
        self.code = code


class PoolAdmin:
    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        branch: str = "main",
        path: str = "pool.db",
        session: Any | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.branch = branch
        self.path = path
        self.session = session or requests
        self.url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "muyu-pokemon-pool/1.0",
        }

    def _require_token(self) -> None:
        if not self.token:
            raise PoolAdminError(
                "服务器尚未配置 GitHub 账号池 Token", "github_token_missing"
            )

    def fetch(self) -> tuple[bytes, str, str]:
        self._require_token()
        response = self.session.get(
            self.url,
            headers=self._headers(),
            params={"ref": self.branch},
            timeout=20,
        )
        if response.status_code == 404:
            raise PoolAdminError("GitHub 中找不到 pool.db", "pool_not_found")
        if response.status_code == 401:
            raise PoolAdminError("GitHub Token 无效", "github_unauthorized")
        if response.status_code != 200:
            raise PoolAdminError(
                f"读取 pool.db 失败（HTTP {response.status_code}）",
                "github_read_failed",
            )
        payload = response.json()
        try:
            data = base64.b64decode(payload["content"])
        except (KeyError, ValueError, TypeError) as exc:
            raise PoolAdminError("GitHub 返回的 pool.db 无法解码", "pool_decode_failed") from exc
        return data, payload.get("sha", ""), payload.get("html_url", "")

    def list_accounts(self) -> list[dict[str, Any]]:
        data, _sha, _html_url = self.fetch()
        try:
            return read_accounts(data)
        except Exception as exc:
            raise PoolAdminError(str(exc), "pool_invalid") from exc

    def _put(self, data: bytes, sha: str, message: str) -> dict[str, Any]:
        response = self.session.put(
            self.url,
            headers=self._headers(),
            json={
                "message": message,
                "content": base64.b64encode(data).decode("ascii"),
                "sha": sha,
                "branch": self.branch,
            },
            timeout=30,
        )
        if response.status_code in (200, 201):
            return response.json()
        if response.status_code in (409, 422):
            raise PoolAdminError("账号池已被其他任务更新，请重试", "pool_conflict")
        if response.status_code in (401, 403):
            raise PoolAdminError(
                "GitHub Token 没有 pool.db 的 Contents 写入权限",
                "github_write_forbidden",
            )
        raise PoolAdminError(
            f"写入 pool.db 失败（HTTP {response.status_code}）",
            "github_write_failed",
        )

    def mutate(
        self,
        operation: Callable[[bytes], bytes],
        message: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for _attempt in range(3):
            data, sha, _html_url = self.fetch()
            try:
                changed = operation(data)
                return self._put(changed, sha, message)
            except PoolAdminError as exc:
                last_error = exc
                if exc.code != "pool_conflict":
                    raise
        raise PoolAdminError("账号池更新冲突，请稍后刷新重试", "pool_conflict") from last_error

    def add_account(self, account_id: str, label: str) -> dict[str, Any]:
        return self.mutate(
            lambda data: add_account(data, account_id, label),
            f"Add account {account_id}",
        )

    def update_account(
        self,
        account_id: str,
        status: str,
        next_available_at: dt.datetime | None,
        label: str | None = None,
    ) -> dict[str, Any]:
        return self.mutate(
            lambda data: update_account(
                data, account_id, status, next_available_at, label
            ),
            f"Update account {account_id}",
        )
