# proxy

The chat proxy. A small FastAPI app (`server/`) that reverse-proxies `/chat*`,
`/tasks*` and `/geo*` to the portal, and a thin Android WebView wrapper
(`android/`) that points at it.

It is deployed twice from this one directory, and the manifest names both:

- **`local-proxy`** — on your own machine, always on. The same public name
  answers inside the house, so a phone that walks in the door stops going out
  to the internet and back.
- **`cloud-proxy`** — optional, off by default, on a VPS or VM you own, reached
  over a reverse tunnel dialled *out* from home. It holds no application code
  and no household data; passwords are verified at home
  (`AUTH_MODE: upstream` → the portal's `/api/auth/verify`).

Remote login requires the account password **and** a per-device signature from
an enrolled device — see [device enrolment](../../docs/proxy-device-enrollment.md).

## Generate a one-time enrollment code (new device)

No HTTP route can mint a code — only SSH to the VPS can. The code is **single
use** and expires in **10 minutes**.

```bash
ssh root@chat.home
cd ~/.local/share/home-stack/cloud-proxy/vps
docker compose exec chat-proxy python manage_devices.py code <username>
# -> Code for user1: 483920  (expires in 10 minutes, single use)
```

- `<username>` is the **numeric login ID** from `users.json` (e.g. `user1`),
  not a display name — the user must already exist there.
- On the phone within 10 min: open the app → it shows a 6-digit code field (only
  if the device isn't enrolled yet) → enter the code → it reloads to the normal
  login → sign in with the account password.

Manage enrolled devices: `manage_devices.py list | add <username> "<pubkey_b64>" "label" | remove <id>`.
Full details and troubleshooting: [device enrolment](../../docs/proxy-device-enrollment.md).

## Deploy

Through the stack's deployer, like everything else:

```bash
./home-stack deploy local-proxy     # the copy on your own machine
./home-stack deploy cloud-proxy     # the VPS copy, if cloud.vps.enabled
```

State — the enrolled device keys and per-user notification topics — lives
outside the pushed tree (`{paths.state}/local-proxy`, `{paths.state}/cloud-proxy`),
so a redeploy never touches it. See
[optional-cloud.md](../../docs/optional-cloud.md) for the VPS switch and
[the home-side setup](../../docs/proxy-home-setup.md) for the tunnel.
