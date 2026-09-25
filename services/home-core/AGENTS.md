# AGENTS.md

Guidance for working in this repo. "Mi Casa" — a private household portal for a
home lab, used day-to-day by the family (Alex, Sam, Robin, Kai). UI copy is in
Spanish (locale `es`).

## The cameras, served from here (`/camaras/`)

Both were links to another host — cameras behind one shared admin password,
lights behind no door at all — so neither could say *which* member was looking,
and neither could carry that member's theme. `_house_proxy` forwards them as
the logged-in member, with the same derived token finance_helper takes.

Three things it does that `finanzas_proxy` does not, each because those apps
write their own absolute paths:

- **`X-Forwarded-Prefix`** — the mount. HomeCameras renders it as `{{ base }}`
  (`''` on the LAN, `/camaras` here); SmartButton has it stamped into the page by
  its own server from the same header — read off `location` it was only right at
  one path depth, and wrong silently.
- **Rewrites the far side's `Location`**, or logging in over there throws you
  out to this app's login.
- **`/camaras/_csrf`, `/luces/_csrf`** (`proxied_csrf`, shared with
  `/finanzas/_csrf`). Proxied, those pages are same-origin with this app, so
  `login_required` refuses any POST/PUT/DELETE without a token — correctly, or
  any site could post to `/camaras/api/*` with the family's cookie. They fetch
  it and attach it; without it every write answers 403 with an HTML body that
  the page reports as `JSON.parse: unexpected character at line 1 column 1`.

`X-CSRF-Token` is in `_PROXY_SKIP_HEADERS` with `cookie` and `authorization`:
it is this app's credential and the far side has no use for it.

**`/theme/night.css`** is a theme translated for the camera wall, which uses
its own token names (`--bg`, `--surface`, `--accent`) and lives in the dark on
purpose. The hue comes from the theme; the darkness is pinned here, and the
light colour is whichever of `paper`/`ink`/`wall-fg` the theme actually made
light — reading it off `paper` breaks every dark theme.

Read-only caveat: adding or removing a camera happens over socket.io, which
this proxy cannot upgrade. Those two need `cameras.home:5000` directly, and the
page says so.

## Repository layout

Two independent apps live here:

- **`local/`** — the main **Flask** app (Python 3.12). The home dashboard: a chat
  assistant ("Alfred"), a service launcher, a backup-history dashboard, and the
  shared **Files** browser. This is where almost all work happens.
- **`cloud/`** — a small separate **PHP** site (public-facing pages: home,
  contactos, login, privado). Unrelated to the Flask app except that both read a
  `users.json` with the same shape. Hostinger serves it at
  `house.home`; since 2026-08-14 `home-core-deploy` also runs it
  here in a container, because Pi-hole now answers that name with hub for
  anything on the LAN (see **The split horizon** below).
- **`proxy/`** — Caddy on hub's **80/443** in front of the two names the LAN
  resolves locally, plus the acme.sh that renews the `*.home`
  certificate it serves them with.

`cloud/users.json` is the shared user store; the Flask container mounts it
read-only as `/app/users.json`.

## Running & deploying `local/`

- Container entrypoint is `python app.py` (see `local/Dockerfile`). `app.py`'s
  `__main__` starts **two** servers on threads: the main app over **HTTPS on
  `PORT` (21001)** using `/certs/{cert,key}.pem`, and a plain-HTTP backup-ingest
  server on `BACKUP_PORT` (5020).
- Local run: `cd local && docker compose up --build`. Compose uses
  `network_mode: host` and mounts `../cloud/users.json`, `./history`, `./certs`,
  and `./backup_data`.
- **Deploy is a MANUAL Jenkins run** (`Jenkinsfile`, agent `hub`, job
  `home-core-deploy`): stage *Deploy local* does `docker compose down` then
  `docker compose up --build -d --remove-orphans --force-recreate` in `local/`,
  then *Deploy cloud* and *Deploy proxy* bring up the PHP site and the Caddy in
  front of both (see **The split horizon**). `SMB_PASSWORD` comes from the
  Jenkins credential `SHARE_SMB_PASSWORD`.
  **Pushing to `main` does not deploy anything.** Measured twice — 2026-07-29
  (7 min of 4-second polling after a push, no restart) and 2026-07-30 (10 min,
  same); both times the container only restarted once someone ran the job. The
  `Jenkinsfile` has no `triggers` block and the job config needs credentials to
  read. So never call a push "shipped": say it is merged and waiting.
- **`Jenkinsfile.share-samba`** (job `share-samba-password`, manual) pushes the
  `SHARE_SMB_PASSWORD` credential into Samba's passdb on storage, making
  the credential the single source of truth for a password that otherwise lives
  in two places and drifts. It writes on the storage agent (which runs as
  root, so no sudo) and verifies from the compute agent with `smbclient` —
  storage has none installed, and checking over the network exercises the
  path this app actually uses. **Run it after changing the credential, then
  re-run `home-core-deploy` / `deploy-alfred` / `restart-alfred`**, since all
  three bake the password into container env at deploy time. Drift here is what
  produced `STATUS_LOGON_FAILURE` on `\\storage.home\share` on 2026-07-30,
  with Samba configured perfectly and simply not accepting the string sent.
- **The container is fed by the Jenkins workspace**, not by any dev clone:
  `/home/homestack/jenkins-node/workspace/home-core-deploy/`. That is where the
  bind-mounted `local/backup_data/*.db`, `local/history/` and `local/certs/`
  actually live, so that is where to read `geo.db` or `tasks.db` on the host.
  Editing a clone elsewhere changes nothing that runs
  (`docker inspect local-web-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'`).
- **Verify a deploy landed by looking inside the container**, not by watching for
  a restart — a warm-cache rebuild and a workspace that never advanced both look
  like a ~10s outage from outside. Grep the running app for a symbol only the new
  code has, and check the count against the old commit:
  `docker exec local-web-1 grep -c <new_symbol> /app/app.py`.
- Python deps: `local/requirements.txt` (flask, requests, websocket-client,
  bcrypt, smbprotocol, pillow). There is **no test suite**.

## The split horizon

Inside the house, **`chat.home` and `house.home` are
hub**. Pi-hole holds an A record for each; off the LAN the public records
still point at the VPS and at Hostinger and none of this is involved. So each
of those two names means a different machine depending on where you are
standing, and that is worth keeping in mind before debugging either one.

What serves them here, all on hub, all from `home-core-deploy`:

| Port | What | Deployed by |
|---|---|---|
| 443 / 80 | `proxy/` — Caddy, TLS for both names | stage *Deploy proxy* |
| 8081 (loopback) | `cloud/` — the PHP site → `house.home` | stage *Deploy cloud* |
| 21001 | `local/` — the Flask app | stage *Deploy local* |
| 21003 (loopback) | home-chat's proxy → `chat.home` | **`home-chat-local-deploy`**, a different repo's job |

- **The certificate is meant to be a real `*.home` from Let's
  Encrypt**, issued by the acme.sh container over the Hostinger DNS API
  (`HOSTINGER_API_TOKEN` credential) — DNS-01, because nothing here is
  reachable from the internet for an HTTP-01 challenge. `proxy/ensure-cert.sh`
  runs first in the deploy stage: Caddy names the certificate files explicitly
  and **will not start with an empty `/certs`**. On renewal acme.sh restarts
  Caddy through the Docker socket, since Caddy only reads certificate files
  when it loads a config.
- **The variable is renamed on the way in, and must be.** The Jenkins
  credential is `HOSTINGER_API_TOKEN`; acme.sh's dnsapi plugin reads
  `HOSTINGER_Token` and nothing else, so `proxy/docker-compose.yml` maps one to
  the other. Get it wrong and acme.sh says *"You didn't specify a Hostinger API
  Key yet"* — which reads as a missing credential and sends you to Jenkins,
  where the credential is sitting there, correct. The `acme-sh` container on
  compute has the same bug: token under the ignored name, a CSR and an
  unfinished order, and no certificate it ever issued. Confirm a name against
  the image before trusting it:
  `grep -o 'HOSTINGER_[A-Za-z_]*' /acmebin/dnsapi/dns_hostinger.sh`.
- **The fallback also covers a token that fails**, not just a missing one. A
  present-but-not-working ACME path used to be worse than no path at all — the
  stage failed, so Caddy never started, so neither name was served. Now a failed
  `issue-cert.sh` drops through to `proxy/local-cert.sh` and says so loudly.
  That fallback is a CA generated on the box, a leaf under it for both names,
  same two filenames. While it is in use, in the order you will notice:
  - **The home-chat Android app refuses to connect over the LAN name.** Since
    Android 7 an app's WebView ignores user-installed CAs unless the app ships
    a `network_security_config` opting in. Nothing on this side fixes that —
    the app needs the real certificate, or its own config change.
  - Browsers warn until `proxy/certs/ca.pem` is installed as a trusted root.
  - Going back is one run of `proxy/issue-cert.sh` with the token set. It
    writes the same filenames in place, and `local-cert.sh` refuses to
    overwrite a certificate it did not issue, so the two cannot fight.
- **`chat.home` goes to a local copy of the home-chat proxy, not
  straight to `local/` on 21001.** The Android app is logged into that origin and
  talks to that proxy's endpoints; serving HomeCore directly there would break
  device enrolment and the signed-challenge login. The two copies must share
  `SESSION_SIGNING_KEY` and `devices.db` or a phone is logged out every time it
  crosses between wifi and mobile data.
- **An A record alone does not redirect the name.** Pi-hole's local records are
  one address family; it stays a forwarder for everything else, so a `AAAA`
  query for `house.home` was still answered from Hostinger
  (`2a02:4780:…`) — leaving a dual-stack client holding a LAN `A` and a public
  `AAAA`, which RFC 6724 tells it to prefer. The redirect would then be bypassed
  by exactly the devices with working IPv6. `split-horizon.sh` also writes
  `local=/<name>/` into Pi-hole's `misc.dnsmasq_lines`, which is dnsmasq for
  "answer this name from local data only, never forward it": the `A` keeps
  coming from the host record, `AAAA` becomes NODATA. The house has no global
  IPv6 today, so this is latent rather than broken — check it with
  `dig AAAA house.home @192.168.1.10` before believing it.
- **hub exempts itself from its own record.** `/etc/hosts` there pins
  `chat.home` to the VPS's real address, because `vps-tunnel.service`
  and home-chat's VPS deploy job both reach the VPS *by that name from this
  box*. Without the pin hub tunnels to itself and off-LAN access dies quietly.
  `proxy/split-horizon.sh` (run with sudo) sets up all of this and is
  idempotent — re-run it after the VPS changes address. `NAMES=` picks which
  names go local; a name left out of it is *removed* from the Pi-hole side, not
  merely left alone, so `NAMES=house.home` is how `chat` stays
  public while the certificate is a local one.
- **Do not check Pi-hole from hub with `dig @127.0.0.1`.** FTL there answers
  on `::1` and on `192.168.1.10` but times out on IPv4 loopback, so a healthy
  setup looks dead. hub resolves through Tailscale (`100.100.100.100`) and
  never uses its own Pi-hole anyway. Ask `@192.168.1.10` — that is what the
  LAN asks.

## Running & deploying `cloud/`

- `cd cloud && docker compose up --build` — php:8.3-apache on 8081, bound to
  loopback. `.htaccess` does the routing (front controller), which is why the
  image turns `AllowOverride` on rather than copying the rewrite into a vhost:
  Hostinger only lets us set `.htaccess`, and two copies of the routing rules
  would drift.
