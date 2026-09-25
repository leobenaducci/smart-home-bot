"""User lookup against a copy of HomeCore's users.json.

The file is synced from HomeCore (same bcrypt hashes + usernames). Keeping a local
copy means login works from the same source of truth without exposing HomeCore's
own login form over the tunnel. Re-read on each lookup so a synced update is
picked up without restarting the proxy.
"""
import os
import json

USERS_FILE = os.environ.get("USERS_FILE", "/app/data/users.json")


def load_users() -> list:
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def find_user(username: str) -> dict | None:
    return next((u for u in load_users() if u.get("username") == username), None)
