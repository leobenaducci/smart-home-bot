#!/usr/bin/env python3
"""Admin CLI for home-chat device enrollment - no HTTP route generates a code
or registers a key directly (see devices.py); only this CLI, run over SSH,
can. Run inside the running container, e.g.:

  ssh root@chat.home
  cd /opt/home-chat
  docker compose exec chat-proxy python manage_devices.py code <username>
  docker compose exec chat-proxy python manage_devices.py list
  docker compose exec chat-proxy python manage_devices.py add <username> "<public_key_b64>" "Sam's phone"
  docker compose exec chat-proxy python manage_devices.py remove <id>

Normal flow: run `code <username>`, tell the family member the 6-digit code
(text, in person, whatever), they enter it once on the app's login screen.
`add` is the manual fallback if you'd rather paste a public key directly.
"""
import sys
from datetime import datetime

import devices


def _fmt(ts):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else '-'


def cmd_code(args):
    if len(args) < 1:
        print('Usage: manage_devices.py code <username>')
        sys.exit(1)
    username = args[0]
    code = devices.generate_enroll_code(username)
    print(f'Code for {username}: {code}  (expires in 10 minutes, single use)')


def cmd_list(_args):
    rows = devices.list_all_devices()
    if not rows:
        print('No devices registered.')
        return
    for r in rows:
        print(f"[{r['id']}] {r['username']:>12}  {r['label'] or '(no label)':<30}  "
              f"added {_fmt(r['created_at'])}  last used {_fmt(r['last_used'])}")


def cmd_add(args):
    if len(args) < 2:
        print('Usage: manage_devices.py add <username> <public_key_b64> [label]')
        sys.exit(1)
    username, public_key_b64 = args[0], args[1]
    label = args[2] if len(args) > 2 else ''
    ok = devices.register_device_key(username, public_key_b64, label)
    if ok:
        print(f'Registered device for {username}.')
    else:
        print('Failed - public key invalid/malformed, or already registered.')
        sys.exit(1)


def cmd_remove(args):
    if len(args) < 1:
        print('Usage: manage_devices.py remove <id>')
        sys.exit(1)
    ok = devices.remove_device(int(args[0]))
    print('Removed.' if ok else 'No device with that id.')
    if not ok:
        sys.exit(1)


COMMANDS = {'code': cmd_code, 'list': cmd_list, 'add': cmd_add, 'remove': cmd_remove}

if __name__ == '__main__':
    devices.init_db()
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