- `users.json` is bind-mounted from the workspace, not baked in, so
  `manage_users.py` takes effect without a rebuild. PHP sessions live in a
  named volume so a deploy does not log everyone out.

### Verifying changes without the container
`flask` and `smbclient` aren't installed on the dev host — they live only in the
image. To sanity-check:
- `python3 -m py_compile local/app.py` for the backend.
- Render templates with Jinja directly (stub `smbclient`/`flask` if importing
  `app.py`) rather than booting the server. Pure path/permission logic
  (`_norm_rel`, `_files_resolve`, `_shared_resolve`) can be exercised this way.

## Auth model

- Cookie-based Flask sessions. `session['user']` is a **numeric login ID string**
  (e.g. `'user1'`), not a name. Passwords are bcrypt-checked against
  `users.json` (`load_users`/`find_user`).
- Guard routes with `@login_required` (pages → redirect) or `@api_login_required`
  (APIs → 401 JSON). Both also enforce CSRF (see below) and are the *only* things
  that should ever branch on `session['user']` for authorization.
- `ADVANCED_USERS` gates "advanced"-tier service tiles on the home page.
- **`SECRET_KEY` and `DEBUG_API_KEY` have no insecure fallback** — `app.py` raises
  `RuntimeError` at import time if either is unset. Both must be real random
  values (`python -c "import secrets; print(secrets.token_hex(32))"`) supplied
  via the deployment's env (Jenkins credential), same pattern as `SMB_PASSWORD`.
  This is a breaking change from the old hardcoded defaults — **the container
  will fail to start** if these aren't set before deploying.
- **CSRF**: a synchronizer token (`session['csrf_token']`, injected into every
  template via a `context_processor` as `{{ csrf_token }}`) is required on
  POST/PUT/DELETE/PATCH for any request relying on the browser session cookie
  (checked inside `login_required`/`api_login_required` via `_csrf_ok()`).
  Exempt: proxy-authenticated requests (`g.is_proxy`, no ambient cookie to
  forge) and requests before a session exists (e.g. the login POST itself).
  Fetch/XHR calls send it as an `X-CSRF-Token` header, read from a
  `<meta name="csrf-token">` tag — see `chat.html`/`files.html`/`devices.html`.
  Any new template with a mutating fetch/XHR call needs both.
- **Login rate limiting**: in-memory sliding-window lockout (`_check_login_rate_limit`),
  keyed by both username and source IP — 5 failed attempts / 15 min locks that
  key out. Resets on process restart; this is a single-process app so that's fine.
