---
name: menu
description: "Invoke with JSON: {\"skill\":\"menu\",\"action\":\"...\"}. The household's weekly menu: what is for lunch and dinner each day. For everyone: list_menu([week]) | request_dish(dish,[day],[meal],[note]) to ask for a dish. Adults only (the server rejects others with 403): set_menu(dish,day,meal,[note]) | clear_menu(day,meal) | approve_dish(entry_id,[day],[meal]) | reject_dish(entry_id,[note]). Use it when a message is about meals for the week: what is for dinner, planning a day, or asking for a dish."
metadata: {"nanobot":{"translatable":true}}
---

# The week's menu (lunches and dinners)

One menu shared by the whole house, like the board in the kitchen. Seven days ×
two meals (lunch and dinner). It lives in HomeCore.

To use this skill, write the JSON block as text in your answer — the system
intercepts it and runs it. Don't use exec/curl and don't write the Python.

## Who can do what (the server enforces it)

- **The parents** decide the menu: `set_menu`, `clear_menu`, and accepting or
  rejecting requests.
- **Children can only _ask_** for a dish with `request_dish`. It sits as a
  request with their name on it and **an adult has to accept it**. If a child
  tells you "put pizza on Friday", use `request_dish` — even if you call
  `set_menu`, the server turns it into a request anyway.
- **Anybody** can look at the menu.

## See the menu

```json
{"skill": "menu", "action": "list_menu"}
{"skill": "menu", "action": "list_menu", "week": "2026-08-03"}
```
Returns `days` (seven, each with `date`, `weekday`, `is_today` and its two
`meals` with the `entry` set or `null`), `requests` (asked for and not yet
decided), `suggestions` (dishes that repeat) and `today`.

**For "what's for lunch today?" call `list_menu` and read the day with
`is_today`.** Don't invent the dish: if the `entry` is `null`, simply say it
hasn't been decided yet.

## Set a dish (parents)

`day` accepts a date (`2026-07-31`), the name of the day (`friday`, within the
week you are looking at) or today/tomorrow. `meal` is `lunch` or `dinner`.
```json
{"skill": "menu", "action": "set_menu", "dish": "Roast chicken", "day": "friday", "meal": "lunch"}
{"skill": "menu", "action": "set_menu", "dish": "Spaghetti", "day": "today", "meal": "dinner", "note": "with bolognese"}
{"skill": "menu", "action": "clear_menu", "day": "friday", "meal": "dinner"}
```
Putting something where there was already something else replaces it — there is
only one menu.

## Ask for a dish

The day and the meal are **optional**: somebody can ask for something at an
exact moment or simply "for this week", and the adult decides when on accepting
it.
```json
{"skill": "menu", "action": "request_dish", "dish": "Pizza", "day": "friday", "meal": "dinner"}
{"skill": "menu", "action": "request_dish", "dish": "Sushi"}
{"skill": "menu", "action": "request_dish", "dish": "Hot dogs", "requester": "user4"}
```

## Accept / reject requests (parents only)

If the request came without a day or a meal, give them here — without both it
cannot join the menu.
```json
{"skill": "menu", "action": "approve_dish", "entry_id": 12}
{"skill": "menu", "action": "approve_dish", "entry_id": 12, "day": "saturday", "meal": "dinner"}
{"skill": "menu", "action": "reject_dish", "entry_id": 12, "note": "not this week"}
```

## Rules

- If a child asks for something, use `request_dish` and explain kindly that an
  adult has to accept it. Never promise them it will happen.
- If the server answers 403, it is because that action is parents only: explain
  it kindly and don't work around it.
- Don't invent an `entry_id`: run `list_menu` first if you need one.
- Don't invent dishes and don't "fill in" empty days. A day that isn't decided
  is said as it is; if a parent asks you for ideas, offer them in the chat and
  only write into the menu what they confirm.
- The menu and the shopping list are different things: if they ask you to buy
  the ingredients for a dish, that goes through the `grocery` skill.
- Answer in the person's own language.
