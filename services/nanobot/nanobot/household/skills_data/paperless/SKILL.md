---
name: paperless
description: "Invoke with JSON: {\"skill\":\"paperless\",\"action\":\"...\"}. Search, list, view, and manage documents in Paperless-ngx. Actions: list_documents | search_documents(query) | get_document(id) | get_document_metadata(id) | download_thumb(id) | download_document(id) | list_tags | list_correspondents | list_document_types | list_storage_paths | upload_document(filepath, [title], [correspondent], [document_type], [tags]) | update_document(id, [title], [correspondent], [document_type], [tags]) | delete_document(id) | list_tasks | trash_document(id). Use when a message is about a scanned document, bill, receipt or invoice the house has filed."
metadata: {"nanobot":{"translatable":true}}
---

# Paperless-ngx

Paperless-ngx is a document management system. Use this skill to manage documents via its REST API.

To use this skill, output the JSON invocation block as plain text in your
response — the system intercepts and executes it automatically. Do NOT use
exec or curl for this skill, and do NOT write the Python yourself.
An empty result list means no documents matched: report that as the answer,
don't debug the connection.

## Configuration

Set these environment variables per bot instance:

- `PAPERLESS_API_TOKEN` — Your Paperless API token (required)
- `PAPERLESS_URL` — Paperless server URL (default: `http://paperless.home:8000`)

The bot instance must have `PAPERLESS_API_TOKEN` in `exec.allowedEnvKeys` in config.json.

## How it is used

Output the JSON block as plain text in your response. The system runs it and
hands you the result.

```json
{"skill": "paperless", "action": "search_documents", "query": "policy"}
{"skill": "paperless", "action": "list_documents"}
{"skill": "paperless", "action": "get_document", "id": 41}
{"skill": "paperless", "action": "download_thumb", "id": 41}
{"skill": "paperless", "action": "download_document", "id": 41, "filename": "Insurance policy"}
{"skill": "paperless", "action": "list_tags"}
{"skill": "paperless", "action": "list_correspondents"}
{"skill": "paperless", "action": "list_document_types"}
{"skill": "paperless", "action": "list_storage_paths"}
{"skill": "paperless", "action": "upload_document", "filepath": "/path/to/file.pdf", "title": "Electricity bill"}
{"skill": "paperless", "action": "update_document", "id": 41, "title": "New title"}
{"skill": "paperless", "action": "trash_document", "id": 41}
{"skill": "paperless", "action": "list_tasks"}
```

Search: `query` is free text over the whole content. To filter there are
`tags__id__all`, `correspondent__id`, `document_type__id` and `ordering`
(`-created` for newest first).

**Never write the `curl` yourself.** It is not only that it costs a turn: the
token goes on the command line, and `exec` writes every full command to a log
that can be read without authentication. A Paperless token was exposed exactly
that way once and all six in the house had to be rotated. The skill reads it
from the environment variable, where it is not recorded. The same goes for
writing the Python by hand.

## Rules

- Always list documents first before referencing a document ID, unless the ID is known from previous interaction.
- **`jq` is not installed** in this container — asking for it returns `jq:
  command not found` and you lose the turn. Read the JSON as it is, or pipe it
  through `python3 -m json.tool`.
- When searching, prefer `query=` for full-text search; use `tags__id__all`, `correspondent__id`, `document_type__id` for filtering.
- Upload: always check that the file exists first with `ls` or `glob`.
- PATCH uses `Content-Type: application/json` for metadata updates; POST for uploads uses multipart form.
- **When the user requests the image or file of a document, always deliver it — never just say it was found:**
  - `download_document` and `download_thumb` return a **`message`** already
    written with the link inside it. Send it exactly as it is, or paste the
    literal `download_link` into your sentence. **Don't write the path from
    memory**: the file is named the way the skill named it, not the way you
    would guess. Observed: the model wrote a link to a file that did not exist,
    with an invented name and without the `download:` prefix — two ways of
    opening nothing.
  - **Web chat**: call `download_thumb(id)` or `download_document(id)` and
    paste the `download_link` **that call returned**, character for character.
    Never write an example link or one from memory: there used to be an example
    here with a concrete id, and the model copied it verbatim, sending the
    thumbnail of the wrong document while saying it had sent the PDF that was
    asked for. An invented link points at something that does not exist or,
    worse, at another of the household's documents. The `download:` prefix is
    not optional — without it the chat resolves the path against the site and
    opens nothing.
- **Don't look at the thumbnail with `describe_image` unless they ask about the
  content.** "Find the policy" means finding it and sending it; looking at it
  adds about two minutes to an answer that was already ready. Measured: the
  Paperless API answers in 0.12s and the turn took 176s, almost all of it
  waiting on that look. You only look if they ask what it says.
  - **ntfy**: download the thumb to `/tmp/doc_{id}_thumb.jpg` and upload with `curl -u "$NTFY_CREDENTIALS" --data-binary @/tmp/doc_{id}_thumb.jpg -H "Content-Type: image/jpeg" -H "Filename: document.jpg" "https://ntfy.home/<user>"`.
