# ntfy push notifications (Alfred Android app)

The Android app runs a foreground service that holds a live WebSocket to the
self-hosted ntfy server (`https://ntfy.home`, same one Alfred already
*publishes* reminders/shares to — see `services/nanobot/config/SOUL.md` and
`services/home-core/local/app.py`'s `send_ntfy`) and turns incoming messages into real
Android notifications, even while the app is backgrounded or locked.

Each family member reads with their **own** ntfy account/token, scoped
read-only to their topics — separate from the single shared `NTFY_CREDENTIALS`
used for publishing. Config (which topics, which token) is fetched by the app
automatically right after login from `GET /api/ntfy-config` — nothing to type
into the app. See `services/proxy/server/ntfy_config.py` / `services/proxy/server/manage_ntfy.py` for the
implementation, and `docs/proxy-integration-plan.md` for how this fits the overall
architecture.

## Required `.env` (VPS, `~/.local/share/home-stack/cloud-proxy/vps/.env`)

One optional value, defaults to the existing server if unset:

```
NTFY_BASE_URL=https://ntfy.home
```

## Adding a new user (or a new phone for an existing one)

### 1. Create a read-only ntfy account for them (on the ntfy server host)

Pick their topic(s) using the same convention the publish side already uses:
the personal topic is the **member id, capitalized** — `user1` publishes to
`User1`. That is `_ntfy_topic()` in `services/home-core/local/app.py`, which
returns `os.environ.get(f'NTFY_TOPIC_{folder.upper()}', folder.capitalize())`,
so the id is the default and `NTFY_TOPIC_USER1` overrides it. Add `Family` for
everyone, and any shared topic you want a subset to receive. See
[optional-cloud.md](optional-cloud.md) for the rule this follows.

A topic named after anything else — a first name, a nickname — is subscribed to
successfully and simply never receives anything, because nothing publishes
there.

```bash
ntfy user add --role=user homechat_readonly
ntfy access homechat_readonly User1 read-only
ntfy access homechat_readonly Family read-only
ntfy token add homechat_readonly
# -> prints a token like tk_AgQdq7mVBoFD3M...
```

That token is a revocable **access token**, not the account's password — the
password is set interactively by `ntfy user add` and never used again by this
setup (nothing here logs in as that account, only bearer-auths with the
token). If a token ever leaks, revoke just it:
`ntfy token remove homechat_readonly <token>` — no need to touch the password.

Repeat `ntfy access` once per topic the account should read; `ntfy token add`
only needs to run once per account (reuse the same token if you're just
adding topics later — re-run `ntfy access` for the new topic, no new token
needed).

### 2. Register it with home-chat

```bash
ssh root@chat.home
cd ~/.local/share/home-stack/cloud-proxy/vps
docker compose exec chat-proxy python manage_ntfy.py set user1 User1,Family tk_AgQdq7mVBoFD3M...
```

`<username>` is the numeric login ID (same one used in HomeCore's
`users.json` and by `manage_devices.py`), not a display name. Topics are
comma-separated, no spaces needed (leading/trailing whitespace around commas
is stripped).

### 3. Verify

```bash
# the token actually works against ntfy, for every topic granted:
curl -H "Authorization: Bearer tk_AgQdq7mVBoFD3M..." \
  "https://ntfy.home/Alex,Parents,Family/json?poll=1"
# expect 200 (even with an empty body if there's nothing queued), not 401/403

# home-chat serves it back to an authenticated session:
docker compose exec chat-proxy python manage_ntfy.py list
```

Then on the phone: log into the app as usual. The foreground service fetches
`/api/ntfy-config` automatically on next launch/resume and connects — no
in-app settings screen. Confirm:
1. A persistent low-priority "Alfred notifications active" notification
   appears shortly after login.
2. A test push arrives as a real notification:
   `ntfy-send Alex "test" "Title"` (from any nanobot container) or
   `curl -u "$NTFY_CREDENTIALS" -d "test" https://ntfy.home/Alex`.
3. It still arrives after force-killing the app from Android's recents
   (proves the foreground service survived).

## Publishing an app update

Update announcements ride the existing `Family` topic every family member
already subscribes to — no dedicated topic, no new ACL grants, no new
home-chat endpoint. The app tells a normal chat notification apart from an
update by the `alfred_update` tag; the APK itself is delivered as the ntfy
message's own **attachment** (ntfy hosts it, inheriting the topic's read ACL),
so there's nothing to host on the VPS either.

