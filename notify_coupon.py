"""Send the first-of-month WxPusher reminder."""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    token = os.environ.get("WXPUSHER_SPT", "").strip()
    admin_url = os.environ.get(
        "POKEMON_ADMIN_URL", "https://18364454.xyz/muyu-pokemon/admin/"
    ).strip()
    if not token:
        print("WXPUSHER_SPT is not configured", file=sys.stderr)
        return 1
    content = (
        "今天是每月一号，请更新 52Pokemon 兑换码。\n"
        f"管理入口：{admin_url}\n"
        "账号冷却按使用后 30 天计算，本次不会自动下单。"
    )
    payload = json.dumps(
        {
            "content": content,
            "summary": "52Pokemon 兑换码更新提醒",
            "contentType": 2,
            "spt": token,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://wxpusher.zjiecode.com/api/send/message/simple-push",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "pokemon-reminder/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"WxPusher request failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    if str(result.get("code")) != "0":
        print("WxPusher rejected the reminder", file=sys.stderr)
        return 1
    print("WxPusher reminder sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
