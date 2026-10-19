"""Seed non-secret account rows from POKEMON_ACCOUNTS_JSON into pool.db."""

from __future__ import annotations

import argparse
import os
import sys

import pool


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync account IDs into pool.db")
    parser.add_argument("--db", default="pool.db")
    args = parser.parse_args()
    try:
        accounts = pool.parse_accounts_json(
            os.environ.get("POKEMON_ACCOUNTS_JSON", "")
        )
        pool.initialize(args.db)
        connection = pool.connect(args.db)
        try:
            added = pool.seed_accounts(connection, accounts)
        finally:
            connection.close()
    except pool.PoolError as exc:
        print(f"Account pool sync failed: {exc}", file=sys.stderr)
        return 2
    print(f"Synchronized {len(accounts)} account IDs; added {added}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
