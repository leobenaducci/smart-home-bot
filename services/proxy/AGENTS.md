# AGENTS.md

Guidance for working in this repo. It is **the way into the house from outside
the VPN** — a small FastAPI reverse proxy on a VPS at `https://chat.home`
plus the **Android app** the family actually uses.

## One tree, two deployments

This directory used to be a submodule with a branch per half, built by two
Jenkins pipelines. None of that survived the extraction: there is one branch,
no Jenkinsfiles, and both copies of the proxy are units in `deploy/manifest.yml`
built from this one directory — `local-proxy` (always on, on your own machine)
and `cloud-proxy` (optional, on a VPS you own). A push still deploys nothing;
`./home-stack deploy local-proxy` / `cloud-proxy` does.

## Two halves

| Path | What |
|---|---|
| `server/` | The FastAPI proxy that runs on the VPS |
| `android/` | The Android app (Kotlin) — a WebView wrapper plus real native pieces |

## The proxy

Runs on the VPS as `home-chat-proxy` with `network_mode: host`, bound to
**`127.0.0.1:8080`** only; the host's Caddy fronts it and terminates TLS. It
reaches HomeCore over an **autossh reverse tunnel** the home side opens on the
VPS's loopback (`HOMECORE_LOCAL_URL`). `VERIFY_UPSTREAM_TLS=0` because HomeCore
presents a self-signed certificate on that private link.

### It runs twice, on purpose

There is a **second copy on the hub**, container `home-chat-proxy-local` on
`127.0.0.1:21003`. It is the `local-proxy` service in `deploy/manifest.yml`,
which layers `docker-compose.local.yml` over the same `docker-compose.yml`.
Point your DNS at the hub for the public name and, inside the house, that name
reaches this copy through the portal's Caddy — no trip out to the VPS and back
through the tunnel. Outside the house nothing changes.

Its upstream is `https://127.0.0.1:21001` (HomeCore is on the same box) instead
of the tunnel. Everything else has to match the VPS or the seam shows:

- **`SESSION_SIGNING_KEY` must be identical** on both. Both units read it from
  `PROXY_SESSION_SIGNING_KEY` in `secrets/smart-home-bot.env`, so they agree by
  construction. Different values log a phone out every time it crosses between
  wifi and mobile data.
- **`data/devices.db` is per-instance.** A device enrolled against the VPS is
  not enrolled here, and login refuses an unenrolled device whatever the
  password is. Copy it across between the two `{paths.state}/*-proxy` directories,
  or mint local codes with
  `docker exec home-chat-proxy-local python manage_devices.py`.
- **Neither copy holds a user store.** Both run `AUTH_MODE: upstream` and ask
  the portal.

**It forwards only the prefixes it lists**, and 404s everything else:

```
/chat*  /tasks*  /geo*  /grocery*  /menu*  /camaras*  /luces*  /finanzas*
/files*  /theme* (GET, three paths)
```

That list is a hard constraint on HomeCore, not a detail of this repo. It is why
several HomeCore features deliberately live under `/chat` even when they are not
chat — the map renderer, shared-file downloads, the document share target. **A
new HomeCore route that the app must reach has to sit under one of these
prefixes, or be added here.**

"Off-VPN" undersells it: **the app comes through this proxy even on the home
wifi**, so an unrouted prefix is broken in the app always, while a browser on
the LAN sees a working page the whole time. That is why the gap keeps being
found by somebody tapping a tile rather than by anyone testing — it happened to
`/menu`, then `/camaras` and `/luces`, then `/finanzas` and `/files`.

Two checks close it, one on each side, because the tile and the route live in
different repositories: HomeCore's `local/test_app_tiles_reachable.py` reads
this file and fails when a Casa apps tile has no route here, and
`server/test_proxy_routes.py` checks the routes behave (a logged-out page
navigation must redirect to `/login`, never 404 — a 404 *is* the shape of this
bug).

`/` redirects to `/chat?embed=1`. The proxy also serves `/login`,
`/login/challenge`, `/enroll/complete`, `/logout`, `/api/ntfy-config`, `/ping`
and `/healthz` itself.

## Auth: password **and** an enrolled device

