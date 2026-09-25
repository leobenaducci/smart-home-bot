# What to back up

`./home-stack backup` writes one archive per run into `paths.backups`, and
that is the whole configuration a household needs — point whatever copies
things off this machine at that one directory.

```
./home-stack backup                  one archive of everything that matters
./home-stack backup --export         refresh the mirror, write no archive
./home-stack backup --auto           mirror now, archive only when one is due
./home-stack backup --list           what exists, and whether it verified
./home-stack backup --verify [id]    unpack it and check what came out
./home-stack backup --verify [id] --only home-paperless   check just those
./home-stack restore <id>            what it would overwrite, and nothing else
./home-stack restore <id> --confirm  put it back
./home-stack restore <id> --only home-paperless --confirm   just that service
```

What goes in is derived from this file's source — the `state:` entries in
`deploy/manifest.yml` — rather than from a list kept alongside it. Everything
else — images, containers, the deploy directory, the checkout — is rebuilt by
`./home-stack deploy`. Do not bother backing it up.

The settings, under `backups:` in `config/home-stack.yml`:

| Setting | Does |
|---|---|
| `encrypt` | AES-256 through openssl, keyed on `BACKUP_ENCRYPTION_KEY` (generate it on the admin page's Credentials screen). Every archive contains the credentials file and every password hash, so turn this on if it leaves the machine — and **keep a copy of the key somewhere this house burning down does not affect.** |
| `keep` | How many recent runs to keep, whatever their spacing. The oldest go only after a new one has been written *and* verified, so a failed run cannot make room by deleting the last good one. |
| `keep_weekly` / `keep_monthly` | The newest run in each of the last N ISO weeks, and each of the last N calendar months. An archive survives if *any* tier claims it, and the tiers overlap — today's run is usually this week's and this month's as well, and that is one file. Both `0` is the old flat behaviour exactly. |
| `full_every_days` | How often `--auto` writes a full archive. `0` means every run. |
| `export.path` | Where the mirror goes. Unset means no mirror. |

## The mirror, for an external uploader

An archive is a different file every night whether or not anything changed, so
a tool that mirrors `paths.backups` to somewhere off-site ships the whole thing
every time — 60-odd MB a night here, growing forever, to protect ~100 MB of
state that moves by a few MB a day. Retention on this machine does not help:
the blobs already uploaded are somebody else's retention problem.

`backups.export.path` writes the same inventory to the same paths every run
instead:

```
<export>/stack/          the 27 essential paths, plus paperless-db.sql
<export>/stack/backup.json   what was captured, what was not, and why
```

Measured here: two back-to-back runs differed in **7 files out of 5654**, and
1.6 MB of that 1.7 MB was the Postgres dump, which is rewritten in full every
time by nature. The same night as an archive: 62 MB.

Three things to know before pointing anything at it.

**It is plaintext.** It holds `smart-home-bot.env`, `users.json` and every
bcrypt hash, exactly as an archive does, and nothing here encrypts it — because
per-file encryption is what keeps an upload incremental, and that belongs to
whatever does the uploading. The directory is `0700` and is a *staging area*.
Encrypt per file, upload the ciphertext.

**Bulk paths are not mirrored.** `{media}/cameras` is hundreds of GB; copying
it to a second place on the same disk buys nothing a mirror-based uploader
could not get from the originals. Point it at that path directly. The mirror
lists it under `not_mirrored` in its own `backup.json`, so nothing has to
remember.

`{media}/paperless` used to be on that list and is not any more. It is 13 MB
here, not hundreds, and leaving it out meant the archive kept
`{state}/paperless-db` — every title, tag and correspondent — while dropping
the documents those entries name. A restore from that looks complete until
somebody opens a document.

**It has no history.** Delete something today and tomorrow's refresh deletes it
from the mirror too. That is what the archives are still for, and why
`keep_weekly`/`keep_monthly` matter once a mirror exists: the mirror answers
"yesterday was fine and today is not", and an archive answers "this has been
wrong since March". Set `full_every_days` and run `--auto` from cron to get
both without a full every night.

The refresh runs `rsync --delete`, so `export.path` must be an absolute
directory of its own. A path that *contains* `paths.state`, `paths.media` or
any other live root would empty it on the first run; that is refused rather
than trusted, and so is anything inside the deploy tree, which a deploy would
delete right back.

`docs/uploader-contract.md` is the other half of this: what the tool copying
this house off-site is expected to do with the three sources, where the
encryption step belongs, and what happens to retention once the copy is a
mirror rather than a directory of dated archives.

`--only` is the shape a restore usually has: the documents came back wrong, or
one person's assistant memory is gone. Without it a restore drags every other
service back to the same hour — including `users.json` and the credentials
file, so putting one service back signs the whole house out. It takes a
comma-separated list, refuses a name the archive does not hold rather than
restoring nothing quietly, and says which paths it left alone.

Two things worth knowing before you rely on it. `--verify` is not a formality:
it unpacks the archive into a scratch tree, counts the accounts in the user
store, counts the keys in the credentials file and refuses a pg_dump that does
not contain a dump. And a restore puts bytes on disk and nothing more — it
prints the services it touched, and they need deploying before anything reads
what landed.

`--verify` takes `--only` too, and the admin page's **Test a backup** card is
that: tick services, unpack into a scratch directory, change nothing live. It
answers "is the copy I would restore any good?" without a wall of output about
the 26 paths you did not ask about. A narrowed run deliberately skips the
whole-archive checks — the user store is home-core's, and demanding it while
checking home-paperless would fail every narrowed run — and deliberately does
**not** write the `verified` marker, because "home-paperless restores" is not
the claim that marker makes. Like `restore --only`, it refuses a name the
archive does not hold rather than checking nothing and reporting success.

### What it cannot capture

A member whose assistant runs under `services.nanobot.runtime: kubernetes`
keeps workspace and memory in a PersistentVolumeClaim rather than under
`{state}/nanobot`. The backup copies the host directory, which on that install
is empty, so the archive verifies and restores while containing none of that
person's assistant memory.

It is not silent about it: the run warns, the archive records it, and
`--verify` says it again from the archive rather than from today's config.
Until it is captured, treat a kubernetes member's PVC as something you back up
yourself.

### The two files it cannot capture

`config/home-stack.yml` and `secrets/smart-home-bot.env` **in the checkout** are
not in the archive: they are what the tool reads to find and decrypt an archive
in the first place. Keep a copy of both somewhere else. Their deployed
counterparts under `{config}/` — the ones the admin page edits, and the ones
that matter at run time — are included.

## Why each one

### Cannot be recovered from anywhere — back these up

| Path | What it is |
|---|---|
| `secrets/smart-home-bot.env` | Every credential. Losing it means re-issuing the model API key and regenerating the rest, which invalidates every signed-in session and every enrolled device. |
| `{config}/smart-home-bot.env` | The *deployed* copy — seeded from the one above on the first deploy, and state afterwards, because the admin page edits it. This is the one a running container reads. |
| `config/home-stack.yml` | Hosts, ports, names, who lives here. Rebuildable by hand, tedious to reconstruct exactly. |
| `{config}/home-stack.yml` | The *deployed* copy, and the same relationship the credentials file has to its seed: the admin page edits this one, and it is what a deploy reads. Enabled services, members and model choices live here. |
| `{state}/home-core/users.json` | The user store: login ids and bcrypt hashes, and the `member` each login belongs to. **The single most important file here** — without it nobody can sign in, and nothing can convert a login id into a member id or a folder, which is what every other table here is keyed on. See the three-ids table in CLAUDE.md. |
| `{state}/home-core/certs` | The portal's own TLS certificate. It is regenerated — by home-core's `pre:` hook, not the proxy's, which writes to a different volume entirely — so it is recoverable. Kept anyway, because "recoverable" here means re-trusting a new certificate on every phone and tablet in the house, and that is a worse afternoon than the few kilobytes cost. |
| `{state}/home-core/data` | The portal's databases: tasks, points, the weekly menu, shopping lists, geofences, chat titles, personas, themes. `usage.db` is here too and is the largest of them: what every assistant turn cost, what the household's own vision, speech and listening models did, and a rolling week of GPU samples. The samples are trimmed to seven days on their own clock (`GPU_KEEP_DAYS`); the usage history is kept for a year, because "what did this cost last summer" is the question the store exists to answer. |
| `{state}/home-core/history` | Every assistant conversation the portal has kept. |
| `{state}/nanobot` | Per-member assistant state and memory. **`media/` is never archived**: it is every image the assistant has ever sent or been sent, large and static, and a nightly archive was re-compressing 241 MB of unchanged files. It is named in each archive's `not_archived` meta (and in the mirror's `not_mirrored`), so a restore knows what it is not getting. |
| `{state}/nanobot-house` | The shared room assistant's state. |
| `{config}/nanobot` | Per-member agent configuration. |
| `{state}/home-voice` | **Learned IR codes.** Each one cost somebody a walk to a drawer and a button press, and nothing can re-derive them. |
| `{config}/home-voice/devices.json` | The voice panel registry and its per-device tokens. Without it every panel gets a 403 that reads like a firmware fault. |
| `{config}/alfred-app` | The Android app's signing key (`debug.keystore`), created by the APK page's first build. **Irreplaceable**: an app signed with a different key is refused as an update, so losing it means every phone uninstalls and reinstalls the app. Never in the repository, because whoever holds it can sign an update the phones accept. |
| `{config}/home-cameras` | Camera definitions, including their RTSP credentials and motion zones. |
| `{config}/home-search` | The SearXNG instance's `settings.yml`, when `services.home-search` is on. Seeded once from `services/home-search/settings.example.yml` and edited in place after, so it is the only copy of whatever you changed — and it holds the instance secret. |
| `{state}/local-proxy` | The always-on proxy's enrolled device keys and per-user notification topics. Lose it and every enrolled phone and panel is refused at login as an unknown device, whatever the password. |
| `{state}/mqtt-dashboard` | Notification bridges. |
| `{state}/paperless-db` | The Postgres database — every title, tag, correspondent and date. Without it `{media}/paperless` is a pile of files with no way to find anything in them. |
| `{media}/paperless` | The documents themselves. Captured on every run, not only with a flag: the database above indexes these files, so keeping one without the other restores a catalogue of things that are not there. |
| `{state}/ntfy` | Notification history and subscriptions. |
| `{state}/homeassistant` | The whole config directory: the YAML somebody wrote, `secrets.yaml`, `.storage` with the device and entity registries and every integration's tokens, the recorder history, and `zigbee.db` with the network key each paired device is joined on. Losing that last one means re-pairing the house one bulb at a time. |
| `{state}/n8n` | Workflows, their run history, and the `config` file holding the key every stored credential is sealed with. n8n starts perfectly well without that file and cannot decrypt one of them. |
| `{state}/nodered` | Flows, `settings.js`, and `flows_cred.json` — which is encrypted with a secret out of that `settings.js` in the same directory. Restoring one without the other gives you every node in place with every credential blank. **The palette is recorded, not copied:** `package.json` and `package-lock.json` are in the archive and `node_modules`/`.npm` are not, so a restore needs one `npm install` in the data directory before the flows will load. That was 20.7 MB of 20.8 MB, rebuildable, every night. |
| `{config}/ssh` | The keys the admin page uses to reach other hosts, if you split the stack. |
| `{config}/mosquitto` | The broker's `mosquitto.conf`. Seeded from the example on the first deploy and never touched again, so the moment anybody edits it the file is theirs and nothing regenerates it. This used to be listed as not worth keeping, on the grounds that you would only need it "if you edited it" — which is advice to a person, not something a backup can know. |
| `{config}/ntfy` | ntfy's own config directory. Empty unless a household puts a `server.yml` in it — the deploy passes ntfy its settings as environment and writes nothing here, so anything present is authored. This was listed as "written from config on deploy", which is simply not true of it. |
| `{state}/cloud-proxy` | Enrolled device public keys, if you run the optional proxy. |

