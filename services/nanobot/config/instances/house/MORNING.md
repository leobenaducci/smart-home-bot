# Morning greeting — not applicable

The morning greeting writes into **one person's** HomeCore chat, and this
instance has no person: it is switched off in `config.json`
(`gateway.morningGreeting.enabled: false`) and it also disables itself, because
the code requires a `HOMECORE_USER_ID` this container never receives.

The file exists so the entrypoint finds the five `.md` files it links into the
workspace and doesn't leave a warning on every start.

If a **spoken** "good morning" in the kitchen is ever wanted, this is not it:
that is a `cron` using the `announce` skill on the room's speaker, at a time
when somebody is actually there to hear it.
