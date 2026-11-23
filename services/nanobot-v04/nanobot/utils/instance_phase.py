"""Which slot of a shared cycle this instance gets.

Five instances start from one `docker compose up` and then each sleeps a fixed
interval, so anything periodic fires on all of them in the same second — for
as long as they stay up, and again from scratch after every deploy. Measured
2026-08-24 on the family heartbeat: 10:01:45, 10:31:46 and 11:01:56, identical
across user1..user5, and the gateway answered the burst with errors that cost
each instance 7.0s of retry waiting for a 540-token call.

One walk, in one place, so everything periodic lands in the same spread rather
than each service inventing its own. `frac(n · φ)` is the standard
low-discrepancy fill: it needs no total, so instance six lands in the largest
remaining gap instead of wherever a digest fell, and an unnumbered instance is
index 0 — the base the numbered ones are spread around — rather than a second,
unrelated generator that nothing keeps clear of the first.

Deterministic on purpose. Re-rolled per boot, a restart would be a fresh chance
to collide, and these five restart together.
"""

from __future__ import annotations

import os
import re

from loguru import logger

_GOLDEN = 0.618_033_988_749_895


def phase_fraction(instance: str | None = None) -> float:
    """This instance's position in [0, 1) of any shared cycle.

    The index comes from `NANOBOT_STAGGER_INDEX` when the deployer supplies it,
    and from the digits in `NANOBOT_INSTANCE` otherwise. That order matters:
    `frac(n · φ)` is low-discrepancy over a *consecutive* run of indices and
    says nothing about a sparse one, and this deployment guarantees sparse.
    Member ids are monotonic and never reused -- refilling a freed slot once
    handed a new person the departed member's derived token, assistant state
    and backups -- so a household that has seen departures runs `user2` beside
    `user15`. Measured on the walk: an id gap of 13 puts those two 62s apart in
    a 30-minute cycle and a gap of 34 puts them 23.7s apart, which is worse
    than the hashing this replaced, and silent, because the offsets still look
    spread when printed.

    Only the deployer can fix that, because only the deployer holds the live
    member list; it exports the member's *position* in it. The instance name is
    the fallback for a container run by hand or by an older deployer.

    `NANOBOT_INSTANCE` is what the entrypoint already uses to pick a config
    overlay. Unset — the CLI, a single-instance install — is 0.0, because a
    deployment of one has nobody to be spread against and a first run delayed
    for no reason is its own small bug. A name with no digits is 0.0 too: the
    base the numbered instances are spread around, rather than a second,
    unrelated generator nothing keeps clear of the first.
    """
    if instance is None:
        supplied = os.environ.get("NANOBOT_STAGGER_INDEX", "").strip()
        if supplied:
            try:
                return (max(0, int(supplied)) * _GOLDEN) % 1.0
            except ValueError:
                logger.warning(
                    "NANOBOT_STAGGER_INDEX is {!r}, not a number; falling back "
                    "to the instance name", supplied,
                )
    name = (instance if instance is not None
            else os.environ.get("NANOBOT_INSTANCE", "")).strip()
    if not name:
        return 0.0
    trailing = re.search(r"(\d+)$", name)
    index = int(trailing.group(1)) if trailing else 0
    return (index * _GOLDEN) % 1.0