`{plugins}` is in every archive and is not any service's state -- it is what
*declares* services, so nothing in the manifest could carry it. A plugin lives
outside the package tree on purpose: `sanitize.py` never looks there, a clone
of this repository never ships it, and a checkout cannot destroy it. Every one
of those is also a reason it is not in git, which makes the archive the only
copy a household has of the services it added itself.

`.git` inside a plugin is kept, for that same reason: with no remote, or with
one that unpushed commits, local branches, stashes and the reflog have not
reached, the history exists nowhere else. `*.log` inside a plugin is **not**
kept — a log records what already happened, nothing is rebuilt from it, and no
service fails to come back without one. That was 10.8 MB of one plugin's
13.0 MB here, in two unrotated files, re-encrypted and re-uploaded nightly.
Rotation in the plugin's own repository is still the better fix; this only
stops the archive paying for the lack of it.

The APK page's staging tree under `{state}` is deliberately absent, and was in
this list for one commit. It looked like a gap -- 46 MB nobody was keeping, with a signing
keystore in it -- and it is a staging tree: the APK page deletes it and
re-copies it from `services/proxy/android/` on every build, so backing it up
archives a disposable copy of git content. The signing key is not in it: it
lives in `{config}/alfred-app`, above, and is archived from there.

`{media}/paperless-consume` is deliberately absent from this list, and carries
`backup: skip` in the manifest: it is the inbox, and a file that lands there is
consumed into the two directories above and deleted within the minute. Backing
it up captures whatever happened to be mid-import — and restoring it drops
those files back into `/consume`, where Paperless ingests them a second time.

