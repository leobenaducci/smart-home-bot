---
name: memory
description: Search past conversations in the agent's history log, and tell this conversation's memories from other conversations'. Use when asked what is remembered about somebody or something, or to remember something for later.
always: true
---

# Memory

## Structure

- `SOUL.md`, `USER.md`, `memory/MEMORY.md`: **managed by Dream.** Do NOT edit them; outdated
  information is corrected when Dream runs next (`/dream-log` shows its activity).
- The history log (the exact `History log` path from the system prompt): append-only JSONL, not
  loaded into context. That path belongs to the agent workspace, which can differ from the
  current project.

## Search Past Events

Search the history log with an available text-search tool (prefer the built-in `grep`). Each
line is a JSON object with `cursor`, `timestamp`, `content` and the conversation it was written
in: **`session_key`** (older entries: `session`). Retrieve entries on demand by topic or date,
and inspect neighbouring entries when context matters.

### Which conversation a memory came from

The conversation key is the difference between remembering and confusing two things, and it
has to be read, not ignored:

- **An entry whose key matches the current conversation is this conversation.** Use it freely.
- **An entry from any other conversation is a different one**: another chat, a notification
  triage (`ev-notif`), a task reminder (`ev-task`), a location event (`ev-geo`), or yesterday.
  It is background, and it is never the antecedent of "this", "that", "the earlier one" or
  "when it is ready".

**The most recent entry is almost always the conversation just left**, because that is what
was written last. That makes it the most tempting and the most wrong thing to answer from.

**If this conversation is empty and the message refers to something, ask.** "Which one?" costs
one turn; guessing costs two and gives a confident answer to a question nobody asked.

- For broad searches, start with `output_mode="count"` or the default `files_with_matches`
  mode before expanding to full content.
- Use `output_mode="content"` plus `context_before` / `context_after` for the exact lines; the
  whole JSON line comes back, so the conversation key is right next to what it says. A `tail`
  of the file is the one shape that hides it.
- Use `fixed_strings=true` for literal timestamps or JSON fragments; `head_limit` / `offset`
  to page through long histories; `exec` only as a last resort.
