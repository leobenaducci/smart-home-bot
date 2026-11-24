---
name: family-message
description: "Invoke with JSON: {\"skill\":\"family-message\",\"action\":\"...\"}. Talk to ANOTHER household member's assistant. send_family_message(to, text, [path]) sends a message: it arrives in that person's chat and rings their phone — use it for \"tell X\", \"send this to X\", \"let X know\". ask_family(to, question) ASKS their assistant something and returns the answer without interrupting that person. Use it whenever the request is directed at somebody else in the house rather than at the person speaking."
# `always: true`: the body below is in every prompt, so this description
# carries triggers rather than the API -- the body and the skill's own code
# already hold that. Measured 2026-09-10: every model that fumbled this
# skill called it as a function (`unknown:<name>`) because the invocation
# convention was in a file it never read. If demoted to on-demand, the
# description has to grow back.
metadata: {"nanobot":{"translatable":true,"always":true}}
---

# Family message (Alfred to Alfred)

Every member has their own Alfred. This hands a message from **your** user to
**another member's** Alfred, who delivers it in their chat and pings their
phone. For "tell mum that...", "send Robin the list".

Write the JSON block as plain text in your reply; the system intercepts and runs
it. Never exec, echo, curl, or Python you write.

## send_family_message

`to` takes a first name, a nickname, the words for mum or dad, or a login id.
`path` optionally attaches a file **from your own folder** on the share: it is
not copied, the recipient gets read-only access plus a download link.

```json
{"skill": "family-message", "action": "send_family_message", "to": "user2", "text": "I'll be late for dinner."}
{"skill": "family-message", "action": "send_family_message", "to": "user3", "text": "The summary you asked for.", "path": "user1/alfred/documents/summary.docx"}
```

Documents you generate already live on the share, so attach their path directly. A
file that exists only in the workspace must go through the `file-share` skill's
`upload_file` first; attach the `remote_path` it returns.

## ask_family

**Check whether you can see it yourself first.** The whole household's chores
read directly with `list_chores` using `"user": "all"`, and that answers at
once. Asking another Alfred costs a turn on their instance and can be slow. Use
it only for what you cannot read: another person's points, or something only
their Alfred knows.

```json
{"skill": "family-message", "action": "ask_family", "to": "user3", "question": "Did you clean the litter tray yesterday?"}
```

Returns `{"answered": true, "answer": "..."}`. The person gets **nothing** -- no
chat message, no notification. It is a query between Alfreds.

- **House matters only**: chores, points, shopping, the menu. Never their
  personal things.
- **They can only READ for you.** Never ask another Alfred to complete a chore,
  redeem a prize or send something; the system blocks it. If a person needs to
  act, send them a message and let them decide.
- **One short, concrete question about a fact.** Ask only somebody who could
  know: for a rotating chore, work out whose turn it was and ask that one, not
  all four.
- `"I don't know"` or `answered: false` is an answer. **Say so plainly** rather
  than filling the gap with what seems likely. Attribute what you do get:
  "according to Robin, ...".

## Rules

- Tell your user in one short line what was sent and to whom.
- The message goes as dictated. Do not editorialize and never invent content.
- One message per request; fan out only if they asked, one call per recipient.
- `"delivered": false` means the other Alfred was unreachable. The message was
  still saved to their chat and pushed to their phone. Say so.
- You cannot read anyone else's chat. This only sends.