Two Jenkins pipelines, both **fully automatic — no parameters, no manual
version bump, nothing to build by hand.** Each one checks out, compiles the
release APK and publishes it. Both run on the **`compute`** agent, which is
where the Android SDK is; `storage` only hosts the share they deploy
*to*, over SSH.

| job | source | channel |
|---|---|---|
| `android/Jenkinsfile` | `main` | **stable** — `latest.json` + an ntfy push on `Family` tagged `alfred_update`, so every phone self-installs |
| `android/Jenkinsfile.beta` | `beta` | **beta** — `latest-beta.json` only, no push; visible solely to a phone with Debug → "Beta updates" ticked |

Versioning is computed at build time and injected via
`-PversionName`/`-PversionCode`: `versionName` is a `YYYYMMDD.HHMM` timestamp
(`-beta` suffixed on the beta job) and `versionCode` is minutes-since-2020, so
it is monotonic and the in-app updater always registers a build as newer. Do
not edit `android/app/build.gradle.kts` to release; its literals are only the
fallback for a local build.

Both sign with the household's key in `{config}/alfred-app/debug.keystore`
(never in the repository) — the same key must sign every APK or devices refuse
the in-app update. The stable job needs
the `NTFY_CREDENTIALS` Jenkins credential for its push; the beta job sends none
and needs none.

**Merge into `beta` to queue something for testing, into `main` once it has
been watched working.** `Jenkinsfile.beta` names its branch explicitly rather
than trusting the job's SCM config, because a job whose Jenkinsfile lives on
`main` would otherwise quietly build `main` and publish it as a beta.

**Before the first real release**, confirm ntfy is actually configured to
accept attachments this size — check `server.yml` on the ntfy host (e.g.
`docker exec <ntfy-container> cat /etc/ntfy/server.yml`) for:
- `attachment-cache-dir` — attachments are disabled entirely if this isn't set.
- `attachment-file-size-limit` — defaults to a modest size that may be too
  small for the APK once dependencies grow; raise it (e.g. `100M`) if needed.
- `attachment-expiry-duration` — defaults to just a few hours; raise it (e.g.
  `24h`) so a phone that's offline for a while still finds the attachment
  when it reconnects, instead of hitting an expired link.
Restart ntfy after editing.

Every phone with the app installed and running downloads the APK in the
background as soon as it receives the push, then posts a second "Update
lista — toca para instalar" notification once the download finishes — tapping
it opens Android's package installer. The family still has to approve the
actual install tap (Android doesn't allow silent self-updates), but there's no
separate download/browsing step.

## Alfred notifying you when he finishes responding while you're away

When a message spawns a background subagent, HomeCore pushes an ntfy
notification with the answer to the user's own personal topic (the same one
registered above) — unless they're looking at the chat right now.

Three things make that actually work, all in `services/home-core/local/app.py`:

- **`_spawn_pickup_worker` owns the delivery, and it starts before the reply
  streams** (`chat_send` Phase 1), not after it. It holds the nanobot WebSocket
  for the turn, waits for `subagent_status: start`, and stays until the task
  answers (`SPAWN_PICKUP_TIMEOUT_S`, default 30 min). Previously it was started
  from the tail of the streaming generator — which Flask tears down the moment
  the app is backgrounded or the phone locks — so precisely the case that
  needed a push never got one, and a 120s cap dropped anything slower than two
  minutes.
- **"Is the user watching" comes from the client, not the socket**
  (`_user_watching` / `POST /chat/presence`). `chat.html` pings while the page
  is visible and reports `visible: false` when it's hidden. An Android WebView
  keeps its `/chat/events` SSE stream open in the background, so the old
  stream-liveness heuristic read "backgrounded" as "watching" and stayed quiet.
- **History writes are deduped** (`append_user_history`), so the worker and a
  live page saving the same reply can't double it up.

