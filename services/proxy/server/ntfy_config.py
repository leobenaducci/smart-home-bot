"""Per-user ntfy subscribe config (topics + read-only access token).

Admin-managed via manage_ntfy.py, independent of users.json (which is a
read-only synced mirror of HomeCore's user store). Re-read on each lookup so an
admin edit is picked up without restarting the proxy, same style as users.py.
"""
import os
import json

NTFY_CONFIG_FILE = os.environ.get("NTFY_CONFIG_FILE", "/app/data/ntfy_config.json")


def _load() -> dict:
    try:
        with open(NTFY_CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    with open(NTFY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_ntfy_config(username: str) -> dict | None:
    return _load().get(username)


def set_ntfy_config(username: str, topics: list[str], token: str = "") -> None:
    data = _load()
    data[username] = {"topics": topics, "token": token}
    _save(data)


def remove_ntfy_config(username: str) -> bool:
    data = _load()
    if username not in data:
        return False
    del data[username]
    _save(data)
    return True


def list_all() -> dict:
    return _load()
