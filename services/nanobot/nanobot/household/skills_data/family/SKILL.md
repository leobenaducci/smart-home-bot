---
name: family
description: "Invoke with JSON: {\"skill\":\"family\",\"action\":\"...\"}. The shared household directory and memory: each member's details — relationship, birthday, school or work and their addresses, likes, hobbies, allergies, sizes, traits. Actions for everyone (read): list_family | get_profile(person) | search_family(query). Adults only, or any person about THEMSELVES (the server rejects the rest with 403): set_fact(person,label,value) to record a detail | add_note(person,note) to note a trait | set_profile(person,[full_name],[relationship],[birthdate],[timezone]) | remove_fact(person,label|id) | remove_note(id). Use it when a message is about a household member's information: where someone studies, what someone does for work, what a person likes, when a birthday is."
metadata: {"nanobot":{"translatable":true,"always":true}}
---

# Household directory (the family's shared memory)

Each member's information, held in HomeCore, so **every Alfred in the house sees
the same thing**. Tell one Alfred something about another person and that
person's Alfred knows it too.

A summary of the household is already in your context as `FAMILY.md`. Use this
skill for somebody's **freshest** detail, to look one thing up, or to **save or
change** information.

Write the JSON block as plain text in your reply; the system intercepts and runs
it. Never exec, curl, or Python you write. Answer in the person's language.

## Who can write

Anybody may read anybody (`list_family`, `get_profile`, `search_family`).
Writing is the **parents** about anyone, and **each person about themselves**.
Anything else gets a 403 from the server: explain it kindly, never work around
it.

`person` takes an id, a first name or a nickname; the server resolves it.

## Actions

```json
{"skill": "family", "action": "get_profile", "person": "user3"}
{"skill": "family", "action": "search_family", "query": "school"}
{"skill": "family", "action": "set_fact", "person": "user4", "label": "School address", "value": "14 Mill Lane"}
{"skill": "family", "action": "add_note", "person": "user5", "note": "Dairy doesn't agree with them."}
```

Also: `list_family`, `set_profile` (birthdate, relationship), `remove_fact` (by
label), `remove_note` (by id).

`get_profile` returns `full_name`, `relationship`, `birthdate`, `facts` (each a
`label` and a `value`) and `notes`. `search_family` covers everybody's details
and notes.

## Rules

- If a message asks or states something about a member, **use this skill** (or
  `FAMILY.md` when that is enough) before answering. Never invent details.
- When somebody tells you something new about a member -- where they study or
  work, something they like, a trait -- **save it** so it stays shared.
- Labels are the update key: reuse the same one to replace a value rather than
  duplicating it. Keep them clear and consistent: `School`, `School address`,
  `Work`, `Work address`, `Hobbies`, `Allergies`, `Shoe size`.
- Never invent an `id`. Run `get_profile` first if you need one to delete.
