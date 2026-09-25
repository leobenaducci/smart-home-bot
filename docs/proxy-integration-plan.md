# Proxy integration plan (historical)

> **This is the plan the proxy was built from, kept as a record of why it is
> shaped the way it is. It is not a description of what ships.** All five
> phases below are done — the cloud proxy, the Android app, device enrollment
> and ntfy push are all in the tree — so read the status markers as history,
> not as work outstanding.
>
> Two decisions in here were *reversed* after it was written, and the current
> ones are the opposite:
>
> - **The VPS holds no user store.** The plan's "Auth source of truth: HomeCore
>   `users.json` (bcrypt), no separate user DB" meant copying the hashes onto
>   the VPS. It ships as `AUTH_MODE: upstream`: the proxy forwards credentials
>   down the tunnel and the home machine verifies them.
> - **Deployment is the stack's deployer**, `./home-stack deploy cloud-proxy`,
>   not a per-directory script and not Jenkins.
>
> For what is actually true now: [optional-cloud.md](optional-cloud.md),
> [proxy-home-setup.md](proxy-home-setup.md),
> [proxy-device-enrollment.md](proxy-device-enrollment.md).

Goal: let family chat with **Alfred** from anywhere (no VPN), using a cloud VPS +
an Android app. Same login as HomeCore. The Android app is a **thin WebView
wrapper** of HomeCore's existing chat UI, limited to chat only.

---

## Decisions (locked)

| Topic | Decision |
|---|---|
| Android client | **WebView wrapper** of HomeCore `chat.html` (reuse streaming/voice/images/history/files) |
| VPS ↔ home link | **Reverse tunnel initiated from home** (home dials out to the VPS) |
| Public TLS | Own domain + **Caddy / Let's Encrypt** (e.g. `chat.<yourdomain>`) |
| Auth source of truth | HomeCore `users.json` (bcrypt), no separate user DB |

---

## Architecture

```
 ┌────────────┐  HTTPS (real cert)   ┌──────────────────────────┐  reverse tunnel  ┌─────────────────┐
 │  Android   │ ──────────────────►  │  VPS (public cloud)      │  (home → VPS)    │ HomeCore local   │
 │  WebView   │  30-day cookie       │  Caddy (TLS)             │ ◄──────────────  │ assistant.home    │
 │  wrapper   │ ◄──────────────────  │  home-chat proxy         │  X-Proxy-User    │ :8443           │
 └────────────┘  SSE stream          │  (FastAPI)               │  X-Proxy-Secret  └───────┬─────────┘
                                      └──────────────────────────┘                          │
                                                                                    nanobot :890X/:876X
                                                                                    whisper.home
```

- Home initiates the tunnel outward, so **nothing at home is port‑forwarded**.
- The proxy reaches HomeCore *local* (not nanobot directly) to reuse all existing
  chat logic: 4‑phase spawn streaming, history, Whisper, workspace files.

### Reverse tunnel options (pick one in Phase 0)

1. **WireGuard (recommended).** Home is a WG peer dialing the VPS (public WG
   server). Home sets `PersistentKeepalive = 25` so the VPS can reach home's WG
   IP at any time. Proxy targets `https://10.x.x.2:8443`. Stable, encrypted,
   behaves like a permanent private link.
2. **frp / rathole.** `frpc` at home, `frps` on VPS; exposes HomeCore:8443 as a
   VPS‑localhost port. Purpose‑built, robust reconnects.
3. **autossh reverse tunnel.** `ssh -R 8443:homecore-host:8443 vps` kept alive by
   autossh. Simplest, least robust.

Whichever is chosen, the proxy just needs a `HOMECORE_LOCAL_URL` pointing at the
home side of the tunnel.

---

## Components & changes

### A. HomeCore local — trusted‑proxy auth shim  *(Phase 1 — code)*
Add a `before_request` that, when a request carries `X-Proxy-Secret` (matching a
shared secret) **and** `X-Proxy-User` (a known user), populates `session['user']`
so every existing `@login_required` / `@api_login_required` handler works
unchanged. An `after_request` strips the `Set-Cookie` from proxied responses so
no session cookie leaks back to the proxy. Mirrors the existing `_debug_auth()`
trust model. `?embed=1` on `/chat` hides the nav/logout links for the WebView.

### B. Cloud proxy — repurpose `home-chat/server/`
- Drop P2P bits (SQLite `User`/`Message`, `/users`, `/messages`, `/ws`). Keep
  JWT/cookie helpers in `auth.py`.
