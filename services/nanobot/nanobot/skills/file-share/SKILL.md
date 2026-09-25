---
name: file-share
description: "Invoke with JSON: {\"skill\":\"file-share\",\"action\":\"...\"}. Save, download, copy and SHARE files on the family SMB share (compute.home/share). Each user has a personal folder; admins (Alex, Sam) can access all folders. Actions: list_files([path]) | download_file(remote_path, [local_path]) | upload_file(local_path, [remote_path]) | save_text(content, remote_path) | copy_file(src_path, dst_path) | move_file(src_path, dst_path) | make_folder(path) | delete_file(path) | share_with(path, users) | unshare(path, [user]) | list_my_shares() | list_shared_with_me([path]) | download_shared(path). Use when asked to save, fetch or share a file with the family."
metadata: {"nanobot":{"translatable":true}}
---

# File Share

The house has a shared SMB folder at `compute.home/share` with one sub-folder per family member (`user1`, `user2`, `user3`, `user4`, `user5`) plus a common `familia` folder everyone can read and write. Use this skill to save generated content there, fetch files from it, share a file with another family member, or copy files between folders.

To use this skill, output the JSON invocation block as plain text in your
response — the system intercepts and executes it automatically. Do NOT use
exec, echo or curl for this skill, and do NOT write the Python yourself.
An empty file list is a valid answer: report it, don't debug.

## Paths

- All `path` arguments are **relative to the share root**, e.g. `user1/docs/report.pdf`.
- Paths that do not start with a user folder (or `familia`) are treated as relative to **your user's own folder**, so `notas.txt` means `<your-folder>/notas.txt`.
- **What you make for the person goes in `alfred/`** inside their folder — `alfred/report.md` means `<your-folder>/alfred/report.md`. That sub-folder is what their chat shows as "My files", and it keeps what you generated apart from what they put on the share themselves. Their own files stay where they are; never move them there.
- `familia/…` is the common folder: everyone can read **and** write there.
- Your user's folder comes from the `FILE_SHARE_FOLDER` environment variable. Admin users (`FILE_SHARE_ADMIN=1`, Alex and Sam) can read/write any folder; everyone else is restricted to their own folder — the skill enforces this and returns an error otherwise.
- Admins can pass `"/"` as `path` to list the share root (all user folders).

## Actions

**List a folder** (defaults to the user's own folder):
```json
{"skill": "file-share", "action": "list_files", "path": ""}
```

**Download a file from the share into the workspace**, when you need to read or process it yourself (default destination `~/.nanobot/workspace/media/<name>`; to *deliver* it you do not need this at all — see Entregar un archivo):
```json
{"skill": "file-share", "action": "download_file", "remote_path": "user1/fotos/perro.jpg"}
```

**Upload a local file to the share** (e.g. something you built in the workspace; `remote_path` defaults to `alfred/<same name>` in the user's folder):
```json
{"skill": "file-share", "action": "upload_file", "local_path": "/home/nanobot/.nanobot/workspace/informe.md", "remote_path": "alfred/informe.md"}
```

**Save generated text directly to the share:**
```json
{"skill": "file-share", "action": "save_text", "content": "…texto generado…", "remote_path": "alfred/recetas/pastel.md"}
```

**Copy or move a file between folders on the share:**
```json
{"skill": "file-share", "action": "copy_file", "src_path": "user1/docs/manual.pdf", "dst_path": "user2/docs/manual.pdf"}
{"skill": "file-share", "action": "move_file", "src_path": "user1/tmp/a.txt", "dst_path": "user1/archivo/a.txt"}
```

**Create a folder / delete a file (or empty folder):**
```json
{"skill": "file-share", "action": "make_folder", "path": "user1/proyectos"}
{"skill": "file-share", "action": "delete_file", "path": "user1/tmp/borrador.txt"}
```

## Sharing with other family members (share store)

Sharing is a **grant**, not a copy: the file stays in your folder, the recipient
gets read-only access to it in their "Compartido conmigo" panel plus a push
notification. Grants live in HomeCore's share store — never copy a file into
someone else's folder to "share" it.

**Share a file/folder with one or more people** (names or login ids; `user4`,
`kai`, and the words for mum and dad… all resolve):
```json
{"skill": "file-share", "action": "share_with", "path": "user1/alfred/documents/report.docx", "users": ["user2", "user3"]}
```

**Stop sharing** (omit `user` to revoke from everyone):
```json
{"skill": "file-share", "action": "unshare", "path": "user1/alfred/documents/report.docx", "user": "user3"}
```

**What am I sharing / what was shared with me:**
```json
{"skill": "file-share", "action": "list_my_shares"}
{"skill": "file-share", "action": "list_shared_with_me"}
{"skill": "file-share", "action": "list_shared_with_me", "path": "user2/recetas"}
```

**Deliver a file someone shared with you in chat** (fetches a local copy and
returns a `download_link`):
```json
{"skill": "file-share", "action": "download_shared", "path": "user2/recetas/pastel.md"}
```

## Delivering a file

Every save action — `save_text`, `upload_file`, `copy_file`, `download_file`,
`download_shared` — hands back a `download_link`. **Paste that link verbatim.**
It points at the file where it lives on the share, and the person opens it from
the chat; there is no copying anywhere first.

```
Saved it to user1/alfred/report.md: [report.md](download:user1/alfred/report.md)
```

A `download:` link only opens what its reader may open: their own folder,
`familia`, and anything shared with them. A link to somebody else's folder, or
to a path in your workspace, **does not fail when you write it — it fails when
they click it**, and from your side it looks like it worked. So never write one
from memory: use the `download_link` you were given.

That link already handles awkward names for you. A file called `Factura
(1).pdf` comes back as `[Factura (1).pdf](<download:user1/alfred/Factura (1).pdf>)`
— angle brackets, because a plain markdown target ends at the first `)`.
Rewriting it by hand is how that breaks.

## Rules

- **Everything you generate for the user goes in `alfred/` inside their folder.**
  `.docx` documents are filed automatically by the `document` skill
  (`<your-folder>/alfred/documents/…`); for anything else you produce (notes,
  lists, summaries, CSVs) use `save_text` — or `upload_file` for a file you
  already built in the workspace — and confirm the saved path in your reply.
- **To share something with another person, always use `share_with`** — never
  `copy_file` into their folder. Copying duplicates the file, leaves them with a
  stale version, and sends no notification. (`copy_file`/`move_file` are for
  reorganizing files *within* what you already have access to.)
- Saving, copying and moving hand back a link too, but they are **side effects,
  not deliveries** (`"deliver": false` in the result). Paste that link when the
  person wants the file; when they asked you to tidy a folder, just say what
  moved. Nothing is sent on your behalf for those.
- Before referencing a remote file, use `list_files` to confirm it exists, unless the path is known from the conversation.
- `delete_file` only removes files or **empty** folders. Never delete anything the user did not ask to delete.
- Non-admin users cannot touch other users' folders; do not attempt to work around this.
