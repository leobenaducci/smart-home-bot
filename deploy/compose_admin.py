#!/usr/bin/env python3
"""The plugin directories the admin page has to be able to read.

`plugins:` accepts an absolute path -- `plugins: [/home/me/my-project]` is the
documented form, and it is the natural one when the plugin *is* the project's
own repository. The deployer runs on the host and sees those directories. The
admin page runs in a container and sees exactly what is mounted into it, which
is `paths.plugins` and nothing else.

So a household whose plugins live in their own repositories got an admin page
that could not list, enable or deploy any of them -- and worse than that, could
not list *anything*: `load_plugins` refuses a directory that is not there, so
one unreachable plugin took the whole merged service list with it and the page
quietly showed only the eleven services the package ships.

One mount per configured directory, at the same path inside as out, because the
deployer inside that container resolves plugin paths against the host's names.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_WANTED = ("plugin_dirs", "base_paths")
D = next((m for m in list(sys.modules.values())
          if m is not None and all(hasattr(m, n) for n in _WANTED)), None)
if D is None:
    import deploy as D  # noqa: E402

GENERATED = "docker-compose.plugins.yml"

HEADER = """\
# GENERATED FILE -- every edit here is lost on the next deploy.
#
# Written by deploy/compose_admin.py from `plugins:` in config/home-stack.yml.
# One read-only mount per plugin directory that is not already inside
# `paths.plugins`, so the page can read a plugin that lives in its own
# repository somewhere else on this machine.
"""


def outside_mounts(cfg: dict) -> list:
    """Plugin directories not already covered by the `paths.plugins` mount."""
    root = (D.base_paths(cfg) or {}).get("plugins")
    out = []
    try:
        dirs = list(D.plugin_dirs(cfg))
    except Exception:  # noqa: BLE001 - a bad `plugins:` list is the deploy's
        return []      # problem to report, not this file's
    for d in dirs:
        d = Path(d)
        if root:
            try:
                d.resolve().relative_to(Path(root).resolve())
                continue
            except ValueError:
                pass
        out.append(str(d))
    return sorted(set(out))


def render(cfg: dict) -> str:
    mounts = outside_mounts(cfg)
    if not mounts:
        # `services:` with nothing under it does not parse, and a household
        # whose plugins all live under paths.plugins needs no overlay at all.
        return HEADER + "services: {}\n"
    lines = [HEADER, "services:\n", "  admin:\n", "    volumes:\n"]
    for path in mounts:
        # Read-only: the page reads a plugin to list and deploy it. Writing to
        # somebody's project checkout is not something this page should be able
        # to do by accident.
        lines.append(f"      - {path}:{path}:ro\n")
    return "".join(lines)


def write(cfg: dict, unit_dir: Path) -> Path:
    path = Path(unit_dir) / GENERATED
    body = render(cfg)
    if not path.exists() or path.read_text(encoding="utf-8") != body:
        path.write_text(body, encoding="utf-8")
    return path


if __name__ == "__main__":
    import yaml
    print(render(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))), end="")
