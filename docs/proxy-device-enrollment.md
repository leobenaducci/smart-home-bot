# Device enrollment (restricting remote login to enrolled devices)

Remote login (`chat.home`) requires both the account password *and*
a per-device signature from a registered EC keypair. A device's keypair is
generated on-device (Android Keystore; the private key never leaves it) and
its public half must be registered by an admin before that device can log in
at all — the correct password alone is not enough from an unregistered
device. See `services/proxy/server/devices.py` for the implementation.

Local HomeCore login (Firefox on the home LAN, hitting HomeCore directly) is
unaffected by any of this — it's a separate app with its own login.

## Required `.env` (VPS, `~/.local/share/home-stack/cloud-proxy/vps/.env`)

See `services/proxy/.env.example`. Three values, none new for this feature:

```
HOMECORE_LOCAL_URL=https://127.0.0.1:21100  # autossh -R endpoint on the VPS
PROXY_SHARED_SECRET=<same value as HomeCore's PROXY_SHARED_SECRET>
SESSION_SIGNING_KEY=<random, e.g. openssl rand -hex 32>
```

`PROXY_SHARED_SECRET` must be **byte-for-byte identical** between home-chat's
`.env` and HomeCore's own environment (it's what lets home-chat's proxy vouch
for a logged-in user to HomeCore over the tunnel). A mismatch causes an
`ERR_TOO_MANY_REDIRECTS` loop after login — see Troubleshooting below.

`TOKEN_EXPIRE_DAYS` (optional, default 180) controls how long a device stays
logged in before needing the password again (independent of device
enrollment, which never expires on its own — see Revoking a device).

## Enrolling a new device

No HTTP route can register a device or generate a code — only SSH access to
the VPS can. This is deliberate: a code obtained by anyone other than the
admin is useless without also being able to run this CLI.

```bash
ssh root@chat.home
cd ~/.local/share/home-stack/cloud-proxy/vps
docker compose exec chat-proxy python manage_devices.py code <username>
# -> Code for user1: 483920  (expires in 10 minutes, single use)
```

`<username>` is the member's login id — the same one the portal's user store
uses, e.g. `user1` — not a display name.

Then, on the phone (within 10 minutes):

1. Install the current APK build (see Android app section below) if not
   already installed.
2. Open the app. If it isn't enrolled yet, it shows a 6-digit code field
   instead of the normal login form.
3. Enter the code. On success the page reloads into the normal login form.
4. Log in with the account's usual password.

## Managing devices

```bash
cd ~/.local/share/home-stack/cloud-proxy/vps
docker compose exec chat-proxy python manage_devices.py list
docker compose exec chat-proxy python manage_devices.py add <username> "<public_key_b64>" "label"
docker compose exec chat-proxy python manage_devices.py remove <id>
```

`add` is a manual fallback for registering a public key directly (e.g. if you
already have the base64 DER text some other way) without a code round-trip.
`remove` revokes a device immediately — the next login attempt from it fails
regardless of password. Logging out of the app does **not** revoke the
device; only `remove` does.

## Android app

`services/proxy/android/` is a plain WebView wrapper (Kotlin, Gradle, no CI — sideloaded
only). It has no build/signing pipeline; rebuild and reinstall on each phone
manually whenever the app changes. `MainActivity.kt`'s `DeviceKeyBridge`
(exposed to the login page as `window.AndroidDeviceKey`) generates and holds
the per-device keypair — nothing else in the app needs to change to add or
remove a device.

## Troubleshooting

### `sqlite3.OperationalError: unable to open database file`

Docker creates an **empty directory** (not a file) at a bind-mount source
path that doesn't exist yet when a container first starts. If
`~/.local/share/home-stack/cloud-proxy/vps/data/devices.db` ends up as a directory instead of a file, every DB
operation fails this way. The deployer creates its `state:` paths before the
compose run precisely so this cannot happen; you see it after a hand-started
container.

Check:
```bash
ls -la ~/.local/share/home-stack/cloud-proxy/vps/data/
```
If either shows as a directory (`drwxr-xr-x`), fix with:
```bash
# `&&`, not two lines: if it is a real database rather than an empty
# directory, rmdir fails and the next command must not run.
rmdir ~/.local/share/home-stack/cloud-proxy/vps/data/devices.db && touch ~/.local/share/home-stack/cloud-proxy/vps/data/devices.db
```

### Login always says "incorrect"

**There is no user store on the VPS, and there must not be one.** The proxy
ships `AUTH_MODE: upstream` (`deploy/manifest.yml`, and the default in
`services/proxy/server/auth.py`): it forwards the credentials down the tunnel
to the portal's `/api/auth/verify` and believes the answer. The bcrypt hashes
stay on the machine at home. Do not copy `users.json` out there to "fix" a
login — it is never read in this mode, and it puts every household password
hash on the one box you do not physically control.

So a login that always fails is the tunnel or the shared secret, not a file:

```bash
# 1. Is the home end of the tunnel up, seen from the VPS?
curl -sf http://127.0.0.1:21100/healthz    # the tunnel_port from cloud.vps

# 2. Does the portal answer the verify endpoint the proxy calls?
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1:21100/api/auth/verify

# 3. Does the proxy agree with the portal about the shared secret?
cd ~/.local/share/home-stack/cloud-proxy/vps
docker compose exec chat-proxy sh -c 'echo -n "$PROXY_SHARED_SECRET" | sha256sum'
```

A network failure is a failed login, not a successful one, so an unreachable
upstream looks exactly like a wrong password from the browser. Compare the
hash in step 3 against `PROXY_SHARED_SECRET` in `secrets/smart-home-bot.env`
on the home side; if they differ, redeploy both ends.

`AUTH_MODE: local` — verifying against a synced copy of the store — still
exists for compatibility and is not what this stack deploys.

### `ERR_TOO_MANY_REDIRECTS` after a successful-looking login

Means `PROXY_SHARED_SECRET` doesn't match between home-chat and HomeCore.
HomeCore rejects the proxied request, redirects to its *own* `/login`, and
that redirect gets relayed straight back through the proxy to the browser —
which (since home-chat itself thinks you're logged in) bounces right back to
`/chat`, looping forever.

1. Confirm the tunnel itself is up first: `curl -s https://chat.home/healthz`
   should show `"upstream": true`. If `false`, that's a separate
   connectivity problem (check the `vps-tunnel` systemd service on the home
   side, per `docs/proxy-home-setup.md`).
2. Compare the secret on both sides *without* printing the raw value:
   ```bash
   # on the VPS
   cd ~/.local/share/home-stack/cloud-proxy/vps && docker compose exec chat-proxy sh -c 'echo -n "$PROXY_SHARED_SECRET" | sha256sum'
   # on the HomeCore host
   docker compose exec web sh -c 'echo -n "$PROXY_SHARED_SECRET" | sha256sum'
   ```
   If the hashes differ, generate a fresh value (`openssl rand -hex 32`), set
   it as the **same** string in both the `PROXY_SHARED_SECRET`
   credential (used by HomeCore's Jenkinsfile) and `~/.local/share/home-stack/cloud-proxy/vps/.env`, then
   redeploy both (trigger the HomeCore Jenkins job; on the VPS
   `docker compose up -d --force-recreate`).