### Worth backing up, but bulky

| Path | Note |
|---|---|
| `{media}/cameras` | Recordings. Often the largest thing here by far, and often the least worth keeping for long. Decide a retention window and back up only inside it. |

### Do not bother

| Path | Why |
|---|---|
| `{state}/faster-whisper/hf_cache` | Model weights. Several GB, re-downloaded automatically. |
| `{state}/audio-cpp/models` | GGUF speech packages — 6.9 GB for the seven shipped defaults, measured. Carries `backup: skip`: they are somebody else's published files and `model_manager_v2.py` re-fetches them. It carries `env: AUDIOCPP_MODELS_DIR` for the reason the camera weights next door do — a compose default that is absolute is one `--check-contract` cannot notice, and a household that moved `paths.state` would find these on a different disk from everything else. |
| `{state}/home-cameras/models` | YOLO weights (`yolo26m`/`s`/`x`), fetched by `download_models.sh` on every deploy — the same bytes for everybody. Carries `backup: skip`. It also carries `env: CAMERAS_MODELS_DIR`, which it did not until this path was the one state mount nothing declared: the deployer exported nothing, compose fell back to its own absolute default under `/var/lib/home-stack/state`, and on a household that moved `paths.state` the weights landed on a different disk from every other state path while this table claimed otherwise. An absolute default is why `--check-contract` never noticed. |
| `{state}/paperless` | Paperless's `data/`: the search index, the trained classifier, its logs and a scheduler cursor. All rebuilt from `{state}/paperless-db` and `{media}/paperless`, and restoring an old index over a restored database is worse than having none — Paperless answers searches from it until somebody reindexes. Carries `backup: skip`. It is also the one path a normal account cannot read: the container runs as root and writes `index/*.json` 0600, which used to abandon the whole run. **This is right because this stack always runs Postgres beside it**; on a Paperless configured for SQLite the database is `data/db.sqlite3` and skipping this would throw away every title and tag. |