Remote login needs both. The account password is verified **at home**: the
proxy forwards it down the tunnel to the portal's `/api/auth/verify`
(`AUTH_MODE: upstream`). No copy of the bcrypt user store is kept out here, and
a network failure is a failed login rather than a successful one. On top of
that, the
device must prove possession of a private key it generated — the server issues a
single-use challenge and the device returns a signature. **The private key never
leaves the phone.**

**No HTTP route can mint an enrollment code.** Only SSH to the VPS can:

```bash
ssh root@chat.home
cd ~/.local/share/home-stack/cloud-proxy/vps
docker compose exec chat-proxy python manage_devices.py code <username>
```

`<username>` is the **numeric login id** (e.g. `user1`), not a display name.
The code is single-use and expires in **10 minutes**; redemption is rate-limited
per IP. Manage enrolled devices with `manage_devices.py list | add | remove`, and
per-user ntfy subscribe config with `manage_ntfy.py`.

Full detail and troubleshooting: [`docs/proxy-device-enrollment.md`](../../docs/proxy-device-enrollment.md).

State is only what the proxy itself owns: `devices.db` (enrolled keys and
codes) and `ntfy_config.json`, in the `data/` mount. That mount points outside
the pushed tree — `{paths.state}/local-proxy` or `{paths.state}/cloud-proxy` —
so a redeploy never touches it. No user store, no application data.

## The Android app

More than a WebView. `android/app/src/main/java/com/chat/app/` holds the native
pieces the house depends on:

| Package | Role |
|---|---|
| `geo/` | OS geofences, boot re-registration, arrival reports to `/geo/api/event` |
| `notif/` | `NotificationListenerService` — relays other apps' notifications so Alfred can act on them |
| `assist/` | On-device speech recognition and the assistant session |
| `ntfy/` | Push subscription |
| `share/` | Alfred as a destination in Android's share sheet |
| `ring/` | "Find my phone" — plays on the **alarm** stream so silent mode cannot mute it |
| `Presence.kt` | Reports foreground/background so HomeCore knows whether to push |
| `AppLog.kt` | Diagnostics uploaded to HomeCore's `/chat/applog` — there is no `adb` here |

**The status bar follows the person's theme** (`ThemeBridge`,
`window.AndroidTheme.apply(wall, plaster)`). HomeCore serves each member their
own palette as `/theme.css`, which paints the page — but the status bar and the
window behind the WebView are the app's, and CSS cannot reach either, so a plum
house under an olive status bar reads as a bug in the app. Every HomeCore page
hands over the two colours it is actually painting with, read back out of the
computed style so the two cannot drift, and does it again whenever the theme
changes under an open page.

The pair is remembered in prefs and re-applied in `onCreate` **before the
WebView loads anything**: the window background is painted long before any
JavaScript runs, so without the memory every launch would sit on The House olive
until the page loaded and then correct itself. It shortens that flash rather
than removing it — the system still draws the starting window from the static
`@color/wall`/`@color/plaster` in `res/values/themes.xml` before any of our code
runs, and no app-side memory can reach that frame. A browser has no bridge and
simply skips it.

`applyThemeColors` paints the status bar, the window behind the WebView **and
the biometric lock panel**, which covers the whole frame whenever the lock is
up — leaving that one on the static house colours put a The House screen under a
themed bar, which is the mismatch this exists to remove. The navigation bar and
the recents card are still unthemed.

**The biometric lock follows the phone's lock, not the app's comings and
goings** — see [`LockPolicy`](android/app/src/main/java/com/chat/app/LockPolicy.kt),
which is where the rule is written down and the only place to change it. It
re-locks when the screen turns off, or after five minutes in the background
with the screen still on; a quick trip to another app does not. Screen-off is
read twice on purpose — an `ACTION_SCREEN_OFF` receiver, and `isInteractive` at
`onStop` — because the broadcast alone races our own `onStart` when the OS has
frozen the process. It used to drop
on every `onStop`, which cost a fingerprint for glancing at another app, and
nine `skipRelock` flags had grown around it — one per place somebody noticed
(file picker, camera, permission dialog, settings). Those are gone: none of
them is the phone leaving your hand. The policy holds no Android types on
purpose, so `android/app/src/test/` can run it on the JVM —
`./gradlew :app:testReleaseUnitTest`, no device.

