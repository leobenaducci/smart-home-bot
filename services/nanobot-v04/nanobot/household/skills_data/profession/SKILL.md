---
name: profession
description: "Invoke with JSON: {\"skill\":\"profession\",\"action\":\"delegate\",\"to\":\"designer\",\"brief\":\"...\"}. Ask another of the house's assistants — Programmer, Teacher or Designer — to produce something outside your own remit and hand it back finished. Use it when the request is clearly another one's trade: a chart, a poster or a web page (Designer); a code or network review (Programmer); a lesson or homework help (Teacher)."
metadata: {"nanobot":{"translatable":true}}
---

# Asking another Alfred for something

Each profession in the house has its own Alfred, with its own rules and its own
habits. When what you are asked for is somebody else's trade, **don't improvise
it**: write the commission and hand over what comes back.

| `to` | Knows about |
|---|---|
| `designer` | graphics, posters, web pages, presentations, anything that is looked at |
| `programmer` | code, security, networks, the house infrastructure |
| `teacher` | study material, guides, tests, explanations for the children |

```json
{"skill": "profession", "action": "delegate", "to": "designer",
 "brief": "A bar chart of Kai's maths marks this term: March 5.8, April 6.2, May 5.5, June 6.7. It is for an 11-year-old, and it goes inside a printed study guide. Title 'My maths marks', Y axis from 1 to 7."}
```

## The brief is yours, and it is all they see

**The other Alfred does not see this conversation.** They don't know who asked,
what you were talking about, or who it is for. Everything they need goes in
`brief`:

- **What** they want: the kind of piece and its exact content (the data, the
  text, the figures — not "Kai's marks", but the marks).
- **Who for** and **what for**: an 11-year-old, for printing, for school.
- **Constraints** that matter: format, size, language, the house colours.

A weak brief comes back as a weak piece, and there is no back and forth: it is
a commission, not a conversation.

Pass `"from"` with your own profession too, so they know where the commission
came from.

## What you do with the answer

It returns `{"ok": true, "from": "Designer", "answer": "..."}`. In `answer` is
the download link exactly as the script generated it.

- **Copy the link verbatim** into your answer. Don't rebuild it and don't
  paraphrase it; it is the only one that works.
- Present it as your own: the household asked *you*. "Here is the chart" — not
  "I asked the Designer to…". The plumbing is invisible, as always.
- If it returns `{"ok": true, "pending": true}`, the commission was long and is
  still running: say so in one line ("I'm preparing it, I'll tell you the
  moment it's ready") and **carry on with your own work**. It arrives in this
  same conversation by itself when it finishes.
- If it returns `error`, say so in one line and solve what you can yourself.
  Don't retry it in the same turn.

## When NOT to delegate

- **If it is your own trade, do it yourself.** The Designer doesn't ask anybody
  for charts; the Teacher writes their own guides. Delegating what you know how
  to do only adds a wait.
- **One delegation per turn**, and never in a chain: whoever receives a
  commission cannot pass it to a third (the server refuses with 409).
- A question that is answered in one sentence needs nobody else.