Chats are **per day** (nanobot session `homeweb:<user>:<date>`; the sidebar
lists days via `GET /chat/days` and each is continuable). The push carries
`click=chat_link(date=<day>)` so the tap opens that day's conversation — see
the next section. A notification can also carry a `welcome` param that greets
you with context on open (`chat_link(welcome=…)`).

This needs no per-user setup beyond what's already in this doc — it reuses the
exact topic each person is already registered with. If it fires while the app
is clearly open and foregrounded, or stays quiet when it's closed, look at
`_visible_last_seen`/`VISIBLE_LIVE_THRESHOLD_S` (25s, one tick past the page's
15s ping) and confirm the phone is running a build of `chat.html` new enough to
post `/chat/presence` at all — an old cached page never pings, which errs
towards notifying.

## Where a notification takes you

Every notification carries ntfy's own `click` field (HomeCore's `chat_link()`),
and `NtfyClientService` now passes it to `MainActivity` as `EXTRA_OPEN_URL`
instead of discarding it — taps used to always land on a blank new chat.

`MainActivity.deepLinkUrl()` rewrites that URL onto the app's own host: HomeCore
builds links from `HOMECORE_PUBLIC_URL` (usually its Tailscale address, which a
phone on mobile data can't reach), so only the path + query survives, and only
for `/chat`, `/tasks`, `/grocery`, `/geo` — anything else falls back to the chat
root, since a push is untrusted input. So a tap lands on the right day's
conversation (`?date=`), with a task message pre-staged (`?prefill=`), or on the
right panel (`?panel=tasks`).

## Silent control messages + task action buttons

Beyond visible pushes, the foreground service intercepts a few **silent** control
messages (matched by ntfy `tags`, never shown as notifications) to drive on-device
features — see `NtfyClientService.handleMessage`:

- `geofence_sync` → re-register the user's OS geofences (`GeofenceManager.sync`).
  Also runs on every ntfy (re)connect, on app resume, and on boot.
- `location_request` → take one fresh fix and POST it (`LocationReporter.reportOnce`).
- `location_track` (body JSON `{until, interval}`) / `location_track_stop` → a
  bounded periodic-fix window (`LocationReporter.startTrack/stopTrack`). GPS runs
  only while a request is active.
- `notif_sync` → reload which apps the notification listener may relay
  (`NotificationRelayService.syncAllowlist`), sent when the user changes a
  toggle in the Notificaciones panel.
- `notif_reply` (body JSON `{key, text}`) → Alfred answering a message through
  its own notification (`NotificationRelayService.sendReply`).
- `ring_phone` (body JSON `{seconds, by}`) / `ring_phone_stop` → "find my
  phone": `PhoneRinger` plays the alarm tone **on the alarm stream**, which is
  what survives silent mode, raises the alarm volume for the duration and puts
  it back, vibrates, and posts a full-screen notification saying who asked with
  a **Detener** button. It always stops itself (45s default, 120s max).
  Triggered by `POST /geo/api/ring` — yourself, or another person if you're an
  admin, the same rule as locate/track.

HomeCore sends these via `_geo_push_control` / `_geo_notify_sync`.

**Task action buttons.** Task reminders published with an ntfy `actions` array
(HomeCore `send_ntfy(..., actions=)`, JSON publish) render as notification buttons —
**Done ✓**, **Later**, **I couldn't** (a reply/RemoteInput field). Tapping fires
`NotifyActionReceiver`, which POSTs to `POST /tasks/api/notify-action` using the
WebView **session cookie** (so it works from the shade with the app closed; the
endpoint is CSRF-exempt native auth). "No pude" merges the typed reason as `note`.

**The notification clears only on a 2xx.** It used to clear in a `finally`, so a
401, a timeout or a missing cookie dismissed it exactly as if it had worked
while the task stayed pending. A failure now keeps the buttons (so it can be
pressed again), toasts, and reports the URL and the reason to `/debug/applog` —
this receiver runs from the shade with the app closed, where `Log.w` reaches
nobody.

**But a press always changes the notification, immediately** — that rule alone
was not enough. "We didn't manage it" was expressed by leaving the notification
untouched, which is indistinguishable from a button that isn't wired to
anything, and that is exactly how it got reported: *"the reminder options don't
do anything"*. The states now are:

```
press → "⏳ Enviando «Lista ✓»…"  (buttons withdrawn, so one option can't be double-fired)
     → 2xx  : gone   (or "✓ Lista ✓" when the action set clear:false)
     → else : buttons back + "⚠️ No se pudo enviar (HTTP 401). Toca de nuevo."
```

So the reason is on the notification itself, not only in a toast that a locked
phone never shows. `AlertNotification` (not `NtfyClientService`) builds these,
because `NotifyActionReceiver` has to rebuild the same notification from a
process the OS started just for the broadcast: the title, body, click target and
the whole `actions` array ride along in each button's PendingIntent extras.

**If the buttons do nothing on a phone, suspect its APK first.** The host
rewrite below and the honest dismissal landed within a day of each other
(2026-07-30); a build from between them fails every button *and* keeps the
notification — the exact symptom above, with none of the new feedback. Read
**Apps → 📜 Log** in the chat and look for `AlfredNotifyAction`: `action FAILED
(...) url=…` names the reason, `action REFUSED, url not allowed` means the path
isn't in `ACTION_PATHS`, and no line at all means the build predates the
reporting.

**The action URL is rewritten onto our own host before it is fired**
(`NotifyActionReceiver.actionUrl`) — only path and query survive, exactly as
`MainActivity.deepLinkUrl` already did for the tap target. It has to be: the
cookie is looked up **by the action URL's host**, and HomeCore builds these URLs
from `HOMECORE_PUBLIC_URL`, which is its Tailscale address
(`https://hub.home:21001`) unless someone set it otherwise. That host is
unreachable on mobile data and cookie-less on the tailnet, so **every task
button had been failing since the day they were added** — silently, because the
notification used to dismiss itself either way. The tap target worked, the
buttons didn't, and the difference was this rewrite.

Because the rewrite means these POSTs now always go out **with our session
cookie**, the path is matched against an exact allowlist (`ACTION_PATHS`) rather
than trusting whatever a push names. A push is untrusted input; it gets to pick
which of a few known endpoints it calls, not to aim an authenticated request
anywhere it likes. A new button needs its path added there — otherwise the
action is refused and the refusal reported.

**Yes/no buttons on Alfred's own pushes.** `ntfy-send` takes a 4th argument of
pipe-separated answers ("Yes|No"), publishes them as actions, and each one posts
its label to `POST /chat/ask-answer` — which puts the answer in the chat as a
user message and lets Alfred take his turn. Same receiver, same dismissal rule.
ntfy allows 3 buttons at most.

**Digital assistant.** The app also registers as a device assistant
(`assist/AlfredVoiceInteractionService`); set it under Settings → Apps → Default
apps → Digital assistant app to talk to Alfred on the assist gesture (hold the
power key). See below for how that speech becomes a message.

## Hold-to-talk: how speech becomes a message

> **Current default: Whisper (path 3)**, still — but no longer for want of a
> diagnosis. On-device recognition returned "I didn't catch that" for every utterance
> because nothing ever asked the recognizer whether it *had* a Spanish model:
> `isOnDeviceRecognitionAvailable()` reports that a recognition service exists,
> not that it can understand anything, and the failure it produces when it
> can't — `ERROR_NO_MATCH` — was the one error the code recorded nothing for.
> So it was retried on the next press, and the next, forever.
>
> `SttSupport.probeSupport()` now asks (`checkRecognitionSupport`), uses a tag
> the recognizer says is **installed**, and triggers a model download when
> Spanish is supported but absent. `noteNoMatch()` sets a path aside for a day
> after three consecutive failures, so it can no longer fail silently forever.
> The default stays `whisper` until one phone has been watched working:
> 🎙️ Voice → "on the phone", then read `/debug/applog` (below).
>
> **"automatic" answered "I didn't catch that" too, and for a different reason.**
> On API 33+ `auto` only picks on-device when the probe says a language is
> installed — so on a phone without one it goes to **path 2**, the system
> recognizer, which the capability work above never touched. Three failed
> utterances each is too generous for a path that has never once transcribed
> anything on this phone, so `noteNoMatch` now sets an unproven path aside after
> a **single** failure and lets Whisper — which demonstrably works here — take
> the next one. `noteRecognized` records the first success per path; after that
> the three-strike rule applies, because by then it has earned it.

`assist/AssistActivity` picks its capture path *before* opening the mic —
`assist/SttSupport` decides, because a SpeechRecognizer and a MediaRecorder can't
both hold the microphone, so there's no changing your mind mid-sentence:

1. **On-device recognition** (preferred, API 31+): words appear live on the
   overlay and only the text is posted to `/chat/voice`. No audio upload, no
   Whisper — the message is on its way a few hundred ms after you stop talking,
   and it works with no signal at all.
2. **A system recognizer**, resolved to an explicit component. Never bound
   implicitly: this app declares its own always-failing `AlfredRecognitionService`
   (a VoiceInteractionService must name one in its own package), and when Alfred
   is the default assistant the system's default recognizer can point straight at
   that stub. `createSpeechRecognizer(ctx)` with no component would bind us to
   our own dead end on every press. Skipped on the lock screen.
3. **Record and upload** — the original path: MediaRecorder + adaptive VAD, the
   `.m4a` posted to `/chat/voice`, transcribed by Whisper on the GPU box. Used
   when nothing else is available, when a recognizer fails *before* any speech
   (one re-arm, never a loop), or when forced.

**Seeing what was heard.** Paths 1 and 2 stream interim results, so the words
appear on the overlay as you speak (`onPartialResults` → `MicWaveView.setPartial`).
Path 3 cannot — Whisper transcribes a finished recording, so the text does not
exist until the upload answers. It comes back in `/chat/voice`'s `{ok, text}`
response, which this path read the status code of and discarded, so the Whisper
route showed "Enviando…" and closed without ever saying what it heard. It now
draws the transcript on arrival and holds the overlay `TRANSCRIPT_HOLD_MS`
(counted from *then*, not from `beginSending` — the upload has already spent
longer than the normal floor). A 2xx carrying no text at all now says "No te
catch that" instead of closing in silence, which looked identical to success.

Behind the lock screen none of this appears, deliberately: `setPartial` is a
no-op when secure, because the waveform gives nothing away and readable words
would.

Failures are remembered as expiry timestamps, not booleans — a phone that later
downloads the Spanish pack heals itself. Language ladder: `es → es-ES → es`,
but for the **on-device** path that ladder is only a preference order: the tag
actually sent is whichever installed language the probe found, matched on the
primary subtag. `es` is not a locale any on-device model ships as, so asking
for it literally is asking to fail — the same lesson HomeCore learned from the
other side, where faster-whisper rejects `es` and gets `es`.

**If it's ever wrong**, the chat's 🐞 Debug submenu has a **🎙️ Voz** selector
(`auto | on the phone | the system recogniser | Whisper`), a line under it
showing what the recognizer last said it has installed, and **♻️ Limpiar estado
de voz** (which also re-probes). All only appear inside the app.

**Reading the log without a cable.** `AppLog.report()` — as opposed to plain
`log()` — also ships the line to HomeCore (`POST /chat/applog`), where it is
readable over HTTP:

```
curl -sk -H "X-Debug-Key: $DEBUG_API_KEY" \
  "https://hub.home:21001/debug/applog?user=user1&limit=100"
```

and mirrored into the container log as `applog:` lines. Every decisive voice
line reports: the chosen path, the probe's answer, each error code with
`spoke`/`onDevice`/`lang`/`locked`, and the result length (never the transcript
— that is the family's, and it is already in the chat). This exists because the
assist overlay lives about two seconds, usually behind a lock screen, and `adb
logcat` needs a cable and a person standing there; a week of failures went
undiagnosed for exactly that reason. If it ever logs `stub recognizer bound`,
something regressed to implicit binding.

**Required manifest bit:** the `<queries>` block for
`android.speech.RecognitionService`. Without it `queryIntentServices` returns
only our own stub on API 30+, and voice silently falls back to Whisper forever
with nothing in the log.

## Reading other apps' notifications

`notif/NotificationRelayService` is a `NotificationListenerService`: it sees
notifications from other apps, relays them to HomeCore so Alfred can act on them,
and can answer them through the notification's own reply action — the same
mechanism smartwatches and Android Auto use.

Two gates, both off by default, both enforced by HomeCore (`notifications.db`)
rather than by a prompt:

| gate | meaning |
|---|---|
| **leer** | Alfred is told about this app at all. Until it's on, the phone sends only the app's identity (package + label) so it can be offered in the settings list — never the title or text. |
| **responder** | Alfred may answer through the notification. `/chat/notifications/reply` re-checks it on every call, so a prompt-injected Alfred still can't answer an app that wasn't ticked. |

Inside those limits the user writes free-text rules ("responde a Sam sobre
times, never about money, ask me if unsure") that Alfred applies as
judgement. Both live in the chat's **Apps → 🔔 Notificaciones** panel, along
with recent activity and what Alfred answered.

Setup on a phone: open that panel and tap **Activar acceso a notificaciones** —
Android only grants this from its own settings screen, there is no runtime
permission dialog. Then tick the apps. Nothing is read until you do.

Worth knowing:
- **A notification's content is untrusted input.** It reaches an agent that has
  tools, so both the system prompt (`_notif_deliver`) and the skill's rules tell
  Alfred it is a message from a stranger, not an instruction. The per-app
  toggles are what actually bound the damage.
- Replies only work while the notification is still live on the phone — Android
  revokes the PendingIntent once the app dismisses it. HomeCore ages them out
  after an hour (`NOTIF_REPLY_MAX_AGE_S`).
- Ongoing, group-summary, local-only and progress/transport/service
  notifications are dropped on the device; identical content within 2 minutes is
  deduped, and a single app is limited to `NOTIF_MAX_PER_APP_PER_MIN` (6)
  hand-offs per minute so a lively group chat can't become a hundred LLM turns.

## Managing / removing

```bash
docker compose exec chat-proxy python manage_ntfy.py list
docker compose exec chat-proxy python manage_ntfy.py remove <username>
```

`remove` only deletes home-chat's copy of the topic/token mapping — it does
**not** revoke the ntfy account or token itself. To fully cut a device/person
off, also run `ntfy token remove <ntfy-username> <token>` (or
`ntfy user remove <ntfy-username>`) on the ntfy server host.

## Troubleshooting

### New account added but `manage_ntfy.py list` doesn't show it

`ntfy_config.json` on the VPS (`~/.local/share/home-stack/cloud-proxy/vps/data/ntfy_config.json`) is a
plain read-write file, separate from `users.json`/`devices.db`. If Docker
hasn't created it yet (first deploy after this feature was added), check for
the same directory-instead-of-file footgun documented in
`docs/proxy-device-enrollment.md` for `devices.db`:
```bash
ls -la ~/.local/share/home-stack/cloud-proxy/vps/data/
# if ntfy_config.json shows as a directory (drwxr-xr-x):
# `&&`, not two lines: if it is a real file rather than an empty directory,
# rmdir fails and the redirect below would blank every member's topics and
# tokens. Those tokens are shown once at creation and have to be re-minted.
rmdir ~/.local/share/home-stack/cloud-proxy/vps/data/ntfy_config.json \
  && echo '{}' > ~/.local/share/home-stack/cloud-proxy/vps/data/ntfy_config.json
docker compose up -d --force-recreate
```

### App never shows the "notifications active" tray notification

- Confirm the user is actually logged in (the endpoint 401s otherwise — the
  service just stops itself rather than retrying forever).
- Confirm `manage_ntfy.py list` shows a config for that username.
- On Android 13+, confirm the notification permission was actually granted
  (Settings → Apps → Alfred → Notifications) — it's requested at first launch
  alongside mic/camera, but can be denied or later revoked.

### Notifications stop after a while

Some OEM battery optimizers (Xiaomi/Huawei/Samsung "aggressive" modes) kill
foreground services anyway despite `START_STICKY`. If this happens
repeatedly on one phone, exempt Alfred from battery optimization for that
device (Settings → Battery → Alfred → Unrestricted).