| `{state}/mosquitto` | The broker runs `persistence false`, so there is nothing durable in here. |
| `{state}/nanobot-shared` | One file: which models were answering 5xx at which gateway, and until when. Every assistant process on the box reads and writes it so an outage is discovered once instead of once per container. Carries `backup: skip` — the deadlines are wall-clock, so restoring an old one would route around a model that recovered hours ago, and losing it costs exactly one rediscovery. |
| `{state}/nanobot-code-workspace` | The code broker's checkouts, one directory per member. Carries `backup: skip`: every project here has a git remote, and that is the copy that matters. What has not been pushed is not this stack's to promise -- if that worries you, the fix is pushing, not archiving. |
| `vane-data` (docker volume) | Vane's own search history and uploads, when `services.home-search` is on. A named volume rather than a path, so no backup run can reach it — nothing outside Vane reads it, and a new instance rebuilds what it needs. |
| `{state}/admin` | What the admin page caches: today the model roster it fetches from models.dev and from whichever providers have a key. Nothing here is authored by a person and every file is re-fetched on demand, so an archive of it buys nothing. It lives on the host rather than in the container only so that a redeploy does not throw the roster away and send the next page load back for 4.3 MB. |
| `{state}/registry` | The in-house image registry's blob store, when `services.registry` is on. Gigabytes, and every image in it is rebuilt by pushing again. |
| `~/.local/share/home-stack` | The deploy directory. Rebuilt from the checkout every time. |

