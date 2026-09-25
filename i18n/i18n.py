"""Translation lookup for the Flask apps.

    from i18n import Translator
    t = Translator(catalogue_dir, default="es")
    t("portal.tiles.cameras", locale="fr")
    t("common.minutes_ago", n=5)

English is the fallback. A key with no translation renders the English string
rather than the key name: a bare `portal.tiles.cameras` on a wall panel is worse
than an untranslated word.
"""

from __future__ import annotations

import json
from pathlib import Path


class Translator:
    def __init__(self, catalogue_dir: str | Path, default: str = "en"):
        self.dir = Path(catalogue_dir)
        self.default = default
        self._cache: dict[str, dict[str, str]] = {}
        self._fallback = self._load("en")

    def _load(self, locale: str) -> dict[str, str]:
        if locale in self._cache:
            return self._cache[locale]
        path = self.dir / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self._cache[locale] = data
        return data

    @property
    def available(self) -> list[str]:
        """Locales that actually have a catalogue. The admin page offers only
        these, so a half-added language cannot be selected by accident."""
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def name_of(self, locale: str) -> str:
        return self._load(locale).get("_meta.name", locale)

    def __call__(self, key: str, locale: str | None = None, **params) -> str:
        catalogue = self._load(locale or self.default)
        text = catalogue.get(key)
        if text is None:
            text = self._fallback.get(key)
        if text is None:
            text = key
        if params:
            for name, value in params.items():
                text = text.replace("{" + name + "}", str(value))
        return text

    def js(self, key: str, locale: str | None = None, **params) -> str:
        """The same string, safe to drop inside a JavaScript string literal.

        Jinja's autoescaping does not help here. Inside an attribute like
        onclick="...confirm('{{ t(...) }}')", the browser HTML-decodes the
        attribute *before* the JS engine parses it, so an escaped `&#39;` in an
        interpolated name becomes a real quote and closes the literal. Escaping
        for JS first, and letting Jinja escape the result for HTML, is the only
        order that survives both passes.
        """
        text = self(key, locale, **params)
        return (text.replace("\\", "\\\\")
                    .replace("'", "\\'")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                    .replace("\r", "")
                    .replace("<", "\\u003c")
                    .replace(">", "\\u003e")
                    .replace("&", "\\u0026"))
