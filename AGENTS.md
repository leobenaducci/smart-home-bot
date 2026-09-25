# AGENTS.md

The guidance for coding agents working in this repository is in
[CLAUDE.md](CLAUDE.md). Read it first, whatever agent you are: it describes how
the stack fits together, the rules that keep it working, and the mistakes that
have already caused outages on a live household.

Some services have their own `AGENTS.md` with what is specific to them
(`services/nanobot/AGENTS.md`, `services/home-core/AGENTS.md`,
`services/home-cameras/AGENTS.md`, `services/proxy/AGENTS.md`). The
`AGENTS.md` files under `services/nanobot/config/` and
`services/nanobot/nanobot/templates/` are different: they are prompts the
household's assistant reads, not instructions for you.
