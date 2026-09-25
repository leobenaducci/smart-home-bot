---
name: memory
description: Two-layer memory system with Dream-managed knowledge files. Use when asked what is remembered about somebody or something, or to remember something for later.
always: true
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style. **Managed by Dream.** Do NOT edit.
- `USER.md` — User profile and preferences. **Managed by Dream.** Do NOT edit.
- `memory/MEMORY.md` — Long-term facts (project context, important events). **Managed by Dream.** Do NOT edit.
- `memory/history.jsonl` — append-only JSONL, not loaded into context. Prefer the built-in `grep` tool to search it.

## Search Past Events

`memory/history.jsonl` is JSONL format — each line is a JSON object with
`cursor`, `timestamp`, `content` and **`session`**.

### `session` — which conversation a memory came from

`session` is the conversation the entry was written in. It is the difference
between remembering and confusing two things, and it has to be read, not
ignored:

- **An entry whose `session` matches the current one is this conversation.**
  Use it freely.
- **An entry from any other session is a different conversation** — another
  chat, a notification triage (`ev-notif`), a task reminder (`ev-task`), a
  location event (`ev-geo`), or yesterday. It is background, and it is never
  the antecedent of "this", "that", "the earlier one" or "when it is ready".

**The most recent entry is almost always the conversation just left**, because
that is what was written last. That makes it the most tempting and the most
wrong thing to answer from. It has happened: a new conversation opened with
"tell me when it is ready", the last entries were about comparing inference
hardware, and the answer was a report on inference hardware — when the question
was about a benchmark.

**If this conversation is empty and the message refers to something, ask.**
"Which benchmark?" costs one turn. Guessing costs two and gives a
confident answer to a question nobody asked.

- For broad searches, start with `grep(..., path="memory", glob="*.jsonl", output_mode="count")` or the default `files_with_matches` mode before expanding to full content
- Use `output_mode="content"` plus `context_before` / `context_after` when you need the exact matching lines
- Use `fixed_strings=true` for literal timestamps or JSON fragments
- Use `head_limit` / `offset` to page through long histories
- Use `exec` only as a last-resort fallback when the built-in search cannot express what you need

Examples (replace `keyword`):
- `grep(pattern="keyword", path="memory/history.jsonl", case_insensitive=true)`
- `grep(pattern="2026-04-02 10:00", path="memory/history.jsonl", fixed_strings=true)`
- `grep(pattern="keyword", path="memory", glob="*.jsonl", output_mode="count", case_insensitive=true)`
- `grep(pattern="oauth|token", path="memory", glob="*.jsonl", output_mode="content", case_insensitive=true)`

Whatever the search, `output_mode="content"` returns the whole JSON line, so
`session` is right there next to what it says. A `tail` of the file is the one
shape that hides it — it looks like a list of facts and reads like continuity.

## Important

- **Do NOT edit SOUL.md, USER.md, or MEMORY.md.** They are automatically managed by Dream.
- If you notice outdated information, it will be corrected when Dream runs next.
- Users can view Dream's activity with the `/dream-log` command.