## Two databases that are not files

`{state}/paperless-db` is a live Postgres data directory (`postgres:16`,
mounted at `/var/lib/postgresql/data`); a file-level copy of a running cluster
is torn across its relation files and WAL and will usually refuse to start on
restore. This is why its manifest entry carries `backup: postgres`: the backup
runs `pg_dump --clean --if-exists` through the container, and the restore
replays it with `psql -v ON_ERROR_STOP=1 --single-transaction`, so a dump that
does not apply leaves the database as it was rather than half-replayed.

The portal's SQLite databases under `{state}/home-core/data` have the milder
version of the same problem and are still copied as files. If you want a
guaranteed-consistent copy, stop home-core for the length of the run or
snapshot the filesystem. Everything else here is safe to copy while the stack
is running.

## Restoring

1. Clone the package and run `./home-stack install`.
2. Put `config/home-stack.yml` and `secrets/smart-home-bot.env` back **before**
   deploying — the installer will not overwrite either if they already exist,
   and generating fresh secrets over a restored user store locks everyone out.
   These are the two files the archive cannot contain; see above.
3. Copy the archives back into `paths.backups`, then
   `./home-stack backup --list` to see them and
   `./home-stack restore <id>` to see what one would overwrite.
4. `./home-stack restore <id> --confirm`. Stop the stack first if you can: a
   container still holding a database open can write over part of what lands.
   Putting one service back rather than all of them is `--only <service>`.
5. `./home-stack check`, then `./home-stack deploy` — restoring puts bytes on
   disk, and a service only reads them when it is redeployed.

The order matters in one place only: restore the secrets before the first
deploy. Everything else can be put back afterwards and picked up on the next
one.

## More than one machine

State is fetched from the machine that owns it, not from wherever the command
was typed. Each `state:` entry inherits its service's `role:`, and the copy
goes through the same `Target` the deployer uses — so a role pointing at
`127.0.0.1` is a local copy, and a role with a real address is rsync over ssh.
The Postgres dump and its replay run on that machine too.

This matters because the failure was silent. Before it, a stack with
`home-paperless` on its own box backed up nothing of it: every path read as
absent, the archive verified green because absent paths are skipped, and
retention then deleted the last backup that did contain the documents.

A role named in the manifest but missing from `hosts:` is refused rather than
guessed at.