**The panel and the WebView are one decision, made in `showLockPanel` /
`hideLockPanel` and nowhere else.** Both visibilities used to be set across
three functions, and the case none of them covered was *unlocked, with a
freshly built panel on top*: the only route out of the locked state ran through
the prompt, so an `onStart` that skipped it left the Activity as `onCreate`
built it — lock up, WebView hidden behind it, nothing ever asked to load. That
was rare until the lock went process-scoped and then became what **every**
Activity rebuild does. The first load lives in `onUnlocked()` for the same
reason: `loaded` is an Activity field, so a rebuilt Activity that skips the
prompt still has to be told to load something. The panel is also
`isClickable`/`isFocusable` — a `TextView` consumes no touches, and a
FrameLayout hands what the top child refuses to the one underneath, so covering
the chat was never the same as blocking it. `LockPanelTest` fails the build if
either property is broken; it reads the source, because there is no Robolectric
here.

**It is process-scoped (`LockPolicy.shared`), never a field of the Activity.**
Android rebuilds an Activity whenever it reclaims a backgrounded one, on any
configuration change it was not told to absorb (locale, dark mode, display or
font size), and always under "Don't keep activities" — and each rebuild
constructed a fresh policy, which is a *locked* one. The first version of this
fix shipped that way and the fingerprint came back on every app switch exactly
as before, which read as the build never having landed. Process death still
locks it, which is the fail-safe that matters.

**Background receivers post with only the session cookie**, before any WebView
exists, so the HomeCore endpoints they hit are CSRF-exempt by design
(`@_geo_native_auth`). Any new native POST target must follow that pattern.

**The app rewrites incoming URLs onto its own host** before using them
(`NotifyActionReceiver.actionUrl`, `MainActivity.deepLinkUrl`), because the
session cookie is looked up by host and the WebView is logged into
`chat.home`. Action paths are checked against an **exact allowlist**
(`ACTION_PATHS`) — a new notification button needs its path added there or it is
refused. [`docs/proxy-notifications.md`](../../docs/proxy-notifications.md) documents where a
notification tap lands.

**Opening the app asks for a new conversation; tapping a notification continues
one.** A plain launch loads `MainActivity.HOME_URL` (`/chat?new=1`), and
HomeCore's `chat.html` treats that parameter as "clean sheet" — it is the only
thing that does. *Asks*, not gets: since 2026-08-18 the page declines while the
last conversation is still current (`launchStaysWarm`, three hours of silence,
the same window that splits conversations everywhere else — it was 30 minutes
before), so inside that window a launch and a notification tap land in the same
place. Do not read `?new=1` as an unconditional clean sheet when chasing "it
opened on the old chat". A tap sets `EXTRA_DEEP_LINK` and carries the ntfy
click URL through `deepLinkUrl`, which never emits `new`, so it lands where the
notification pointed. The distinction is *which URL is chosen*, not a flag the
page interprets, so a new caller gets the safe default by doing nothing.

The two halves are in different repositories and can rot apart in either
direction, silently — the symptom is only ever "sometimes it opens on the old
chat". `services/home-core/local/test_chat_new_on_launch.py` runs the page's real init
block against stubs *and* reads this repo's `MainActivity.kt`, the same
cross-repo trick as `test_app_tiles_reachable.py`.

The Android app has no pipeline in this package — its two Jenkinsfiles did not
survive the extraction. Build it from `android/` with Gradle and distribute the
APK yourself; [`docs/proxy-notifications.md`](../../docs/proxy-notifications.md)
describes the ntfy-attachment route the app uses to pick up a new build.

## Deploying the proxy

Pushing does not deploy. The deployer does:

```bash
./home-stack deploy local-proxy
./home-stack deploy cloud-proxy      # only when cloud.vps.enabled
```

Both rebuild from this directory and leave the `data/` state mount alone. See
[`docs/optional-cloud.md`](../../docs/optional-cloud.md) for the VPS switch and
[`docs/proxy-home-setup.md`](../../docs/proxy-home-setup.md) for the home-side tunnel.

## Related

The root [`README.md`](../../README.md) and [`CLAUDE.md`](../../CLAUDE.md) have
the machine map and stack conventions;
HomeCore's [`AGENTS.md`](../home-core/AGENTS.md) documents everything on the other
side of the tunnel.