- `GET/POST /login` → verify against HomeCore `users.json` (bcrypt) → 30‑day
  signed, HttpOnly, Secure cookie. Reuse HomeCore login card styling.
- Reverse‑proxy chat routes to `HOMECORE_LOCAL_URL`, injecting
  `X-Proxy-Secret` + `X-Proxy-User=<cookie user>`. **Stream SSE** for
  `/chat/send` and `/chat/events`; pass through `/chat/history`,
  `/chat/transcribe` (multipart), `/chat/workspace*`, `/chat/download`.
- `/` → `/chat?embed=1` when authed, else `/login`.
- HomeCore uses a self‑signed cert → proxy HTTP client to it uses `verify=False`
  (link is private) or pins the cert.

### C. Android — WebView wrapper (replace scaffold)
- Single WebView activity loading `https://<domain>/`.
- Persist cookies (`CookieManager`, flush on pause) → login survives restarts.
- Grant mic (`onPermissionRequest`) for voice/Whisper; file/camera
  (`onShowFileChooser`) for image attach.
- Lock navigation to the domain (`shouldOverrideUrlLoading`).
- Remove `usesCleartextTraffic`; add `RECORD_AUDIO`, `CAMERA` permissions.

### D. Deployment
- Caddy for auto‑TLS on `chat.<yourdomain>` → FastAPI proxy.
- docker-compose: `chat-proxy` + `caddy` (+ tunnel client if containerized).
- Env secrets: `PROXY_SHARED_SECRET` (shared with HomeCore), `SESSION_SIGNING_KEY`,
  `HOMECORE_LOCAL_URL`.

---

## Auth flow
1. App opens, no cookie → proxy serves `/login`.
2. User enters HomeCore credentials → proxy verifies vs `users.json` → 30‑day cookie.
3. Chat request → proxy reads cookie → forwards to HomeCore with `X-Proxy-User` +
   `X-Proxy-Secret`.
4. HomeCore resolves the user's `nanobot_id` as usual → correct Alfred instance.

---

## Security (VPS is public)
- Only the proxy is exposed; home stays private behind the outbound tunnel.
- Real TLS (Caddy/Let's Encrypt); no self‑signed cert ever reaches the phone.
- Rate‑limit `/login` + lockout.
- Rotatable cookie signing key; optional per‑device revocation list (only reason
  to keep a small DB).

---

## Phased rollout
0. **Network:** ⏳ *in progress* — VPS provisioned (Ubuntu 24.04, Docker, host
   Caddy). Reverse tunnel chosen = **autossh**; VPS `tunnel` user created and
   awaiting the home host's public key. Home side pending — see `docs/proxy-home-setup.md`.
1. **HomeCore proxy‑auth shim** + `?embed=1` layout. ✅ *code done* (activates when
   `PROXY_SHARED_SECRET` is set on the home side).
2. **Cloud proxy** (login + SSE passthrough). ✅ *deployed & live* at
   `https://chat.home` (verified: TLS, login page, 13/13 local e2e
   tests). Fronted by the host Caddy → `127.0.0.1:8080`.
3. **Android WebView** wrapper; sideload, verify streaming/voice/images. ⬜ next
   after chat works end‑to‑end.
4. **Roll out** to family phones. ⬜
5. **Push notifications** via a self-hosted **ntfy** foreground service (not
   FCM) for proactive Alfred messages while backgrounded (SSE only works while
   the app is foregrounded). ✅ *code done* — see `docs/proxy-notifications.md`.

### Deployment specifics (as built)
- VPS `/opt/home-chat/` holds the compose project; secrets in `.env` (chmod 600).
- Proxy runs `network_mode: host`, uvicorn bound to `127.0.0.1:8080`.
- `data/users.json` = copy of `HomeCore/cloud/users.json` (same file HomeCore local
  mounts, so credentials stay in sync).
- `chat.home` block appended to `/etc/caddy/Caddyfile` (backup saved).
- autossh endpoint: `HOMECORE_LOCAL_URL=https://127.0.0.1:8443` on the VPS.
- To push server code changes: `./home-stack deploy cloud-proxy` (and
  `./deploy/deploy.py cloud-proxy --dry-run` to preview). The standalone
  `services/proxy/deploy.sh` predates the deployer and is not what this stack
  runs.
