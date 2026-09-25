#!/usr/bin/env python3
"""Admin CLI for per-user ntfy subscribe config. Run inside the container:

  docker compose exec chat-proxy python manage_ntfy.py set <username> <topic1,topic2,...> [token]
  docker compose exec chat-proxy python manage_ntfy.py list
  docker compose exec chat-proxy python manage_ntfy.py remove <username>

Example: manage_ntfy.py set user1 Alex,Parents,Family <read-only-token>

The token is a per-user ntfy access token (ntfy token add <ntfy-username>),
not that ntfy account's password - see ntfy's own docs on access tokens.
"""
import sys

import ntfy_config


def cmd_set(args):
    if len(args) < 2:
        print("Usage: manage_ntfy.py set <username> <topic1,topic2,...> [token]")
        sys.exit(1)
    username = args[0]
    topics = [t.strip() for t in args[1].split(",") if t.strip()]
    if not topics:
        print("At least one topic is required.")
        sys.exit(1)
    token = args[2] if len(args) > 2 else ""
    ntfy_config.set_ntfy_config(username, topics, token)
    print(f"Set ntfy config for {username}: topics={topics}")


def cmd_list(_args):
    rows = ntfy_config.list_all()
    if not rows:
        print("No ntfy config set.")
        return
    for username, cfg in rows.items():
        topics = ",".join(cfg.get("topics", []))
        has_token = "set" if cfg.get("token") else "(none)"
        print(f"{username:>12}  topics={topics:<24}  token={has_token}")


def cmd_remove(args):
    if len(args) < 1:
        print("Usage: manage_ntfy.py remove <username>")
        sys.exit(1)
    ok = ntfy_config.remove_ntfy_config(args[0])
    print("Removed." if ok else "No config for that user.")
    if not ok:
        sys.exit(1)


COMMANDS = {"set": cmd_set, "list": cmd_list, "remove": cmd_remove}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
