#!/usr/bin/env python3
"""Removed on purpose.

Household members are added, removed and deactivated on the admin page, and
nowhere else. Two ways to edit the user store meant two ideas of who lives here:
this script wrote usernames straight into users.json without allocating a member
id, so the assistant, the per-member credentials and the portal disagreed about
the same person.

    http://<hub>:8099/members

The user store itself is state on the machine that runs the portal, at
`{paths.state}/home-core/users.json`. See docs/backups.md.
"""
import sys

sys.exit(__doc__)
