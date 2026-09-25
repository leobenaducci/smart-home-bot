# Handing the backup to an external uploader

For whoever maintains the job that copies this house off-site. It describes
what this stack *produces* and what it expects the uploader to do; it does not
describe any particular uploader. Read `docs/backups.md` first for what is in a
backup and why — this file is only the boundary between the two.

The short version: **three sources, one of which is new.**

| Source | What it is | Size here | Churn |
|---|---|---|---|
| `backups.export.path` + `/stack` | The mirror. Every essential path, refreshed in place. | ~97 MB | a few MB/day |
| `{paths.media}/cameras` | Recordings. Already a directory of finished files. | tens of GB | append-mostly |
| `{paths.media}/paperless` | Scanned documents. Already a directory of finished files. | ~13 MB | append-only |

Read the real values rather than copying the table. `./home-stack check` names
the config file actually in use in its first lines — the copy in the checkout
is only the seed the deployer reads `paths.config` out of — and `paths:` and
`backups.export:` in that file are the authority:

```bash
./home-stack check | head            # names the config in use
./home-stack backup --list           # where archives live, and what verified
```

The mirror also writes `<export>/stack/backup.json`, which names every path it
captured, everything it deliberately skipped, and — under `not_mirrored` — the
bulk paths the uploader is expected to read directly. That file is the
authority. Do not keep a second copy of the path list in the uploader's config;
this stack derives its inventory from `deploy/manifest.yml` and checks it two
ways in `deploy/test_backup.py`, and a hand-maintained duplicate is a second
thing to fall out of step. The failure is silent — a path nobody listed is a
service nobody backed up.

## The mirror is plaintext. Do not upload it as it is.

`<export>/stack` holds `smart-home-bot.env`, `users.json` and every bcrypt hash
in the house, in the clear, permanently. It is `0700` and it is a **staging
area**.

This is deliberate, and it is the trade that makes the whole thing work. The
archives in `paths.backups` are AES-encrypted, which is why an uploader could
safely mirror that directory — but an archive is a different 60-odd MB file
every night whether or not anything changed, so uploading them means uploading
all of it, forever. Per-file encryption is what makes an upload incremental:
one changed file becomes one changed blob. A tarball is the shape that cannot
do that.

So the uploader owns the encryption step:

```
<export>/stack/  →  per-file encrypt  →  vault  →  upload the vault
   plaintext          e.g. gpg             ciphertext
   0700, local        one file in,
                      one file out
```

**Encrypt per file, upload the ciphertext, never the staging tree.** If the
uploader uses an allowlist for what may leave the machine — and it should —
that list must not be able to match a plaintext file from this directory.

## Ordering

The mirror must be refreshed before the uploader reads it, or every blob is a
day stale. One command does both the mirror and the occasional archive:

```bash
./home-stack backup --auto
```

`--auto` refreshes the mirror every run and writes a full archive only when
`backups.full_every_days` has elapsed. Run it from cron shortly before the
upload job, the same way a vault refresh runs before the job that ships the
vault.

If it fails, the previous mirror is still on disk and still complete — the
refresh writes its metadata last precisely so a half-updated tree is never
described as a finished one. **Ship yesterday's rather than shipping nothing.**
A failed refresh is a reason to alert, not a reason to skip the upload.

## Retention is now the uploader's problem too

This stack keeps a tiered set locally (`keep`, `keep_weekly`, `keep_monthly` —
recent runs, plus one a week, plus one a month, unioned). That bounds
`paths.backups` on this disk and nothing else.

The mirror **has no history at all**. Delete something today and tomorrow's
refresh deletes it from the mirror too, and then from wherever the mirror was
uploaded. That is the one thing the old directory-of-archives gave you for
free, and switching to a mirror gives it up.

So the off-site copy needs its own answer, and there are only two that work:

1. **Versioning or soft-delete on the destination**, so an overwritten or
   removed blob is recoverable for some window. This is the better answer:
   it is the destination's job and it cannot be forgotten.
2. **Keep uploading the periodic full archives** from `paths.backups` as well.
   They are already encrypted, so they need no vault step, and at
   `full_every_days: 7` there is roughly one a week rather than one a night.

If the uploader takes option 2, that job needs a retention policy of its own.
A job with no prune uploads every archive ever made and keeps it: at 62 MB a
night that is ~1.9 GB/month and ~22 GB/year, growing forever, to protect ~100
MB of state. Whatever the destination's equivalent of "keep N days" is, set it.

## Things not to do

**Do not add a second deleter for the recordings.** Local retention belongs to
this stack — `services.home-cameras.recording_retention_days` deletes a clip
together with its `.json` verdict sidecar, which an outside deleter working on
file age will not. Two deleters on one tree is one too many, and the timer
becomes a shredder the first time the mirror alone breaks. Mirror what the
stack keeps; let the stack decide what exists.

**Do not mirror `{media}/cameras/pending/`.** It is the recorder's write
target: every clip lands there unjudged and the review worker then bins it,
promotes it into a month folder, or files it for review. It is the one
directory where a file is guaranteed to move, so mirroring it uploads every
kept clip twice.

**Do not point the uploader at `paths.backups` *and* the mirror** expecting
them to be different data. They are the same inventory in two shapes. Pick the
mirror for the incremental copy and the archives for point-in-time, or you pay
for both.

**Do not put the mirror anywhere a live path lives underneath it.** The refresh
runs `rsync --delete`. `export.path` is checked at every run and refuses a
directory containing `paths.state`, `paths.media` or any other configured root,
and refuses anything inside the deploy tree — but the uploader should not be
pointed at a parent of it either, for the same reason in the other direction.

## Restoring, when it comes to that

The mirror restores by hand: the tree under `<export>/stack/<service>/` is the
same layout an archive unpacks to, and `backup.json` says which live path each
one came from. `{paths.state}/paperless-db` is the exception — it is a
`pg_dump`, replayed with `psql -v ON_ERROR_STOP=1 --single-transaction`, not a
directory to copy back.

The archives restore with the tool, which is the better path when it is
available:

```bash
./home-stack backup --list                              # what exists
./home-stack backup --verify <id>                       # does it actually restore
./home-stack backup --verify <id> --only home-paperless # just that service
./home-stack restore <id> --only home-paperless --confirm
```

**The encryption key for the archives is inside the archives.**
`BACKUP_ENCRYPTION_KEY` lives in `smart-home-bot.env`, which every archive
contains. Keep a copy somewhere else, or an off-site archive is noise. The same
applies to whatever key the uploader's vault step uses.
