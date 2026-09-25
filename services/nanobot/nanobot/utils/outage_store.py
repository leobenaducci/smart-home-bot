"""Outage windows that outlive one process, so the house learns once.

`LLMProvider._OUTAGE_WINDOWS` shares what a route discovers with every other
route reaching the same gateway — inside one process. There are six of those on
this box (five family instances and casa), and until now each of them
rediscovered the same outage from scratch. Measured on 2026-08-24 while
`gpt-5.6-luna` was 500ing at opencode: eleven requests and 31.0s of waiting,
paid once per container, so up to six people each waited through one dead turn
for the same failure on somebody else's server.

This is that discovery written where the others can read it. Event-driven and
not polled: a probe on a timer is asleep for the minutes that matter — the
notification session alone starts a turn every few minutes, so a real turn
finds an outage long before an hourly check would, and the check costs a
request whether or not anything is wrong.

Two things are deliberately unlike the in-process window:

* **Wall clock, not monotonic.** Monotonic epochs are per-process and mean
  nothing to a reader in another container. The cost is that an NTP step moves
  a shared deadline by the size of the step; it is bounded by the window
  itself (an hour) and the worst case is one route re-proving a model dead, or
  holding a fallback slightly too long. Worth it to be legible across a mount.
* **Last write wins, and a lost one is survivable.** Two containers marking
  different models in the same instant can drop one update. That costs a
  rediscovery — the thing this file exists to avoid, once — and never
  correctness, so it does not justify a lock file that could be left behind by
  a container that died holding it.

Every failure here is non-fatal by construction. No mount, a read-only mount, a
truncated file, somebody's editor: the store degrades to "knows nothing" and
each process keeps the behaviour it had before this existed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

_FILENAME = "model-outages.json"
# The mount the compose files provide. Overridable for a deployment that puts
# it somewhere else, and for tests.
_DEFAULT_DIR = "/shared-state"


class OutageStore:
    """A JSON file of `{gateway: {model: unix_deadline}}`, read through a stat."""

    # The longest a single window may be. The caller owns the real number
    # (`LLMProvider._MODEL_OUTAGE_COOLDOWN_S`) and passes it on the first
    # write; this is the fallback for a file written by something else.
    max_window_s: float = 3600.0

    def __init__(self, directory: str | None = None) -> None:
        self.use_dir(directory or os.environ.get("NANOBOT_SHARED_STATE_DIR") or _DEFAULT_DIR)

    def use_dir(self, directory: str | os.PathLike[str]) -> None:
        self.path = Path(directory) / _FILENAME
        self._cache: dict[str, dict[str, float]] = {}
        self._mtime: float = -1.0
        self._read_warned = False
        self._write_warned = False

    # --- reading -----------------------------------------------------------

    def deadlines(self, gateway: str) -> dict[str, float]:
        """Models presumed down at *gateway*, to their unix deadlines."""
        if not gateway:
            return {}
        self._refresh()
        return self._cache.get(gateway, {})

    def remaining(self, gateway: str, model_identity: str) -> float:
        """Seconds left on a shared window, or 0.0 when there is none.

        This is on the path of every LLM call, so it is a `stat` and a dict
        lookup in the common case — the file is only re-read when somebody has
        written to it since the last look.
        """
        deadline = self.deadlines(gateway).get(model_identity)
        if not deadline:
            return 0.0
        left = max(0.0, deadline - time.time())
        if left <= self.max_window_s:
            return left
        # A deadline further out than one window can only come from a clock
        # that was wrong when it was written -- a container that started before
        # NTP stepped it writes `time.time() + 3600` against a date months
        # ahead, and once the clock corrects, every process on the box adopts
        # that as "remaining". Nothing would clear it: `_prune` drops only
        # deadlines already past, the local lazy expiry is cancelled by the
        # next re-adoption from the file, and there is no reset anywhere short
        # of deleting the file by hand. This module's docstring claims the
        # window bounds the damage; this is the line that makes that true.
        logger.warning(
            "Outage deadline for {}/{} is {:.0f}s away, beyond the {:.0f}s "
            "window -- treating it as one window and rewriting it. A clock "
            "step while it was being written is the usual cause.",
            gateway, model_identity, left, self.max_window_s,
        )
        self.mark(gateway, model_identity, time.time() + self.max_window_s)
        return self.max_window_s

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            # No mount, no file yet, no permission. All the same answer.
            self._cache = {}
            self._mtime = -1.0
            return
        if mtime == self._mtime:
            return
        try:
            with self.path.open(encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("not an object")
        except Exception as exc:
            # Keep whatever was last read rather than forgetting: a half-written
            # file is a moment, and the window it describes is an hour.
            if not self._read_warned:
                logger.warning("Shared outage store unreadable ({}): {}", self.path, exc)
                self._read_warned = True
            self._mtime = mtime
            return
        self._read_warned = False
        self._mtime = mtime
        self._cache = {
            str(gw): {
                str(model): float(until)
                for model, until in (models or {}).items()
                if isinstance(until, (int, float))
            }
            for gw, models in loaded.items()
            if isinstance(models, dict)
        }

    # --- writing -----------------------------------------------------------

    def mark(self, gateway: str, model_identity: str, deadline: float) -> None:
        """Publish that *model_identity* is down at *gateway* until *deadline*."""
        if not gateway or not model_identity:
            return
        try:
            data = self._load_for_write()
            data.setdefault(gateway, {})[model_identity] = float(deadline)
            self._prune(data)
            self._atomic_write(data)
        except Exception as exc:
            # A house that cannot share still works; it just rediscovers.
            if not self._write_warned:
                logger.warning(
                    "Shared outage store not writable ({}): {} — each instance will "
                    "discover outages on its own, as before",
                    self.path, exc,
                )
                self._write_warned = True
            return
        self._write_warned = False

    def _load_for_write(self) -> dict[str, Any]:
        """Re-read from disk rather than trusting the cache: another container
        may have written since the last stat, and merging onto a stale copy is
        how the update that mattered gets dropped.

        Repairing an unreadable file by replacing it is deliberate -- this is
        derived state and a store nobody can parse is worse than a lost
        window. But it is not free, and it was silent: every *other*
        container's and every other gateway's live windows go with it, and
        `mark()` only warns when the write itself fails. Absent is the ordinary
        case and says nothing; unreadable says what it is discarding, so a
        house that starts re-paying ladders has a line to find.
        """
        try:
            with self.path.open(encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                return loaded
            raise ValueError(f"top level is {type(loaded).__name__}, not an object")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning(
                "Shared outage store unreadable ({}): {} — rewriting it, which "
                "drops any window another instance had published",
                self.path, exc,
            )
            return {}

    @staticmethod
    def _prune(data: dict[str, Any]) -> None:
        """Drop expired entries on the way past, so the file cannot grow."""
        now = time.time()
        for gateway in list(data):
            models = data.get(gateway)
            if not isinstance(models, dict):
                data.pop(gateway, None)
                continue
            for model in list(models):
                until = models[model]
                if not isinstance(until, (int, float)) or until <= now:
                    models.pop(model, None)
            if not models:
                data.pop(gateway, None)

    def _atomic_write(self, data: dict[str, Any]) -> None:
        """Temp file plus `os.replace`, so a reader mid-write sees one or the
        other and never half of each."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".outages-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # The next read must not trust a cache the write just invalidated.
        self._mtime = -1.0