- **Trusted devices** ("local-only per-device authorization", `/account/devices`):
  checking "Confiar en este dispositivo" at login issues a random per-device
  secret (hashed, stored in `local/backup_data/devices.db`) as a long-lived
  `device_token` cookie. `_device_auth` (a `before_request`, registered *after*
  `_proxy_auth` so proxy traffic is never second-guessed by it) auto-establishes
  `session['user']` from a valid device token — but only when
  `_is_local_request()` is also true (source IP in `LOCAL_NETWORK_CIDRS`, default
  covers RFC1918 + Tailscale's `100.64.0.0/10` + loopback). A stolen/copied
  device_token cookie is useless off the local network. `/logout` revokes the
  current device's token (deletes the DB row + clears the cookie) — trust must
  be re-established via the checkbox next login. Devices are individually
  listed/revocable at `/account/devices`, independent of the account password.

A third exemption: `Sec-Fetch-Site: same-origin`, **on the proxied mounts
only** (`_PROXIED_MOUNTS`). It is a forbidden header name, so page script
cannot forge it, and it is what lets an app served under a mount write
without being handed a token first. Scoped deliberately: in the shared gate
it would lift enforcement from every session-backed route here, and `/luces/`
serves a page that has no login of its own on this origin.

## Finance dashboard proxy (`/finanzas`)

`finance_helper`'s dashboard runs in its own container on `hub:8090` and
authenticates with the per-user proxy headers, not a HomeCore session. That used
to mean every member derived `sha256("<master>:<login id>")` by hand and pasted
it into the page once per browser. `finanzas_proxy` forwards `/finanzas/*` there
instead, injecting `X-Proxy-User` and `X-Proxy-Secret` (via `_proxy_user_token`,
which existed unused until now) from `session['user']`. The browser never sees a
token, access follows the session, and traffic rides this app's TLS rather than
plaintext `:8090`.

Three things that will bite if changed:

- **`/finanzas` redirects to `/finanzas/`, and that matters.** The dashboard
  resolves its API calls against the directory it was served from (`API_BASE` in
  `webui.html`). Served from the bare path, every call would resolve to this
  app's root instead.
- **The member comes from the session, never the request.** `finance_helper`
  enforces the same rule itself (`config.MEMBER_IDS`), so this is defence in
  depth — but do not "simplify" it by forwarding a `user` parameter.
- **CSRF is not exempted.** Proxying makes the dashboard same-origin with this
  app, so its POSTs hit `_csrf_ok()` like any other cookie-backed request.
  `/finanzas/_csrf` hands the page the token; exempting the route instead would
  leave `/finanzas/api/month/delete` open to a cross-site POST.

The `Finanzas` entry in `SERVICES` carries a `health_url` because its `url` is
now internal — `_check_service_url` returns OK unconditionally for paths, so
without it the tile would look healthy with the container down. Any future
proxied service needs the same.

**LAN/VPN only, and deliberately so.** The VPS proxy forwards exactly five
prefixes (`/chat* /tasks* /geo* /grocery* /menu*`, see home-chat's `AGENTS.md`)
and `/finanzas` is not among them, so `chat.home/finanzas/` returns
404. That is consistent rather than broken: the services grid is HomeCore's `/`,
which the VPS redirects to `/chat` instead of forwarding, and every other tile
points at a `*.home` host that is unreachable off-VPN anyway. Decided
2026-08-02 not to widen the allowlist. If the dashboard ever *must* work
off-VPN, move this route under one of the five prefixes rather than adding a
sixth — `webui.html` derives its API base from the path it was served from, so
the dashboard itself needs no change.

## Geo: tracking, sharing, and the hourly heartbeat

`/geo/api/track` runs in **two directions**, and they are not the same
permission:

- `{"user": …}` — **track** somebody else's phone. Admin-only: the subject did
  not ask for it.
- `{"share_with": …}` — **share** my own location with somebody for a while.
  Open to everyone, because it is consent by definition; refusing it to
  non-admins would mean a child cannot tell a parent where they are.

Both write the same `location_tracks` row — `target_user` is whose phone
reports, `requester` is who hears about it — so `_geo_track_update` narrates a
share to its recipient with the code that narrates a track to whoever asked.
The row is keyed on `target_user` alone, so the two cannot run at once: a share
replaces a track on the same phone and the tracker silently stops receiving.
Two watchers would need a row per (target, requester).

**The phone also reports once an hour on its own** (`LocationPingWorker` in the
app). Before that, a stored location existed only if somebody had run `locate`
or `track`, so "where is X?" fell back to saved places and geofence
crossings and had nothing to say about a day with no crossings. The heartbeat
uses a balanced-power fix that accepts a 15-minute-old cached one, so most
hours it costs nothing. It is a target, not a guarantee — Doze defers periodic
work, and a phone left alone overnight reports less often.

## Files feature (`/files`, biggest subsystem)

Backed by an **SMB share** via `smbprotocol` (`SMB_*` env vars;
`smbclient.ClientConfig` set once at import). The share moved from compute to
**`\\storage.home\share`** (its 7 TB disk) on 2026-07-30 — `SMB_HOST` is set
in the `Jenkinsfile`, because `docker-compose.yml` defaults it to `compute.home`
and a deploy without it silently keeps using the old host. Each member maps to a
top-level folder on the share:

- `FILES_FOLDERS`: login ID → folder (`user1`, `user2`, `user3`, `user4`). Note
  `user5` has a folder but no login.
- `FILES_ADMINS` (user1, user2) can browse the whole share; everyone else is scoped
  to their own folder.
- `FAMILY_FOLDER = 'familia'`: a common folder **every user can read AND write**.
- Permission core: `_files_resolve(username, rel_path)` returns a UNC path or
  `None`. It rejects `..`, and for non-admins allows only their own folder or
  `familia`. **All read/write endpoints must go through it** — never build a UNC
  from user input directly.

### Alfred's files live here too (`<carpeta>/alfred/`, `ALFRED_FOLDER`)

The chat's "My files" panel lists `<su-carpeta>/alfred/` on this share —
not the nanobot workspace, which is where it used to look. The workspace is a
directory inside that member's container: nothing else can see it, nothing
backs it up, and it goes away with the container. So everything Alfred
generates is filed on the share (`file-share`'s `save_text`/`upload_file`, and
the `document` skill's `FILE_SHARE_DOCS_SUBDIR`, now `alfred/documents`), and
the panel is that folder.

`/chat/download/<path>` has **two legs**, chosen by the first segment:

| Path | Served from |
|---|---|
| `media/…` | the workspace, via the nanobot API (`_workspace_media_path`) |
| anything else | this share (`_share_download_path` → `_files_resolve` then `_shared_resolve`) |

`media/` is only transient delivery now — a camera snapshot, a paperless
thumbnail, an image to show inline. Everything meant to last is a share path,
and the reader's access to it is exactly what the Files page gives them, plus
whatever was granted to them.

**This is what stopped the recurring dead link.** Before, `download:` reached
only `media/`, so handing over a file meant copying it there first; a link to
anything else rendered perfectly and 403'd when the person clicked it, and
Alfred never saw the failure — the reader did. Every save action now returns a
`download_link` to where the file actually is, so the link is never written
from memory. The 403 that remains names the refused path and where the file
should have gone, because it is read by the person who clicked and by Alfred
when the page is read back to him.

The panel lists that one folder and no higher: every row carries a delete
button, and `/chat/workspace/file` refuses anything not strictly below
`<carpeta>/alfred/`. Browsing the rest is what `/files` is for. A folder that
does not exist yet is an empty panel, not a 500 (`errno.ENOENT`); a share that
is down is still an error.

Serving rules, all in `_stream_share_file` — the one copy of this, after four
near-identical ones had started to drift:

- **Only `_INLINE_MIMES` render inline.** Not `image/*`: an **SVG is a
  document** that can carry `<script>`, and `familia/` is writable by the whole
  family, so an inline SVG is script running with the reader's session and CSRF
  token. Everything else downloads, and every response carries a
  `sandbox` CSP as well.
- **`?dl=1` forces an attachment.** The panel's 📥 uses it: an image served
  inline navigates the Android WebView away from the chat, because its download
  listener only fires on an attachment.
- **`FILES_HIDDEN` applies here too.** The APK folder is excluded from every
  listing, so naming it in a link must not reach it either.
- Filenames go in `filename*=UTF-8''`. Header values are latin-1, so
  "Informe anual — 2026.pdf" cannot go in the plain parameter — the three older
  copies still raised on it.

A **filename with brackets** is linked in markdown's angle form,
`[texto](<download:…>)`: every parser here ends a bare target at the first
`)`, so "Factura (1).pdf" produced a link to `…/Factura (1`. `INLINE_RE`,
`sanitizeHref` and nanobot's rescue regex all read both forms.

Tests: `local/test_alfred_files.py` (the access decision, no Flask),
`local/test_alfred_files_routes.py` (the routes end to end against a fake
share, run inside the app image — see its docstring for the `docker run`), and
`local/test_chat_links.js` (what the page makes of Alfred's markdown, run with
plain `node`; it lifts the regexes out of the served page rather than copying
them).

### Folder sizes (`/files/api/dirsize`)
A directory's `st_size` over SMB is the size of the entry, not of its contents,
so folders showed `—`. `_dir_size` walks the subtree; the listing does **not**
wait for it. `files.html` renders rows immediately and fills sizes in afterwards,
two requests at a time, cancelling the batch on navigation.

The walk is bounded (`_DIRSIZE_MAX_ENTRIES`, `_DIRSIZE_MAX_SECONDS`) and results
cached for `_DIRSIZE_TTL_S`. **`partial` is the important field**: it means the
walk stopped early or hit an unreadable subfolder, so the number is a floor and
the UI must show it as `≥`. The budget is checked *inside* the per-directory
loop, not only between directories — checking only between them meant one folder
with a million files was walked in full and still reported as complete.
`local/test_dirsize.py` covers this against a stubbed share; run it directly.

### Sharing (read-only grants)
- Metadata lives in a SQLite DB `local/backup_data/file_shares.db`
  (`init_shares_db`); files never move. A grant is `(owner, path, target)`.
- **`_grant_share(owner, rel, targets, notify=)` is the only place grants are
  created** — used by `/files/api/share`, by Alfred's `file-share` skill
  (`share_with`) and by Alfred→Alfred messages with an attachment. Alfred must
  share this way; copying a file into someone else's folder is not sharing.
- Recipients get **read-only** access via `_shared_resolve` (prefix-match against
  their grants) — used only by `/files/api/shared/list` and `.../shared/download`.
  Never wire a write path through it. Sharing is only allowed from inside the
  sharer's own folder.
- On share, the recipient gets an **ntfy** push (`send_ntfy` / `_ntfy_topic`,
  per-user topic `homecore-<folder>`). No-op unless `NTFY_BASE_URL` is set; topics
  overridable via `NTFY_TOPIC_<FOLDER>`.

### Frontend
`local/templates/files.html` — a single self-contained page (inline CSS + vanilla
JS, no build step). Current design is "The House": olive/plaster/honey palette, a
left "room rail" (personal / Familia / Para ti) driving one content area. Keep it
self-contained; the family may access it offline, so **avoid external font/CDN
dependencies**.

## Tareas (`/tasks`, chores + rewards)

Per-user task lists with a points ledger and prize catalog, aimed at the kids
(gamified, Spanish UI in `tareas.html`). Everything lives in the `# --- Tareas ---`
section of `app.py` and `local/backup_data/tasks.db` (`init_tasks_db`).

- **Roles**: admins = `ADVANCED_USERS` (user1, user2) create/edit tasks, recurring
  templates and prizes, review completions, fulfill redemptions. Everyone else
  can only complete/revert/excuse their own tasks and redeem prizes. Admin
  APIs use `tasks_admin_required`; enforcement is always server-side.
- **Who a task is for**: `assignee`/`assignees`/`user` in the tasks APIs accept
  a login id, a folder name (`user3`/`user1`/`user2`/`user4`), or a **friendly
  nickname** (Kai → `user4`/`kaito`/`ka`, and the words for mum and dad, …). All resolve
  through `_resolve_assignee` / `TASKS_NAME_ALIASES` — so the admin UI and Alfred
  can name people the way the family actually does. Add new nicknames in
  `TASKS_NAME_ALIASES`.
- **Admin create UI**: `tareas.html` has ONE "Nueva tarea" form with a
  "Tarea recurrente" toggle (default ON, since most family chores repeat).
  Toggling swaps between weekday-mask + rotate (→ `/templates`) and a single
  date (→ `/tasks`); the **"Para" multi-person selector is shared** by both
  modes (one-off create fans out to one task per selected person via the
  `assignees` list). The emoji field auto-suggests from the title (client-side
  `ICON_RULES`/`suggestIcon`) until the admin types their own, with a 🎲 button
  to re-suggest. A 🔔 "Recordatorios" toggle reveals *Hora de la tarea* /
  *Avisar antes* (`remind_before`) / *Recordar cada* / *Hasta*. Each row has a
  **Duplicar** button (clones into the form as a new item). Lists are sorted by
  `time_start` (ORDER BY in the templates + list endpoints) — no manual sort.
- **Nothing waits forever** (`_tasks_auto_resolve`, every worker tick). A task
  in `review` for `TASKS_AUTO_APPROVE_DAYS` (2) **approves itself and grants
  the points** — the kid did the chore and was waiting on an adult's inbox,
  which is the system punishing them for somebody else. A `pending` task
  `TASKS_AUTO_EXCUSE_DAYS` (2) past its due date **closes itself as
  `excused`**; reminders had already stopped at the end of its day, so it was
  only accumulating as a reproach. Both sweeps are idempotent and the ledger's
  partial unique index on `(ref_type, ref_id)` is what actually stops a double
  grant — the sweep runs every five minutes. Auto-approvals are stamped
  `reviewed_by = 'sistema'` and auto-excuses carry `TASKS_AUTO_EXCUSE_NOTE`, so
  neither is ever mistaken for a person's decision. The kid is told (they
  earned points); the adults are not, because a 07:00 push about a chore they
  did not look at is noise — that lands in the weekly report instead.
- **Weekly report** (`_tasks_send_weekly_report`): Sunday at/after
  `TASKS_NOTIFY_HOUR`, to `ADVANCED_USERS` only, covering the seven days ending
  **yesterday** so it is about the week that finished. Per person: approved,
  points, how many approved *by themselves*, excused vs timed out, still
  pending, still waiting review. Numbers are computed in Python and the prompt
  tells Alfred not to change them — a report whose figures drift is worse than
  none. Once per day via `tasks_meta.last_weekly_report`, stamped *before*
  delivery like `last_reminder_at`, since each send is a slow Alfred turn.
  Skipped entirely when the week had no tasks. Pinned by
  `local/test_tasks_auto.py`.
- **State machine**: `pending → review → approved` (approve grants points);
  `pending → excused` (kid explains why they couldn't — stops reminders, no
  points); revert/reject go back to `pending`. `approved` is terminal —
  corrections via `/tasks/api/adjust` ledger entries.
- **Points**: append-only `points_ledger`, balance = `SUM(delta)`. A partial
  unique index on `(ref_type, ref_id)` makes double-granting impossible.
- **Recurring tasks**: `task_templates` (weekday mask, multi-assignee,
  optional rotation) are materialized lazily by list endpoints and by
  `_tasks_daily_worker` — both funnel into the idempotent
  `_materialize_tasks` (UNIQUE(template_id, due_date, assignee) is the real
  guard). Rotation is stateless: occurrence-count since `anchor_date`.
- **Times & reminders**: `time_start` is the task's own time (HH:MM),
  `time_end` an optional window end. `remind_before` (minutes, admin-set) makes
  the daily worker start nagging that many minutes *before* `time_start`
  (0 = at the hour); `remind_every` is the repeat interval (default
  `TASKS_REMIND_MINUTES`, 30). It nags the assignee until the task
  leaves `pending`, the window/day ends, or the kid postpones
  (`/tasks/api/postpone` → `snoozed_until`). Delivery goes **through the
  user's Alfred**: `_alfred_notify` posts the reminder context to their
  nanobot on the `homeweb:<user>` chat session, appends the reply to the chat
  history, and pushes it via ntfy when the app isn't open (plain ntfy if
  nanobot is down). `last_reminder_at` is stamped *before* the slow Alfred
  call so overlapping ticks can't double-send. All date math uses `TASKS_TZ`
  (Etc/UTC) — the container clock is UTC; `tzdata` is in
  requirements for that. Schema evolutions are patched in `init_tasks_db`
  via `PRAGMA table_info` + `ALTER TABLE` (the DB pre-exists in the volume).
- **ntfy delivery**: task events (reminder, new task, completed→admins,
  approved/rejected→kid, redemptions) all push via `send_ntfy`/`_notify_user`
  to the **same server Alfred uses** — `NTFY_BASE_URL=https://ntfy.home`,
  per-user topic = `folder.capitalize()` (`Alex`/`Robin`/`Sam`/`Kai`, matching
  nanobot's `ntfy-send`). Publishing needs **basic auth**: set
  `NTFY_CREDENTIALS` ("user:pass", same Jenkins credential as Alfred);
  `send_ntfy` uses it as HTTP basic auth (bearer `NTFY_TOKEN` is only a
  fallback). Reminders push only when the user isn't looking at the chat
  (`_user_watching`); the other events always push.
- **Tap target**: every task notification sets an ntfy `Click` header (via
  `_notify_user`/`_notify_tasks_admins`, defaulting to `chat_link()`) so tapping
  it **opens the user's Alfred chat**. Actionable events also pass
  `click=chat_link('<message>')` — that lands the user in `/chat` with a
  task-referencing message pre-staged in the input (chat.html reads
  `?prefill=`; it never auto-sends). Base URL is `HOMECORE_PUBLIC_URL` (default
  the Tailscale URL; point at the cloud proxy for off-VPN taps).
- **Alfred integration**: the nanobot `tasks` skill (in the nanobot repo)
  calls `/tasks/api/*` with proxy-auth headers. `_proxy_auth` accepts, besides
  the master `PROXY_SHARED_SECRET`, a **per-user derived token**
  `sha256("<master>:<user_id>")` valid only for that `X-Proxy-User` — each
  nanobot container gets only its own token so a prompt-injected instance
  can't impersonate another user.
- **Going the other way**, every call HomeCore makes to a nanobot must carry
  `nanobot_auth_headers(nanobot_id)` (the per-instance `NANOBOT_API_SECRET_USERn`,
  `{}` when unset). `/chat/download/media/<path>` was sending nothing, which is
  why nanobot's `/v1/workspace/*` had to stay open to keep photos and downloads
  working. (`/chat/workspace` and `/chat/workspace/file` no longer call a
  nanobot at all — they read the share; see *Alfred's files live here too*.)
  HomeCore sends the credential first and nanobot enforces after — same order as
  the chat API in 92737bc, so the family never loses a working feature to a
  deploy that lands in the wrong sequence.

## Minuta (`/menu`, weekly lunch & dinner menu)

What's for lunch and dinner each day, in `backup_data/menu.db` (`init_menu_db`).
Same social contract as the grocery list — everyone reads, only `ADVANCED_USERS`
(Alex/Sam) decide, the kids ask — and the same "a non-admin's write becomes a
request" conversion in `menu_api_set`, so a kid saying "pon pizza el viernes" is
heard as asking rather than refused. `clear`/`approve`/`reject` are hard 403s.

- **Weeks are keyed by their Monday** (`_menu_week_start`/`_menu_valid_week`), so
  the plan is real dates: you can fill in next week without erasing this one, and
  what was cooked stays as history — which is where the "se cocina seguido"
  suggestions come from. `?week=` accepts *any* date in the target week.
- **A request may be loose.** `day`/`meal` are nullable on `status='requested'`
  ("pizza esta semana"); a parent supplies the slot when approving. They are NOT
  nullable on `status='planned'`, and a partial `UNIQUE INDEX ... WHERE
  status='planned'` is what actually guarantees one dish per slot — planning over
  an occupied slot deletes first, so it replaces rather than duplicating.
- `_menu_parse_day` takes an ISO date, a Spanish weekday (resolved *inside the
  week being edited*, so "viernes" means that week's Friday, not the next one)
  or today/tomorrow; `_menu_parse_meal` takes the words people actually say, in
  either language. Alfred passes user phrasing straight through.
- ntfy: a new request notifies the admins, approve/reject notifies the requester,
  both tapping through to `chat_link` + `?panel=menu`.
- UI mirrors the grocery list: `/menu` standalone page (`menu.html`) and the
  Minuta panel in `chat.html`. The nanobot side is the `menu` skill.

## Geo — places, location reminders, on-demand location (`/geo/api/*`)

Household-shared named **places** + **geofence reminders** ("remind me X when I
get home", "tell me when Kai gets to school"). Arrival detection is
**on-device** — the Android app registers OS geofences and POSTs `/geo/api/event`;
HomeCore delivers the fired reminder through the target's Alfred (`_geo_deliver`) +
ntfy. Store: SQLite `backup_data/geo.db` (`init_geo_db`: `places`,
`geofence_reminders`, `user_locations`, `location_tracks`). Cross-user
reminders/queries are admin-only (Alex/Sam = `ADVANCED_USERS`).

**Believing a report (`_geo_event_rejection`).** The phone is the only witness to
a crossing, and it is not always right: on 2026-07-30 it reported "Kai left
del colegio" at 06:43 while she had been home all night and did not reach school
until 07:38. Five checks guard this. The app sends **no `lat`/`lng`** with a
geofence event — log lines read `(fix none)` — so the accuracy gate and every
other geometric test sit behind `if not fix: return None` and never execute on a
report as it arrives. The check that does run on every event is the **state
check**: an `exit` from a place `user_whereabouts` does not have the person
inside is refused. It works only because it is placed *before* that early
return — **that ordering is load-bearing, do not move it.**

**The second opinion (`_geo_confirm_transition`).** Rather than wait for an APK
that sends a fix, HomeCore asks for one: a `location_request` push — the same one
`locate` uses, already handled by the shipped app — answered by
`POST /geo/api/location`, and *then* the geometric checks have something to run
on. Three rules shape it:

- **Only when it matters.** `_geo_event_worth_confirming` gates it on the report
  being about to tell somebody something: not a repeat, an active reminder
  matching it, out of cooldown, and no usable fix already attached. Everything
  else commits inline and never wakes the GPS.
- **It can only refute.** No answer, an answer too vague to mean anything (worse
  than `GEO_FIX_MAX_ACCURACY_M` or with no `acc` at all), or an exception →
  the report stands. A notification that wrongly arrives gets reported; one that
  wrongly never arrives does not.
- **Nothing is written until it answers.** The whereabouts row and the one-shot
  reminder are committed by `_geo_event_commit`, which the confirmation path
  calls only after the GPS agrees — a refuted arrival must not leave "is at the
  colegio" behind for `where_is` to repeat.

It runs on a daemon thread and the endpoint answers `{"ok":true,"fired":0,
"confirming":true}` immediately — `fired` is unknown at that point, not zero; the
`geo: confirm agrees|REFUTES` log line is the real outcome. Pinned by
`local/test_geo_confirm.py`.

**Every drop is logged.** A rejected event still answers `200 {"ok":true,
"ignored": ...}`, so the log is the only trace: `docker logs local-web-1 | grep
"geo:"` shows rejections, repeat-guard suppressions, cooldowns, accepted-but-
unmatched, and one line per fire naming target → notify. Note `app.logger` needs
its level set (see `_LOG_LEVEL` near the top of `app.py`) — Flask leaves it at
NOTSET, which silently discards every `.info()`.

**Movement alerts are collapsed before they are sent** (`_geo_hold_or_deliver`).
On 2026-08-16 one afternoon produced four alerts about Robin (arrived 12:00,
arrived 12:12, left 12:14, arrived 12:22) plus Sam leaving home at 12:17 and
arriving at 12:18 — a minute apart, i.e. somebody walking to the car. So an
alert about *somebody else's* movement is held `GEO_COLLAPSE_S` (5 min) first,
and while it waits a newer crossing for that person **replaces** it (only the
latest state is still true) and the opposite crossing **cancels** it and
itself. `GEO_ROUNDTRIP_S` cannot usefully exceed the hold — the cancel only
fires while both halves are pending, so the effective window is the smaller of
the two. `GEO_MAX_HOLD_S` (15 min) stops somebody crossing a fence every four
minutes from deferring their own alert forever.

**A reminder the user set for themselves is never held** (`notify == target`).
"Comprar pan cuando llegue a casa" five minutes late is five minutes after they
left the shop — that is content, not news, and the split is the whole design.
Pinned by `local/test_geo_collapse.py`.

**Delivery is off the request thread** (`_geo_deliver_all`, a daemon thread).
`_geo_deliver` calls `_alfred_notify`, a blocking LLM round-trip; inline it held
the phone's POST open for 13s, and a geofence receiver that times out gets
retried by Android, arriving as a second crossing.

`_geo_notify_sync`/`_geo_push_control` send **silent** ntfy control messages the
app intercepts (never shown): `geofence_sync` (reload fences), `location_request`
(one fresh fix), `location_track` (JSON `{until,interval}`) / `location_track_stop`,
and `ring_phone` (JSON `{seconds,by}`) / `ring_phone_stop`.

**Find my phone** — `POST /geo/api/ring` (self, or another person if admin, same
rule as locate/track) makes the phone ring *through* silent mode: the app plays
it on the **alarm** stream, which is the one silent doesn't mute and DND lets
through. Capped at `GEO_RING_MAX_S`, stops itself, and the phone always shows
who asked plus a Detener button — a phone you can't silence is worse than a
phone you can't find. Alfred reaches it via the `geo` skill's `ring_phone`.
A daily `_geo_sync_worker` re-pushes `geofence_sync` (catches pushes missed while a
phone was offline).

On-demand location: `POST /geo/api/locate` (fresh fix — long-polls ~12s on a
per-user `threading.Event` woken by `POST /geo/api/location`), `POST /geo/api/track`
(+ `/track/stop`, `GET /geo/api/track` status) for a bounded window, `GET
/geo/api/location[/<user>]` (last known, enriched with the saved place it's inside).
GPS on the target device runs ONLY while a request is active. Adults are told when
watched (`_geo_notify_watched`); kids silently, per the family model.

**Maps (`GET /chat/map?lat=&lng=&label=[&acc=|&radius=][&zoom=]`, `@login_required`)**
— a location answer with a picture. HomeCore fetches the OSM tiles **server-side**,
composites them with a marker and returns `image/png`; the browser never talks to
a tile provider, so the family's coordinates leave the network only from this
container, only for tiles it doesn't already have. Tiles cache on the mounted
volume (`backup_data/tiles/`, oldest-first eviction at `MAP_CACHE_MAX_TILES`), so
after the first few maps the outbound calls stop. It sits under `/chat` because
that's the one prefix the cloud proxy forwards — same relative URL works on the
LAN, over the tailnet and off-VPN, always same-origin https. `acc`/`radius` draw
the uncertainty circle and, absent an explicit `zoom`, pick one that keeps it in
frame (a 2 km cell-tower fix must not be drawn at street zoom). Needs **Pillow**;
if it's missing the endpoint 503s and nothing else breaks. Provider is swappable
via `MAP_TILE_URL` / `MAP_TILE_USER_AGENT`. Alfred embeds it as
`![map](/chat/map?…)` — `sanitizeHref` resolves the relative URL against the page
origin, so `chat.html` renders it inline with the existing lightbox, unchanged.
The geo skill hands Alfred a ready-made `map` string on `get_location`/`locate`/
`track_status`; **`where_is` deliberately has none** — it stores a place *name*,
not coordinates, so a pin would show the centre of the colegio as if it were the
person.

**Native-app auth (important)**: the app POSTs `/geo/api/event`, `/geo/api/location`
and `/tasks/api/notify-action` from background receivers with only the session
cookie — no browser CSRF token — so those use **`@_geo_native_auth`** (session
required, CSRF-exempt), NOT `@api_login_required`. Any new native-app POST endpoint
must do the same or it will silently 403.

## Consumo de Alfred (`usage.db`, `/stats`)

What every turn cost and what kind of turn it was. nanobot reports one record
per completed run (`agent/usage_report.py`, fire-and-forget, never blocks a
turn); HomeCore stores it and is the only place the numbers are interpreted.

It exists because model choice was guesswork wearing a lab coat. Replacing
`deepseek-v4-flash` on 2026-08-16 meant firing synthetic prompts at seven
candidates — a proxy for the house's traffic, not the traffic — and the number
that actually decided it (a large share of input arrives cached, so `cache_read`
weighs far more than the headline input price suggests) came from one log line
somebody had pasted into a config comment. The 98% that figure was first written
as is wrong: measured over 1,415 real runs it is 62%, and notification triage
(34%) and chores (40%) are worse.

**The session key is the whole feature.** nanobot already encodes what kind of
turn it is (`ev-notif`, `ev-task`, `ev-geo`, `ev-ask-*`, `fin`/`dev`/`edu`/`dsg`,
`dlg-*`, a bare conversation id for ordinary chat, `whatsapp:*`), so the
breakdown only needs naming — `_usage_scope` + `_usage_label`, reading
professions off `CHAT_SCOPE_SPACES` so there is one roster rather than two.

`USAGE_RATES` is a **snapshot** of models.dev (`opencode`) with the refresh
command in the comment. Deliberately not fetched live: the page must not depend
on reaching the internet, and silently re-valuing last month's history when a
price moves is worse than being openly a little stale. Uncached input is billed
at `input` and the rest at `cache_read` — those differ ~30× on one model, and
billing cached tokens at the input rate inverts every conclusion the page
exists to support. `/stats/api/summary` also reports each trailing window
against `assistant.spend_budget` (`USAGE_BUDGET_5H` / `_WEEK` / `_MONTH`), so
the question is "how close are we" rather than "how much did we spend" -- and
unset is the default, which reports `cap` and `pct` as null rather than zero.
Those used to be the flat Go plan's ceilings ($12/5h, $30/week, $60/month);
per-token billing publishes no allowance to draw against.

Admin-only (`ADVANCED_USERS`) at both the page and the API: this is the whole
house's traffic, and how much each person talks to Alfred is not something the
house needs to publish to itself. Pinned by `local/test_usage_stats.py`, and
the `/stats` prefix is routed in home-chat's proxy — without it the tile works
on the LAN and is a dead link in the app (see `test_app_tiles_reachable.py`,
which caught exactly that here).

## WhatsApp (`whatsapp.db`, `/chat/whatsapp/*`)

The relay above sees a **notification**; this sees the **conversation**.
nanobot's WhatsApp channel is linked to one person's account through the Baileys
bridge and POSTs every message here as it arrives (proxy auth, like the other
nanobot→HomeCore calls). Store: `backup_data/whatsapp.db` (`init_wa_db`:
`wa_chats`, `wa_log`).

Same division as the notification relay, for the same reason: **the channel is
transport, HomeCore is the store and the permission gate.** nanobot decides
nothing about what it may answer — `POST /chat/whatsapp/message` returns
`can_read` and `reply_mode` on every single message, read from the DB.

**The two flags are deliberately asymmetric.** `can_read` defaults **on**:
linking the account is the consent, and the point of the link is that "how much
was the bill Jana sent?" has an answer — defaulting it off would mean
muting a hundred chats before anything worked. Muting one stops it being stored
at all (older messages stay; muting is not deleting). `reply_mode` defaults
**`off`** for every chat and always will: `off` / `ask` / `auto`, and it is
re-read here on every message rather than trusted from a prompt.

**`ask` needs a way to say yes**, or it is just a slower `off` — which is what
it was until `POST /chat/whatsapp/approve` existed. The draft is delivered to
the user through their own Alfred; if they say to send it, Alfred approves that
chat and then sends. The approval is **one message**, expires in
`WA_APPROVAL_WINDOW_S`, and is consumed by the send in the same connection that
reads it. `off` cannot be approved at all — a setting whose meaning is "no way
around this" must not have one. `approve_reply` is deliberately absent from
nanobot's `_ASK_READABLE_ACTIONS`, so a turn driven by an incoming WhatsApp
message can never approve its own reply.

Endpoints: `POST /chat/whatsapp/message` (ingest), `GET /chat/whatsapp/recent`
(`?chat=` for one conversation), `GET /chat/whatsapp/search` (body, sender and
chat name matched together — in a group the handle people remember is the
group's name, in a one-to-one the person's), `GET|POST /chat/whatsapp/chats`.

Two things that bit and are now pinned by `local/test_whatsapp_store.py`:
`ORDER BY ts DESC` alone returns a same-second burst oldest-first under a
heading that says newest, so every ordering carries `, id DESC`; and the dedupe
index on `(username, wa_id)` is **partial** (`WHERE wa_id != ''`), because
Baileys replays on reconnect but two messages it could not identify must not
collide with each other.

**Configured from the phone** via the `#wa-panel` panel in `chat.html`, reached
from the apps menu (`data-app="wa"`). The Alfred Android app is a WebView around
this page plus native helpers — there is no native settings screen — so this
ships with a HomeCore deploy and needs no APK. It is deliberately the same
furniture as `#notif-panel` beside it, because it asks the same two questions
about a different source: what may Alfred see, and what may he say. One row per
conversation, a `leer` checkbox, and three buttons for `reply_mode` — a
checkbox has nowhere to put `preguntar`, which is the state that makes the
feature usable. Pinned by `local/test_whatsapp_panel.py`, which checks the ends
meet: a `data-app` nothing dispatches, a panel id the JS never looks up, and a
fetch to a route that does not exist all fail silently.

Alfred reads it through the `whatsapp` skill (`search_messages`,
`list_messages`, `list_chats`) — read-only, and deliberately **not** in
nanobot's `_ASK_READABLE_ACTIONS`: those are shared household things, and a
stranger in a WhatsApp group asking what Sam wrote in the family group is
exactly what that allowlist is for.

## Profesiones — the chat spaces (`CHAT_SPACES`)

A **space** is a dedicated chat context sharing nothing with the normal
conversation: not its history, not its model session, not its standing
instruction. Finanzas was the first; the same shape turned out to be exactly
what a *profession* needs, so there are four, listed in the apps menu under
**Profesiones**:

| space | URL | scope | persona file |
|---|---|---|---|
| `finanzas` | `/chat/finanzas` | `fin` | `local/personas/finanzas.md` |
| `programador` | `/chat/programador` | `dev` | `local/personas/programador.md` |
| `profesor` | `/chat/profesor` | `edu` | `local/personas/profesor.md` |
| `disenador` | `/chat/disenador` | `dsg` | `local/personas/disenador.md` |

Adding one is a `CHAT_SPACES` entry plus a persona file — the route, the
history directory, the session id, the menu entry and the reply routing are all
generated from the dict. Two fields are **permanent** once shipped:

- **`scope`** is the 4th `chat_id` segment. It is written into stored chat_ids
  and read back by `/chat/agent-event`, so changing one orphans every reply
  still in flight. It must never be all digits (that is a conversation start)
  and never collide with an `ev-*` machine scope.
- **`url`** ends up in bookmarks and must stay ASCII — hence `disenador`, while
  the title says Designer.

Routes are registered as **literal rules** by `_register_space_routes()`, not as
`/chat/<space>`. A converter rule would sit alongside two dozen literal
`/chat/*` endpoints, and the day a space collides with one the collision is
silent — the wrong view answers.

Flask does not catch that for you: `add_url_rule` only asserts on a duplicate
*endpoint name*, and these are `chat_space_<key>`, which cannot collide. Two
rules for the same path with different endpoints both register and the first
one wins — and `_register_space_routes()` runs before most of the literal
`/chat/*` views, so the space would be the winner. The check is therefore
written by hand: **`_assert_no_space_route_collisions()`**, called at the bottom
of `app.py` once every route exists. A `url` that shadows another view fails
startup instead of quietly answering for it.

### The prompt each turn carries

`_space_block(space, username)` is prepended to **every** turn, not just the
first: a session past the context window is consolidated, and an instruction
living only in message one is exactly what a summary drops. It composes two
layers, in this order:

1. **The house default** — `local/personas/<space>.md`, shipped with the code,
   mtime-cached. These are prompts, not documentation: a wording change is a
   behaviour change. Roughly 1k tokens each against a 65k window (measured:
   programador 1228, profesor 1018, disenador 946, finanzas 258 — run
   `test_token_budget.py` rather than guessing). A file that cannot be read is
   logged at `error`: the profession would otherwise run as a bare
   `[Contexto: X]` header, and Finanzas without its text is Alfred answering
   from memory again.
2. **The person's own additions** — `backup_data/personas.db`, keyed
   `(username, space)`, edited from the ⚙ panel in that profession's header
   (`GET`/`PUT /chat/persona?space=…`), capped at `PERSONA_MAX_CHARS` and run
   through `_strip_standing_markers` on the way in *and* out — it is user text
   that ends up **inside** the standing-context region, so an unpaired
   `[[[/standing-context]]]` in it would close the region early and hand
   everything after it to nanobot as a user turn to store on every turn.

**The order is load-bearing.** The override is appended *below* the default and
framed as preferences that refine but never cancel it. An override that could
precede and contradict the default would let "olvida lo de consultar la base"
turn Finanzas back into a chat where Alfred answers from memory. It lives in a
table rather than a per-user file because it is user input: a `(user, space)`
key cannot be steered into a path, and `backup_data` is a bind mount, so a
deploy never wipes what someone wrote.

### The blocks are sent every turn and stored on none

Both blocks are wrapped in **standing-context markers** (`STANDING_OPEN` /
`STANDING_CLOSE`, matched by `nanobot/utils/standing_context.py`). nanobot shows
the model the text on every turn, markers stripped, and persists the message
*without* it.

That split is not a micro-optimisation. The user turn is stored verbatim and
replayed in every later prompt, so a 1 200-token persona re-sent for thirty
turns was thirty copies — 36 840 tokens, over half the 65k window, all
identical. It was also self-defeating: the duplication is what drove the session
into the consolidation that re-sending exists to survive. `local/test_token_budget.py`
prints the growth table and is the check that it stays flat.

The two services agree on the marker literals by convention, not by a shared
import — they deploy separately, and a constant one has to import from the other
is a dependency neither wants. If they ever disagree the failure is visible and
harmless: the markers show up in the chat. HomeCore also strips the markers out
of the user's own text before wrapping, so nobody can hide their words from
their own history.

nanobot also remembers the newest block per session, so the turns HomeCore never
sends one for — a background task announcing its result — are still answered in
that profession's voice rather than the ordinary Alfred's.

### The normal chat points at them

Outside a space, `_professions_hint_block()` goes on the turn instead: the list
of professions and an instruction to offer the fitting one **once**, as a
`:::goto` block the page renders as a button. It is spelled out there that
offering is never refusing — Alfred answers and does the work in the same turn,
and stops offering once the user has carried on regardless. An assistant that
replies "eso es en Finanzas" and stops has invented a rule nobody agreed to.

### A job set inside a profession comes back to it

nanobot's `retarget_for_delivery` rewrites a cron job's stored chat_id at fire
time — the day, because a job created in July kept posting to July, and the
conversation, because joining one from weeks ago is a lie. A **space scope is
kept**, though: a space has no conversations to join, and the scope is what
routes the reply back into it. Without that, a reminder someone set while
talking to the Profesor would be delivered into the ordinary chat and answered
by the normal Alfred — without the persona that promised to schedule it.

Told apart by shape, not by a list: all-digits is a conversation, `ev-*` is a
machine-event session (both replaced), anything else is a space scope (kept).
nanobot should not have to track HomeCore's `CHAT_SPACES`.

### One profession asking another (`POST /chat/api/delegate`)

The Teacher needs a plot and the Designer is the one who can draw it. Rather
than teach every persona every other persona's craft, a profession writes a
brief and hands it over: `{"to": "disenador", "brief": "...", "from": "profesor"}`.

The delegate runs as an **ordinary turn** — same instance, same user, all its
skills — with the *target's* standing block and a session of its own
(`…:dlg-<scope>`), so the Designer's working notes never land in the Teacher's
history and the Profesor's context never steers the design. Deliberately not a
subagent: a subagent gets a generic prompt and cannot invoke the JSON skills, so
it could not call `document`, which is the whole point of asking.

It answers inline when the delegate finishes within `DELEGATE_WAIT_S` (45 s —
the exec tool that invokes the skill dies at 60, so waiting longer would kill
the *caller*). A longer piece keeps running and is delivered into the caller's
conversation the same way a background answer is, so nobody is left holding a
promise. `_delegations_inflight` allows one per person at a time, which is also
what stops a delegate delegating back: the second call gets a 409.

The `dlg-` scope is not in `CHAT_SPACES` on purpose — a stray relayed message
from that turn stays unstamped instead of being filed into a profession's
history the person never opened.

### Professions run on the stronger model

`/chat/send` sends `profile: <space>` and `powerful: true` whenever `space` is
set. The profile is the profession's **name**, not a model: nanobot resolves it
through `modelProfiles`, so which model a profession runs on is a config line
over there rather than a HomeCore deploy. Delegated turns pass the *target's*
profile, so a design brief handed to the Designer gets the Designer's model. nanobot decides *which* model that is (`modelPowerful` in
`config/config.json`, currently `deepseek-v4-pro`); HomeCore only says the
turn is worth it. The normal chat stays on the fast model — the family asks
about the weather far more often than it asks for an architecture review — and
a deployment with nothing configured degrades to the main model rather than
failing.

### Design goes through `html`, not `pdf`

The Designer used to route posters, invitations and CVs to `pdf`. That renderer
can express heading, text, bullets, questions, table and image — and nothing
else: no colour, position, columns or typography. So every visual piece came out
as a text document with a chart in the middle, which reads as "the model is a
bad designer" when the model never had a say. Visual pieces now go to `html`,
where the persona writes the markup itself and controls everything; `pdf` keeps
what genuinely is text (reports, guides, letters). `pptx` has the same ceiling and
the same rule.

### Each profession has its own paper

`<body data-space="…">` plus four `body[data-space=…] #messages` rules give each
one a distinct background: a ledger grid, a dot grid, notebook rules, a diagonal
weave. Inline SVG data-URIs of a few hundred bytes — nothing is fetched — drawn
in the existing palette at the edge of visible, because the text on top has to
stay exactly as readable as it is in the normal chat.

## Which nanobot session a turn runs in

`homeweb:<user>:<day>:<conv>` is the user's conversation (see "Per-day chat"
below for how `<conv>` is assigned). `_alfred_notify(..., scope=)` instead
appends an event-type segment (`homeweb:<user>:<day>:ev-notif` / `ev-geo` /
`ev-task`) to run machine-generated turns **outside** it.

**Machine events must pass a scope.** Until 2026-07-31 they did not, and every
relayed phone notification, geofence crossing and task reminder was injected
into the same session as the chat. One day (2026-07-30, Alex) carried **274
`[HomeCore system]` injections** — 187 WhatsApp, 288 geofence crossings, 35
reminders — against 348 real messages: ~900 turns of context for a 348-message
conversation. Alfred answered every question with a context dominated by text
the user never sent, and at ~1 MB against a 65k-token window autocompaction kept
summarising the actual conversation away while the noise kept arriving. It
presents as "Alfred mixes up conversations", and it is invisible in the UI
because the *visible* history was always correct — the pollution is only in
nanobot's context.

Scope is per event **type**, not per event. Per-event sessions isolate a shade
better, but nanobot never prunes session files and this house makes ~270
notifications a day. Keeping machine traffic out of the human conversation is
the whole goal, and a per-type session does that completely.

A silent verdict now costs the conversation nothing. When Alfred does speak, the
reply still reaches the visible history through `persist`, so the user sees it;
it is simply not smeared through his context. For detail he can call the
`notifications` skill's `list_notifications` — retrieval beats carrying every
event forever.

Deliberately still on the conversation session: `/chat/voice` answers and
family DMs. Those are things a person actually said.

**`/chat/agent-event` accepts 3 or 4 segments** for this reason. It used to
require exactly 3, which would have 403'd every background reply an event turn
produced. The security property is unchanged: `parts[1]` must equal the
authenticated user, so a request body still cannot pick whose history it writes.

## The queue, stopping, and branches (`_queues`, `_turn_cancel`, `_fork_start`)

Alfred answers **one thing at a time per conversation** — nanobot serialises
every turn sharing a `chat_id`, because they are one model session — so the box
used to lock while he worked. It queues now, and **the queue is the server's**
(`_queues`, keyed by `chat_id`, capped at `QUEUE_MAX_PER_CONV`). Same reasoning
as `_turns`: a phone locking, an app backgrounded and a profession switch are all
page loads, and a queue that dies with the tab loses exactly the commands
somebody walked away from. `_queue_advance` runs at the end of **every** turn,
from that turn's own worker thread, so the list drains with nobody watching.

- `POST /chat/queue` adds one — and starts it when the conversation turns out to
  be free, because the page decides between "send" and "queue" from a view that
  is up to a second old. `GET` reads the queue and what is running.
- `POST /chat/queue/cancel` takes one back before it ever reaches Alfred.
- `POST /chat/turn/<id>/cancel` stops the turn in progress. **What it had
  already written is kept**, filed with `interrupted: true` and drawn with a
  mark: half a paragraph of real work should not vanish because somebody wanted
  the rest sooner. Anything queued behind it still runs — this stops one answer,
  not the list.
- `POST /chat/queue/replace` is both halves in one call: to the front of the
  queue, then cancel the running turn, whose own ending starts it. A cancel that
  left the replacement at the back would not be a replacement.

**Cancelling takes two actions and needs both.** `_turn_cancel` POSTs nanobot's
`/v1/stop` *and* closes the response. Closing alone only lands when nanobot next
writes, which a turn thinking inside a tool call does not; telling it alone
leaves this side parked on `iter_lines`. The turn is marked `cancelled` first, so
the worker — which wakes up inside an exception — files it as interrupted rather
than blaming the network for what the person asked for.

**A branch is a conversation, not a copy.** «Fork» on a queued command runs it in
a new `conv` starting from where this one is (`_fork_start`): its own sidebar
entry, its own model session, and two facts on its first message — `branch_of`
and `branch_at`. Nothing is duplicated into the day file (that would double every
message, and `append_user_history`'s dedup would swallow half of them anyway);
`_session_read` splices the parent's messages back when the branch is opened,
marked `from_parent`. The page draws a ‹ 1/2 › switcher on the last message the
two readings share. The *model* is a different matter — a new session remembers
nothing — so the branch's first turn carries the parent transcript as ordinary
text (`_fork_seed` → `_compose_turn_content(seed=)`), not as standing context,
which is re-sent every turn and stored on none: the branch's second turn would
have forgotten it.

Two consequences worth knowing:

- **A branch is never the conversation an unaddressed turn joins.**
  `_derived_conv` skips them, and `_fork_start` does not move the `.conv`
  sidecar. A branch is by construction the newest thing in the day, so without
  this every reminder, voice reply and family DM after a fork would land in the
  side thread rather than the conversation being had.
- **`_iter_sessions` re-joins a conversation it has already seen.** A branch runs
  *beside* its parent and the two write into the same day file turn about;
  without this the second half of each becomes a third conversation with an
  invented id — a duplicate in the sidebar, and a wrong answer from
  `_derived_conv`. Silence still splits. `splitConversations` in `chat.html`
  mirrors it, as always.

## Per-user themes (`/theme.css`, `THEME_ROLES`, `THEME_SHAPES`)

The House is the house style; a theme is one member's version of it. Every page
links `/theme.css` **after** its own `<style>`, so the theme wins by order
without needing specificity. No session or no theme is an **empty sheet with a
200** — never a 401 and never a redirect, because it sits in the head of every
page and an error there is a console full of noise on every load.

A theme can change three things and nothing else, which is what makes it safe
to let Alfred generate one — the worst it can do is look wrong, never move a
button or hide a control:

- **Colour.** The fourteen roles in `THEME_ROLES` and nothing outside that list;
  an open token list would let a theme redefine `--display` or the spacing.
  The derived shades (`--ink-deep`, `--rule`, `--tint`…) are **computed** from
  the roles, not asked for, so they cannot be forgotten or got wrong.
- **Shape.** One of the four named sets in `THEME_SHAPES`, applied by *selector*
  with `!important` rather than through the `--r-*` tokens — the pages hardcode
  around 200 radii and rewriting them all is a far bigger risk than this sheet
  is. It is the last stylesheet on the page and only exists when somebody chose
  a theme, so being an override is its whole job.
- **The ground.** A generated backdrop, painted on `body::before` at 18%
  opacity behind everything, or — with no backdrop — the two background washes
  restated in the theme's own colours, so a plum house does not keep The House's
  cream glow in the corner.

**Contrast is checked before anything is stored**, with the pairs in
`THEME_CONTRAST` (AA for text, a lower bar for the accent, which is used for
borders and fills rather than words). A refusal names the pair and the ratio,
because Alfred reads that message and tries again. **The same check is
duplicated in the skill** (`nanobot/skills/theme/scripts/theme.py`) so a bad
palette costs a sentence instead of a round trip and a minute of image
generation — the two are verified against each other over random palettes in
`local/test_themes.py`; keep them in step.

Alfred writes the palette himself and it is *that* palette that goes into the
image prompt, so the backdrop is made to match the theme. Never the other way
round: a theme sampled from an image takes its accent from whatever happened to
be bright in a corner.

**A theme lands on open pages by itself.** Every page polls `/theme/version` —
one integer, no session required, `0` when there is none — every 20 seconds
while it is visible, and swaps the `href` on the `/theme.css` link when the
number moves. Without it, «Alfred, ponme la casa en verde» repainted nothing
until somebody thought to reload, which is the moment the feature stops feeling
like it works. Only while visible, so a backgrounded phone asks for nothing,
and also on `visibilitychange` so coming back to a tab is instant rather than
up to twenty seconds late.

**The Android app follows it too.** HomeCore's own nine templates hand
`--wall`/`--plaster` to `window.AndroidTheme.apply` (home-chat's `ThemeBridge`),
which colours the status bar and the window behind the WebView — neither of
which CSS can reach. Read back out of the computed style, so it cannot drift
from the sheet. The proxied ledger does not: it is finance_helper's own HTML,
with no `/theme.css` link and no bridge call, so it paints The House and the
app's chrome keeps whatever the previous page set.

Note this only works off-VPN because home-chat's proxy routes `/theme.css`,
`/theme/version` and `/theme/backdrop` through to HomeCore. A prefix that is not
in that list 404s at the VPS and the page silently falls back to The House
values baked into its own `:root` — which looks like "the theme did not save"
rather than "the sheet never arrived".

**Only HomeCore's own pages, plus the proxied ledger.** Cameras and the MQTT
dashboard are other origins with their own login and no idea which member is
looking, so they stay on The House. If somebody asks why the cameras are still
green, that is why.

## Background answers, presence & notifications

- **`/chat/send` waits for nothing.** Only the immediate reply travels over that
  request. HomeCore used to hold a WebSocket to nanobot open for up to 30 minutes
  waiting for a spawned task — which capped how long a task could take at how
  long we'd hold a socket, and made delivery depend on a connection that dies
  exactly when it matters (phone locks, app backgrounded).
- **nanobot pushes instead.** When its WebSocket channel has output for a
  `homeweb:<user>:<day>` chat_id and finds nobody subscribed, it POSTs to
  **`/chat/agent-event`** (`nanobot/channels/homeweb_relay.py`, proxy-authed with
  the same per-user derived token the skills use). That saves the reply to the
  day's history and pushes ntfy unless the user is watching. Subagent
  **start/done/progress events are relayed always**, subscribed or not — they're
  the record `bgtasks.db` keeps and the "En segundo plano" panel lists
  (`GET /chat/background-tasks`). The endpoint re-checks the chat_id against the
  authenticated user; never let a body decide whose history it writes to.
- **The panel is a log, not a chat.** `progress` events carry the task's own
  running commentary — the model's text before each tool call, and the tool
  calls with their outcome (`bg_task_events`). Tapping a task opens a timeline:
  monospace, dense, timestamped — 💭 monologue / 🔧 call / ↳ result / ▸ phase
  (`BGTASK_EVENT_KINDS`, mirrored by `TRACE_ICONS` in `chat.html`), closed by
  the finished answer as an unmarked card. The answer arrives on the `message`
  event *after* `done`, so the panel keeps polling a finished task until its
  `result` lands rather than stopping the moment the status flips.
  `GET /chat/background-tasks/<id>?after=<last event id>` is what it polls
  (`TRACE_POLL_MS`, 2.5 s, only while open and visible, stopping when the task
  ends), so an open panel fetches the delta rather than four hours of
  commentary. The finished answer still lands in the conversation and still
  pushes ntfy — the panel is additional, not a replacement.
- **Progress is deliberately ignored on the SSE stream.** `chat.html` returns
  early on `subagent_event === 'progress'`. Painting from both sources would
  draw every line twice (only the stored copy has an id), and that stream only
  carries the conversation the page is attached to while the panel opens from
  anywhere — a trace that filled in only sometimes is worse than one that lags
  2.5 s. The early return also matters on its own: these arrive many times a
  minute, for hours, and must not trigger the task-list refresh below them.
- **Both ends are bounded, and the caps only cost commentary.** nanobot stops
  narrating after `NANOBOT_SUBAGENT_PROGRESS_MAX` events and clips each line;
  the relay queues progress through a single ordered worker (one connection,
  correct order) that drops the *oldest* when its queue fills, since someone
  opening the panel wants what the task is doing now. HomeCore keeps
  `BGTASK_EVENTS_KEEP` rows per task, trimmed on insert, each truncated to
  `BGTASK_EVENT_MAX_CHARS`. A task whose narration is dropped runs to
  completion exactly as before.
- A task with no `done` event after `BGTASK_STALE_S` (12 h) reads as `lost` —
  computed on read, so there's no reaper thread. `_bgtask_attach_result` pairs a
  proactive reply with the newest task that finished in the last
  `BGTASK_RESULT_WINDOW_S` and has no result yet (nanobot announces the finish
  and phrases the answer as two unlinked events).
- Duplicate delivery is handled at the bottom: `append_user_history` dedupes, and
  `/chat/agent-event` only pushes ntfy when the append actually happened — so a
  reply the open page already received and persisted is never pushed twice.
- The cap on a single background task is nanobot's
  `NANOBOT_SUBAGENT_MAX_RUNTIME_S` (set to 4 h in `docker-compose.multiuser.yml`;
  the upstream default of 20 min quietly truncated real research into a
  partial-progress summary).
- **"Is the user watching?" is client-reported**: `chat.html` posts
  `POST /chat/presence` while visible (`_user_watching` / `_mark_user_watching`),
  because an Android WebView keeps its `/chat/events` SSE stream open in the
  background. Every "only notify if they're away" check uses `_user_watching`.
  The Android app **also** reports it from `onStop`/`onResume` via
  `POST /chat/presence-native` (`Presence.kt`, native auth — no CSRF token
  exists outside the WebView). Both write the same state; the app's report is
  the one that survives a process being frozen on the way to the background,
  which the page's `pagehide` fetch does not reliably manage. A missed "hidden"
  leaves someone marked as watching for `VISIBLE_LIVE_THRESHOLD_S`, and every
  reply inside that window is silently not pushed — the shape of "I asked
  Alfred something, locked the phone, and never heard back". The decision is
  logged: `docker logs local-web-1 | grep "push:"`.
- **The chat page repairs itself rather than waiting to be reopened.** The SSE
  stream is the only thing that paints a reply into an already-open page, and it
  dies quietly — a backgrounded WebView, a laptop waking from sleep, a network
  blip. It has no `Last-Event-ID` replay, so whatever arrived during the gap was
  delivered to nobody. `chat.html` therefore reconnects when closed (checked on
  the presence ping and on `visibilitychange`) and re-reads the day's history on
  **every reconnection** (`es.onopen` → `catchUp()`), not just when the tab
  becomes visible — a desktop tab left open all afternoon never fires
  `visibilitychange` at all. `catchUp` repaints only when the message count
  actually changed, and never mid-turn, since the streaming bubble is not in the
  server's history yet.
- **Whoever decided the view last owns it: `viewEpoch`.** `initHistory`,
  `openDay`, `openSessionEntry` and `catchUp` all read history over the network,
  so the one that went out first can come back last. Each captures the epoch
  before awaiting and drops what it loaded if it changed; `startNewChat` bumps
  it. The bug this closes was in the house on 2026-08-15: the app launches on
  `/chat?new=1`, whose `startNewChat()` runs in `initHistory`'s `.then()` —
  while the `catchUp` that `initHistory`'s own `connectEvents()` started is
  still in flight. It landed second, painted the abandoned conversation back
  over the clean sheet and re-pinned its id, but could not clear `freshChat`
  (only the thing that moves the view owns that flag). So the page showed one
  conversation while the next message re-stamped a *third*: Alfred was asked to
  turn off a light he had no memory of, and one exchange became two sidebar
  entries. `local/test_catchup.js` covers it — and note that a name `catchUp`
  calls but the test does not lift is a `ReferenceError` inside its own `try`,
  which the page swallows; that is how five checks there sat silently green over
  a `catchUp` that threw on every unpinned repaint.
- **`?new=1` does not paint first.** The parameter is acted on after
  `initHistory` resolves (the sidebar and the day list come from that load), so
  the page reads it up front as `LAUNCH_NEW` and simply does not open the
  previous conversation. Otherwise a cold start shows a conversation it is about
  to leave for the length of a history fetch — long enough to read it and start
  typing into it. `resumeTurns` follows the same rule: a running turn belonging
  to another conversation is never drawn into the view, including when the view
  has no conversation yet.
- **A launch onto a warm conversation continues it** (`launchStaysWarm`,
  `LAUNCH_WARM_MS` = `SESSION_GAP`, 3h — it was 30 min until 2026-08-18, when
  opening the app an hour later and finding a blank sheet was reported from the
  house as Alfred having forgotten). The app asks for a clean sheet on every
  launch, but it cannot tell "a new thought" from "the next sentence
  about what I just asked" — it has been in the background either way. The page
  can, because it has just read the day, so it declines and tells the init block
  through `launchContinued`. **Warmth is measured on stamped messages only**
  (`conversationActiveAt`): geofence alerts and reminders are filed *unstamped*
  into whichever conversation is open, and with four phones in the house they
  arrive every few minutes — reading the last message would call every
  conversation warm until midnight. **That makes the launch the only reader on
  the stamped clock** — `backToCurrentChat`, `send()` and the server's
  `_conv_last_ts` all measure silence from the last message of *any* kind — so
  on a day whose alert trickle outlives the last human turn by more than
  `SESSION_GAP`, a launch opens a clean sheet while every other door opens the
  old conversation. Equal windows are not yet one rule; the clock is the half
  still to be unified. Both halves are in
  `local/test_chat_new_on_launch.py` and `local/test_catchup.js`; the app half
  of the contract (`HOME_URL`) is in `home-chat`, which is why that test reaches
  across the superproject.
- `append_user_history` **dedupes** within 10 minutes, so a background worker
  and a live page can both persist the same reply safely. It compares
  `_dedup_key` — the text after skill blocks are stripped and whitespace
  collapsed — **not** the raw strings. Comparing raw strings is what produced
  «Alfred repite la respuesta»: the server stripped `{"skill": …}` before
  storing and `chat.html`'s two turn paths did not, so the same answer was two
  different strings and both were kept, one with the plumbing on the front.
- **Anything with `role: bot` is stripped of skill blocks by
  `append_user_history` itself**, not only by the four doors that call it
  (`_turn_deliver`, `/chat/agent-event`, `_alfred_reply`, and the page). Each
  door still strips — the earlier a block dies the better — but the store is
  the boundary that cannot be bypassed by a caller that forgets, which is
  exactly how the page bypassed it. A bot message that is *only* a block is not
  stored at all and `append_user_history` returns False, which every caller
  already reads as "nothing was filed". A `role: user` message is never
  touched: somebody pasting JSON at Alfred meant to.

## Voice (`POST /chat/voice`, `POST /chat/transcribe`)

- `/chat/voice` (native app, `@_geo_native_auth`) takes **either** a `text` form
  field — the phone transcribed it on-device, no Whisper round trip — **or** an
  audio `file`. Keep the audio branch: it's the fallback and old APKs use it.
- It returns as soon as the transcript is saved; `_voice_deliver` runs the LLM
  turn on a daemon thread and pushes the reply via ntfy. Never put that back
  inline — it used to block the response for up to 90 s against a 45 s client
  timeout, so a delivered message reported "no se pudo enviar".
- All Whisper calls go through `_whisper_transcribe`, which sends
  `WHISPER_LANGUAGE` (default `es`, reduced to its first subtag since
  faster-whisper rejects `es`). `WHISPER_PROMPT` exists but is empty on
  purpose — `_ASR_HINT_SUBSTR` is the scar tissue from a prompt that leaked into
  transcripts.
- `_clean_transcript(text, drop_hallucinations=)` — the junk list ("gracias",
  amara.org, hint echoes) is for what *Whisper* invents out of silence; it's off
  for on-device text, which reports "no match" instead of inventing words.

## Document attachments (`POST /chat/upload-doc`)

Attach a PDF, Word, Excel, CSV, txt or md file in the chat and Alfred reads it.
**The extraction happens here, not in nanobot**: the family's model is a text
model and Alfred's tools (`exec`/`glob`/`grep`) would see a `.xlsx` as bytes, so
`_extract_document_text` turns the file into text and `/chat/send` folds it into
the message. Images do *not* come this way — they still travel as data URLs to
the vision model, a different pipeline for a different reason.

- Per format: `pypdf` / `python-docx` / `openpyxl`, each **imported lazily inside
  its own extractor**, so a missing library costs that one format and reports
  itself instead of stopping the app from booting.
- `.doc` and `.xls` (pre-2007 binary) are **refused by name**. They need entirely
  different parsers; emitting mojibake and letting Alfred reason about it is
  worse than saying no.
- Every failure is phrased in Spanish and shown to the user as-is, because every
  one of them is actionable: wrong format, scanned PDF, file too big. A PDF that
  yields no text says so rather than attaching an empty document — the chip in
  the UI reports the extracted character count for the same reason.
- `openpyxl` reads with `data_only=True`, i.e. the **cached** result of a
  formula. A sheet generated by a script and never opened in Excel has no cache
  and reports empty computed cells. That is the file, not a bug.
- **A document is untrusted third-party text reaching an agent with tools** —
  the same category as a relayed phone notification, and `_documents_block`
  frames it the same way: contents go between explicit markers, and anything
  inside that reads like an instruction is content to report, not to obey.
- Attachments are extracted at upload and stored under `history/docs/<user>/`,
  swept after `DOC_TTL_S` (a week). This is a *chat attachment*: files the family
  wants to keep, browse or share belong in `/files`. `/chat/send` takes ids, not
  text, and skips ids that have expired rather than failing the message.
- The Android file picker previously hardcoded `image/*` and ignored the page's
  `accept=""` — so documents are unselectable on the phone until the APK
  carrying `MainActivity.fileChooserMimeTypes` ships. The browser works as soon
  as HomeCore is deployed.

**Three routes, one body.** `_receive_document` does the reading, extracting and
storing; the routes differ *only* in how they authenticate, and that difference
must not spread into the file handling:

| route | auth | caller |
|---|---|---|
| `POST /chat/upload-doc` | `@api_login_required` (CSRF) | the 📎 button |
| `POST /chat/share` | `@_geo_native_auth` (CSRF-exempt) | the app's share sheet |
| `GET /chat/doc/<id>` | `@api_login_required` | the page, drawing a shared chip |

`/chat/share` is CSRF-exempt for the documented reason: the share activity posts
before any WebView exists, so it has no token to send — the same case as
`/chat/voice` and `/geo/api/event`. `GET /chat/doc/<id>` returns **metadata
only**; the text never makes the round trip, because the page has no use for it
and `/chat/send` reads it server-side from the id. Both the id and the username
are sanitized into the path, so one person's id cannot resolve into another's
directory — covered by tests.

## Device log relay (`POST /chat/applog`, `GET /debug/applog`)

The Alfred app's own diagnostic lines, so they can be read from outside the
house. The phone decides what to send: `AppLog.report()` uploads, `AppLog.log()`
stays local. Stored per user under `history/applog/<user>.log`, ring-buffered,
and mirrored to the container log as `applog:` lines so a phone event and a
server event land on one timeline.

```bash
curl -sk -H "X-Debug-Key: $DEBUG_API_KEY" \
  "https://hub.home:21001/debug/applog?user=user1&limit=100"
```

- Lines carry **two** clocks: the phone's local stamp as written, and a server
  stamp in **UTC** in front, because UTC is the container's clock and therefore
  the one `docker logs` prints. Don't normalize them to one — each half is
  compared against a different thing.
- `POST` is `@_geo_native_auth` (CSRF-exempt, session cookie), like `/chat/voice`
  — background receivers post with no WebView loaded.
- `GET` takes `X-Debug-Key`/`?key=` for any user, or a plain session for your
  own. It is the reason this exists: `adb logcat` needs a cable and a person
  standing next to the phone, which is why on-device speech recognition failed
  for a week undiagnosed.
- Not a general log sink. It is size-capped both per upload and on disk, and a
  chatty caller pushes the interesting lines out of the buffer.

## Phone notifications (`notifications.db`, `/chat/notification*`)

The Android app's `NotificationListenerService` relays other apps' notifications
here so Alfred can act on them. Two permissions per app, both default-off and
both enforced server-side:

- `can_read` — Alfred hears about it at all. Until it's on, the phone posts only
  the app's identity (package + label) so it can appear in the settings list;
  `notif_ingest` stores nothing else.
- `can_reply` — Alfred may answer through the notification's own reply action.
  **`notif_reply` re-checks this against the DB on every call** — never let a
  prompt be the thing that decides.

Plus per-user rules, handed to Alfred as judgement. The toggles are the
boundary; the rules are the discretion inside it. They live in
`notif_rule_items` — **one row per rule, each individually switchable**
(`POST/DELETE /chat/notifications/rules[/<id>]`). `_notif_rules` renders only the
*enabled* ones as a numbered list for the prompt: a switched-off rule must be
invisible to Alfred, not shown as disabled, or he reasons about why it's off.
The old free-text `notif_rules` blob is dead — `init_notif_db` splits it into
rows once and stamps `migrated`, so wiping every rule doesn't resurrect the
original text on the next boot.

Flow: `POST /chat/notification` (native auth, CSRF-exempt) → dedupe + per-app
rate limit → `notif_log` → `_notif_deliver` on a daemon thread → `_alfred_notify`
→ ntfy push when the user isn't watching. Alfred answers via the `notifications`
skill → `POST /chat/notifications/reply` → a silent `notif_reply` ntfy control
message carrying `{key, text}` → the phone fires the RemoteInput. `_notif_sync`
pushes `notif_sync` when a toggle changes so the phone reloads its allowlist.

**Notification text is untrusted third-party input reaching an agent with
tools.** `_notif_deliver` frames it as such in the prompt and the skill repeats
it, but the real containment is the per-app gates — keep it that way.

## Alfred → Alfred direct messages (`POST /chat/dm`)

One family member's nanobot messaging another's, optionally with a file:
`{"to": "user2", "text": "…", "path": "user1/documentos/x.docx"}`. `to` goes
through `_resolve_assignee` (nicknames included). An attachment is **not
copied** — `_grant_share` gives the recipient read-only access to the sender's
own file, delivered as a `/chat/shared-download?path=…` link (that route mirrors
`/files/api/shared/download` but lives under `/chat`, the only prefix the
Android app reaches through the cloud proxy). Delivery goes through the
recipient's own Alfred (`_alfred_notify`, so it lands live in their open chat),
falls back to a verbatim history append if nanobot is down, and pushes ntfy
unless they're watching. The nanobot side is the `family-message` skill; auth is
the same per-user derived proxy token as `tasks`, so an instance can only ever
act as its own user.

## Per-day chat (Copilot-style history)

Each person's chat **history** is scoped by day (`history/<user>/<date>.json`;
`_valid_day` guards the path against traversal; the legacy `history/<user>.json`
is read as a fallback for *today* so nothing is lost on the first deploy) — but
the **model session** is scoped by *conversation*: nanobot session id
`homeweb:<user>:<date>:<conv>`, where `<conv>` is the conversation's start (ms).
Before this split, one session per day meant every topic — plus every reminder,
geo alert and notification `_alfred_notify` injected — shared one model context,
so "new chat" was only visual and Alfred mixed conversations.

Who assigns `<conv>` (see `_conv_resolve`): the page sends it with `/chat/send`
(it pins a continued conversation from the sidebar and assigns a fresh start on
"New conversation" — the id is the first message's own `ts`, so the page, the
sidebar split and the server derivation all agree). Server-initiated
*conversation* turns — voice replies, family DMs, anything through
`_alfred_notify` **without** a `scope` — derive it from the day's history: the
current conversation's start, or a new one after `CHAT_SESSION_GAP_MS` of
silence, the same 3h rule that splits the sidebar. (Machine events — reminders,
geo, notifications — don't participate: they pass a `scope` and run in isolated
`ev-*` sessions; see "Which nanobot session a turn runs in" above.) The `.conv`
sidecar next to the day file remembers the page's last explicit choice; it
exists for exactly one case ("New conversation" pressed with *no* gap, which
time-derivation can't see). A turn that opens a fresh conversation stamps its
message with `ts = conv` so the stored and ts-derived ids stay equal.
`/chat/events` attaches to the conversation on screen (`?conv=`) and the page
reconnects it when that changes; `/chat/agent-event` accepts 3- or 4-part
chat_ids and keeps storing by day.

**A message that names a conversation opens one** (`_iter_sessions`, mirrored in
`chat.html`'s `splitConversations` — keep them identical). The boundary is a
`conv` different from the run's id *or*, while the run is still unstamped, from
where the run began. The weaker earlier rule (compare only against a stamped
run) let an unstamped run **adopt** the newcomer's id instead of breaking: a
scheduled 6 AM message landing on a day whose only earlier entries were
unstamped machine-event replies was appended to that chatter rather than
starting the chat it had asked for. A message continuing an unstamped run
carries that run's own start (that is what `_conv_resolve` derives), so it still
adopts and legacy history reads exactly as before. nanobot's cron delivery names
a fresh conversation per firing (`utils/homeweb_chat_id.retarget_for_delivery`),
which is what this rule exists to honour. `local/test_sessions.py` covers it. On the nanobot side, `history.jsonl`
consolidation entries are tagged with their session key and `# Recent History`
only re-injects a session's own entries — the other half of the
conversation-mixing fix. `GET /chat/days` lists days (most recent first)
for the sidebar; `GET /chat/history?date=` loads one; `chat.html`'s sidebar makes
past days continuable. `chat_link(prefill=…, welcome=…, date=…)` — `welcome` shows an
ephemeral bot greeting on open so a notification tap lands you in a chat with
context; `date` opens that day's conversation instead of today's. The Android
app rewrites the whole link onto its own host, so only path + query matter (see
`docs/proxy-notifications.md`, "Where a notification takes you"). In the chat, day
dividers and the sidebar spell the date out (`formatDayFull`), and history is
painted with each message's own timestamp — not the paint time.

## Task notification action buttons (`/tasks/api/notify-action`)

Pending-task reminders carry ntfy action buttons (`_task_reminder_actions`):
**Done ✓** (complete → review), **Later** (postpone 1h), **I couldn't** (excuse —
a reply/RemoteInput action carrying a typed reason). `send_ntfy(..., actions=)`
switches to ntfy's JSON publish to attach them. The app fires the tapped action at
`POST /tasks/api/notify-action` with the session cookie (`@_geo_native_auth`,
CSRF-exempt); it's idempotent (no-op if the task already left `pending`).

**The notification is dismissed only when the POST returns 2xx** (Android side,
`NotifyActionReceiver`). It used to clear in a `finally`, so a 401 or a timeout
made the button look like it had worked while the task stayed pending — a
button that lies about having been pressed is worse than one that doesn't
respond. On failure it stays put, toasts, and reports to `/debug/applog`.

**Watch the origin.** These action URLs are built from `HOMECORE_PUBLIC_URL`
(default `https://hub.home:21001`), and the app looks up the session cookie
**by the URL's host**. The WebView is logged into `https://chat.home`,
so while that env var pointed at the Tailscale address, every button posted to a
host that is unreachable on mobile data and cookie-less on the tailnet — 401 or
timeout, every time, for as long as the buttons have existed. Nobody noticed
because the app dismissed the notification regardless.

Fixed on both sides, and it needs both: the app now rewrites an action URL onto
its own host before firing it (`NotifyActionReceiver.actionUrl`, the same trick
`MainActivity.deepLinkUrl` already did for the tap target), so it no longer
depends on this env var being right — but **older APKs still send it verbatim**,
so point `HOMECORE_PUBLIC_URL` at the cloud proxy anyway. Because the rewrite
means these POSTs now always carry our session cookie, the app matches the path
against an **exact allowlist** (`ACTION_PATHS`); a new button needs its path
added there or it is refused and logged.

## Yes/no answers from a notification (`POST /chat/ask-answer`)

Alfred attaches answer buttons by passing a 4th argument to `ntfy-send`
("Yes|No", max 3 — ntfy's limit). Each button posts its own label here; the
answer goes into the day's chat as a **user** message and `_voice_deliver` runs
Alfred's turn on it in the background, same as `/chat/voice`. The question is
quoted into the message because a push can sit unread for an hour and a bare
"Yes" three messages later is a guess.

## Backup dashboard
`/backups` + `/api/backup-history*` only **display** backup runs reported by an
external job (POSTed to the backup-ingest server). The actual backup job (what
paths get backed up) is **not in this repo**. History is in
`local/backup_data/backup_history.db`.

Two producers post runs, distinguished by `host` (see the `home-backups` repo):
the nightly `backup.py` run as `compute`, and the two Azure media mirrors
(`media_sync.py --push-history`) as `storage/immich` and `compute/cameras`.
Mirrors get their own run rows on purpose — different host, schedule and storage
account.

Two things to know before changing this code:

- **`size_bytes == 0` means "failed" here.** Both `_backup_status()` and
  `backups()` turn a run red if *any* of its jobs reports 0 bytes. That is why
  mirror jobs report the total mirrored tree rather than the night's delta — a
  mirror with nothing new to upload would otherwise cry wolf. Don't "fix" a
  producer to send deltas without changing this rule first.
- **Don't use `cursor.lastrowid` after the upsert.** `INSERT ... ON CONFLICT DO
  UPDATE` inserts no row on the update branch, so `lastrowid` still holds some
  earlier INSERT's id and the run's jobs get attached to the *wrong run* — it
  previously replaced another host's jobs wholesale on any re-push. The id is
  now re-selected by `(run_date, host)`. Re-pushes are routine (Jenkins reruns).

## Conventions

- User-facing strings are Spanish. Sizes/dates use `es` formatting.
- Secrets come from env vars with safe empty defaults (`SMB_PASSWORD`,
  `PUSHOVER_*`, `NTFY_BASE_URL`); never hardcode. `.env` is gitignored.
  Exception: `SECRET_KEY`/`DEBUG_API_KEY` have **no** fallback — see Auth model.
- Notifications: **Pushover** (`send_pushover`, single household key) for general
  alerts; **ntfy** (`send_ntfy`, per-user) for share notifications.
- `local/backup_data/` is a mounted volume — the SQLite DBs there persist across
  deploys; don't commit their contents.
- Templates other than `files.html`: `index.html` (dashboard), `chat.html`
  (Alfred, talks to a per-user "nanobot" over WebSocket), `login.html`,
  `backups.html`.

### `chat.html` rich content rendering
Bot messages are rendered through a hand-rolled `renderMessageContent()` (never
`innerHTML` — always real DOM nodes via `createElement`/`textContent`, with
`sanitizeHref()` allowlisting `http(s)` only, so scraped page content or a
prompt-injected link can't inject script/`javascript:` URLs). It supports:
- markdown-lite: `**bold**`, `` `code` ``, `[text](url)`, `![alt](url)`
- a `:::card\nimage: ...\ntitle: ...\nprice: ...\nsite: ...\nurl: ...\n:::`
  block, rendered as a product card (used by nanobot's `marketplace-search` skill)
- a `:::goto\nspace: programador\nlabel: ...\nwhy: ...\n:::` block, rendered as a
  button into that profession. The href is built from the server-supplied
  `PROFESSIONS` list, never from the block — the model chooses *which*
  profession and never where the button points, and an unknown name draws
  nothing (a dead button is worse than no button)
- LaTeX math via vendored KaTeX (`static/vendor/katex/`, not a CDN — this repo
  is meant to work offline). Only `\(...\)`, `\[...\]`, `$$...$$` delimiters are
  enabled; single `$...$` is deliberately excluded because product prices like
  "$19.990" would otherwise be misparsed as inline math.
