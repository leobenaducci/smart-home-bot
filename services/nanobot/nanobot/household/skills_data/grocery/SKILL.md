---
name: grocery
description: "Invoke with JSON: {\"skill\":\"grocery\",\"action\":\"...\"}. The household's shared shopping list. Use it whenever a message is about the shopping list: adding something, asking what is still missing, or saying what has already been bought."
# `always: true`: the body below is in every prompt, so this description
# carries triggers rather than the API -- the body and the skill's own code
# already hold that. Measured 2026-09-10: every model that fumbled this
# skill called it as a function (`unknown:<name>`) because the invocation
# convention was in a file it never read. If demoted to on-demand, the
# description has to grow back.
metadata: {"nanobot":{"translatable":true,"always":true}}
---

# Shopping list (the household's groceries)

One list shared by the whole house (like the board on the fridge): anybody adds
to it or marks something bought. It lives in HomeCore. Items sort themselves by
supermarket aisle.

To use this skill, write the JSON block as text in your answer — the system
intercepts it and runs it. Don't use exec/curl and don't write the Python.

## Who can do what (the server enforces it)

- **Children can only _ask_** for special items with `request_grocery`. They sit
  as a "request" with their name on it and **an adult has to approve them**
  before they join the list. If a child asks you to add something, use
  `request_grocery` (even if you call `add_grocery`, the server turns it into a
  request anyway).
- **The parents** can `add_grocery` (straight onto the list) or ask on somebody
  else's behalf with `request_grocery(..., requester="user3")`. Only they can
  `approve_grocery` / `reject_grocery`.
- **Anybody** can mark something bought (`mark_bought`) while out shopping, and
  remove their own requests.

## See the list

```json
{"skill": "grocery", "action": "list_groceries"}
```
Returns `items` (each with `id`, `name`, `qty`, `category`, `status` =
`requested`|`pending`|`bought`, and `requested_by` if it is a request) and
`suggestions` (things bought often, to offer).

## Add / request

`category` is optional; without it the server guesses (fruit-veg, meat, dairy,
bakery, pantry, frozen, drinks, cleaning, toiletries, pets, other). `qty` is
free text ("2 kg", "a dozen").

**Add what was said, straight away.** "Agregá leche" is one item called `leche`
with no `qty` -- add it, don't ask how much. A quantity goes in only when the
person gave one; asking first turns a two-second errand into a conversation,
and the bench caught eleven models doing exactly that.
```json
{"skill": "grocery", "action": "add_grocery", "name": "milk", "qty": "2 L"}
{"skill": "grocery", "action": "request_grocery", "name": "chocolate cereal", "note": "the one in the blue box"}
{"skill": "grocery", "action": "request_grocery", "name": "yoghurt", "requester": "user4"}
```

## Mark bought / remove

Accepts the name or the `item_id` (from `list_groceries`).
```json
{"skill": "grocery", "action": "mark_bought", "name": "milk"}
{"skill": "grocery", "action": "remove_grocery", "name": "bread"}
```

## Approve / reject requests (parents only)

```json
{"skill": "grocery", "action": "approve_grocery", "item_id": 12}
{"skill": "grocery", "action": "reject_grocery", "item_id": 12, "note": "we already have some at home"}
```

## Rules

- If a child asks for something, use `request_grocery` and explain kindly that
  an adult has to approve it.
- If the server answers 403, it is because that action is parents only:
  explain it kindly and don't work around it.
- Don't invent an `item_id`: run `list_groceries` first if you need one.
- A message starting with `[HomeCore system]` asking you to check the shopping
  list (for example on arriving at a supermarket) is the system asking for
  help: call `list_groceries`, count how many items are still `pending` and
  name them in a short, warm message. Do NOT invent items.
- Answer in the person's own language.