SHARED_OUTAGES = OutageStore()


# ---------------------------------------------------------------------------
# Detours: a server the house has lent to something else for a while
# ---------------------------------------------------------------------------
_DETOUR_FILENAME = "model-detours.json"


class DetourStore:
    """Servers whose calls go elsewhere until a deadline, and where to.

    The admin page writes this while a benchmark borrows a card: the house's
    own text servers on it are unloaded, and every call for them goes to the
    everyday cloud model until the benchmark gives the card back. Unlike an
    outage it is decided, not discovered -- so it names its target instead of
    walking the rescue chain -- and it always has a deadline, so a benchmark
    that dies holding it cannot leave the house on the cloud for good.

        {"gateways": {"http://host:11437/v1": {"until": 1790..., "model": "...",
                                               "provider": "custom"}},
         "reason": "benchmark of qwen3.5:2b"}

    Read on every call, so it is a stat and a dict lookup unless the file
    changed. Every failure reads as "no detour".
    """

    def __init__(self, directory: str | None = None) -> None:
        self.use_dir(directory or os.environ.get("NANOBOT_SHARED_STATE_DIR") or _DEFAULT_DIR)

    def use_dir(self, directory: str | os.PathLike[str]) -> None:
        self.path = Path(directory) / _DETOUR_FILENAME
        self._cache: dict[str, dict[str, Any]] = {}
        self._mtime: float = -1.0

    def target(self, gateway: str) -> dict[str, Any] | None:
        """{"model", "provider"} for calls to *gateway* now, or None."""
        gateway = (gateway or "").rstrip("/")
        if not gateway:
            return None
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._cache, self._mtime = {}, -1.0
            return None
        if mtime != self._mtime:
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
                gws = doc.get("gateways") if isinstance(doc, dict) else None
                self._cache = {str(k).rstrip("/"): v for k, v in (gws or {}).items()
                               if isinstance(v, dict)}
            except (OSError, ValueError):
                self._cache = {}
            self._mtime = mtime
        entry = self._cache.get(gateway)
        if not entry or not entry.get("model"):
            return None
        try:
            if float(entry.get("until") or 0) <= time.time():
                return None
        except (TypeError, ValueError):
            return None
        return {"model": str(entry["model"]), "provider": entry.get("provider") or None}


SHARED_DETOURS = DetourStore()
