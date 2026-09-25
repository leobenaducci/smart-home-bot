# Translations

Seven locales: **en, fr, it, de, es** (EFIGS) plus **zh** and **ja**.

One flat JSON catalogue per locale, `<code>.json`, keyed by a dotted name.
Flat and format-neutral on purpose — the stack has three kinds of user-facing
surface and they all have to read the same file:

| Surface | Reads it via |
|---|---|
| The portal and the admin page (Flask) | `i18n/i18n.py`, `{{ t('...') }}` in Jinja |
| The entry page (PHP) | `i18n/i18n.php`, `<?= t('...') ?>` |
| The camera wall and the MQTT dashboard (browser JS) | `i18n/i18n.js`, `t('...')` |

There is no shared stylesheet in this stack and there must not be one — the
apps are different containers on different boxes, and a `<link>` across them
would make the camera wall depend on the hub being up to be legible. The
catalogues are different: they are small, they are copied in at deploy time,
and each app serves its own copy.

## Keys

`common.*` is anything more than one app shows. Everything else is namespaced
by app (`portal.*`, `admin.*`, `cameras.*`, `mqtt.*`, `entry.*`). Add a key to
`en.json` first — it is the reference, and `check.py` reports every other
locale against it.

home-core's household pages read the catalogue too: the chores page uses
`tasks.*`, and the chat apps menu plus the module header row use `nav.*` and
the shared `common.*` module names. `en.json` is the source, `es.json` is
complete for these, and the remaining locales fall back to English until
somebody translates them — that is the documented behaviour, not a gap to
block on.

```
./i18n/check.py              what is missing, per locale
./i18n/check.py --unused     keys nothing references
```

`en.json` is the fallback: a missing key renders the English string rather than
the key name, because a bare `portal.tiles.lights` on a wall panel is worse
than an untranslated word.

## Adding a locale

Add the code to `locale.available` in `config/home-stack.yml`, copy `en.json`
to `<code>.json`, translate it. The admin page only offers locales that have a
catalogue, so a half-added language cannot be selected by accident.
