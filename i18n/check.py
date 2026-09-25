#!/usr/bin/env python3
"""Report translation coverage against en.json, which is the reference.

    ./i18n/check.py              missing keys per locale
    ./i18n/check.py --unused     keys no source file references
    ./i18n/check.py --unused --all  and the ones built from a family name
    ./i18n/check.py --strict     exit non-zero if anything is missing
"""

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load(locale, directory=HERE):
    return json.loads((directory / f"{locale}.json").read_text(encoding="utf-8"))


def plugin_catalogues():
    """`[(name, dir, source_dir)]` for every configured plugin that ships i18n.

    A plugin's keys are its own coverage set. They must not be required in this
    package's en.json -- the core cannot know what a household's own service
    calls things -- and they must not turn up under `--unused` here, where
    nothing would reference them because their source lives elsewhere.

    Degrades to nothing rather than failing: this script runs on a bare python3
    by design, and reading the config needs yaml and the deployer.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "deployer", ROOT / "deploy" / "deploy.py")
        deployer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deployer)
        cfg = deployer.load_yaml(deployer.CONFIG)
        found = []
        for plugin in deployer.load_plugins(cfg):
            catalogue = plugin["root"] / "i18n"
            if (catalogue / "en.json").exists():
                found.append((plugin["name"], catalogue, plugin["root"]))
        return found
    except Exception:  # noqa: BLE001 - no config, no yaml, no plugins: fine
        return []


def main():
    reference = load("en")
    ref_keys = {k for k in reference if not k.startswith("_meta.")}
    locales = sorted(p.stem for p in HERE.glob("*.json") if p.stem != "en")

    if "--unused" in sys.argv:
        # Grep the tree rather than parse it: keys reach templates, PHP and JS
        # as plain strings, and no single parser sees all three.
        where = [str(ROOT / "admin"), str(ROOT / "services"), str(ROOT / "deploy")]

        def referenced(needle):
            return subprocess.run(["grep", "-rqF", needle, *where],
                                  capture_output=True).returncode == 0

        # Two lists, not one. A key can be built rather than written --
        # `t(f"relation.{name}")`, or a catalogue looked up by service id --
        # and its whole text then appears nowhere while its family does.
        # Folding those in silently would make this list quiet enough to stop
        # catching the thing it is for: a single dead key in a family that is
        # still very much alive. So they are separated and both are printed.
        dead, built = [], []
        for key in sorted(ref_keys):
            if referenced(key):
                continue
            parts = key.split(".")
            families = {".".join(parts[:-1]) + "."}
            families |= {".".join(parts[:n]) + "." for n in range(2, len(parts))}
            live = next((f for f in sorted(families, key=len, reverse=True)
                         if referenced(f)), None)
            (built if live else dead).append((key, live))
        print(f"{len(dead)} key(s) referenced nowhere:")
        for key, _ in dead:
            print(f"  {key}")
        if built:
            # Counted, not listed, unless asked. These are mostly real --
            # `t(f"admin.access.key_{why}")` is a dozen keys nothing spells out
            # -- and printing all of them every time is how the list above
            # stops being read.
            print(f"\n{len(built)} more are built rather than written (their "
                  f"family is referenced). --all lists them.")
            if "--all" in sys.argv:
                for key, live in built:
                    print(f"  {key}  ({live})")
        unused = [k for k, _ in dead]

        # A plugin's keys are checked against the plugin's own source, which is
        # the only place that could reference them.
        for name, catalogue, source in plugin_catalogues():
            keys = {k for k in load("en", catalogue) if not k.startswith("_meta.")}
            stray = []
            for key in sorted(keys):
                # Not the catalogue itself: every key appears in its own
                # en.json, so including it makes each key reference itself and
                # the check finds nothing, ever. The core check greps admin/
                # and services/ for the same reason.
                hit = subprocess.run(
                    ["grep", "-rqF", "--exclude-dir=i18n", key, str(source)],
                    capture_output=True)
                if hit.returncode != 0:
                    stray.append(key)
            print(f"\n{name}: {len(stray)} key(s) referenced nowhere:")
            for key in stray:
                print(f"  {key}")
        return 0

    worst = 0
    print(f"reference: en.json, {len(ref_keys)} keys\n")
    for locale in locales:
        data = load(locale)
        missing = sorted(ref_keys - set(data))
        extra = sorted(set(data) - ref_keys - {"_meta.name", "_meta.dir"})
        pct = 100 * (len(ref_keys) - len(missing)) / len(ref_keys)
        status = "complete" if not missing else f"{len(missing)} missing"
        print(f"  {locale:<4} {pct:5.1f}%  {status}")
        for key in missing[:8]:
            print(f"         - {key}")
        if len(missing) > 8:
            print(f"         … and {len(missing) - 8} more")
        for key in extra:
            print(f"         ? {key} (not in en.json)")
        worst = max(worst, len(missing))

    # Each plugin against its own en.json. Reported separately because it is a
    # separate promise: this package being fully translated says nothing about
    # whether a household's own service is.
    for name, catalogue, _source in plugin_catalogues():
        plugin_ref = {k for k in load("en", catalogue) if not k.startswith("_meta.")}
        plugin_locales = sorted(p.stem for p in catalogue.glob("*.json")
                                if p.stem != "en")
        print(f"\n{name}: {len(plugin_ref)} keys")
        for locale in plugin_locales:
            data = load(locale, catalogue)
            missing = sorted(plugin_ref - set(data))
            pct = 100 * (len(plugin_ref) - len(missing)) / len(plugin_ref) if plugin_ref else 100
            status = "complete" if not missing else f"{len(missing)} missing"
            print(f"  {locale:<4} {pct:5.1f}%  {status}")
            for key in missing[:8]:
                print(f"         - {key}")
        # A locale this package has and the plugin does not is not an error:
        # the plugin's own en.json is the fallback, exactly as en.json is here.

    if "--strict" in sys.argv and worst:
        print("\nincomplete", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
