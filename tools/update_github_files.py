"""Update selected repository files through the GitHub Contents API.

This helper is used by the server-side deployment path when a local Git proxy
is unavailable. It never prints the GitHub token or file contents.
"""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

import requests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--message", default="Update deployment files")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TRIGGER_TOKEN", "").strip()
    owner = os.environ.get("GITHUB_OWNER", "Augfif")
    repo = os.environ.get("GITHUB_REPO", "pokemon_purchase")
    branch = os.environ.get("GITHUB_REF", "main")
    if not token:
        print("GITHUB_TRIGGER_TOKEN is not configured")
        return 1
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "muyu-update-helper/1.0",
    }
    base = f"https://api.github.com/repos/{owner}/{repo}/contents/"
    for relative in args.files:
        local = args.root / relative
        if not local.is_file():
            print(f"{relative}: local file missing")
            return 1
        current = requests.get(
            base + relative,
            headers=headers,
            params={"ref": branch},
            timeout=20,
        )
        current.raise_for_status()
        response = requests.put(
            base + relative,
            headers=headers,
            json={
                "message": args.message,
                "content": base64.b64encode(local.read_bytes()).decode("ascii"),
                "sha": current.json()["sha"],
                "branch": branch,
            },
            timeout=30,
        )
        print(f"{relative}: HTTP {response.status_code}")
        if response.status_code not in (200, 201):
            print(response.text[:300])
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
