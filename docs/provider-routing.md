# One provider or many, and where a router would fit

A decision record. The question asked on 2026-09-08 was whether this stack
should stop supporting several model providers and instead point everything at
[9Router](https://github.com/decolua/9router), keeping the admin page as the
place a household picks a model and letting the router handle the rest.

**Short answer: not as the single upstream for the assistants, and it does not
need a rewrite to try — `openai-compatible:` is already the slot it fits in.**

## What 9Router is

MIT-licensed, self-hosted, a Node service in a container
(`decolua/9router`, port 20128), with a browser dashboard for providers,
routing rules and keys. It exposes **one OpenAI-compatible `/v1` endpoint**,
translates between the OpenAI, Claude and Gemini request formats, and fans out
to 40+ upstreams. It holds the credentials — API keys for the paid providers
and **OAuth tokens for subscription accounts** (Claude Code, Copilot, Cursor,
Antigravity) — and supports **multiple accounts per provider** with round-robin
or priority order. Its documented routing rule is
**"Subscription → Cheap → Free"**, switching when a quota is exhausted or a
call errors. It also rewrites requests: "RTK" compresses tool output like git
diffs and directory trees for 20–40% fewer input tokens, and a "Caveman mode"
cuts response length by up to 65%.

None of that is hosted by anybody else. On the "everything runs on one PC"
axis it fits this house well, which is why the question is worth a real answer
rather than a reflex.

## Why it is the wrong default here

**1. Its headline feature is the thing this stack has a rule against.** The
project describes itself as "Unlimited FREE AI coding … Auto-fallback … never
hit limits", and the mechanism is routing through subscription accounts and
rotating between them. `CLAUDE.md` already settles the same question in the
narrow case and it cost something to settle: the flat Go plan is used by
`opencode serve` alone, because Go's traffic is "monitored for abusive traffic"
and expects a client that "properly identifies itself" — while what the rest of
this stack sends is containers on one key running unattended around the clock,
roughly 1,800 turns a month with nobody at a keyboard. That is the shape a flat
plan flags, and **the account can be blocked for it**. A router whose purpose
is to keep unattended traffic under subscription quotas does not remove that
hazard; it makes it the default path and spreads it across more accounts.

**2. The session header is unanswered, and it is not optional.** Every request
to `opencode.ai` must carry `x-opencode-session`, one stable id per
conversation, and the four callers here each answer with the best id they
actually have. The rule that matters is the second one: *never hard-code the
id, or every household on earth becomes one caller.* A translating proxy that
normalises requests and rotates accounts is structurally the thing that turns
per-conversation ids into one caller, and 9Router's documentation says nothing
about passthrough of unknown headers. Until that is **tested rather than
assumed**, nothing that talks to `opencode.ai` should sit behind it.

**3. It concentrates every credential in a new place with a web UI.** Today
`OPENCODE_API_KEY` is the only credential that leaves the network. 9Router's
dashboard would hold every provider key *and* live OAuth tokens for
subscription accounts, in one container, behind its own API key. That is a
higher-value secret store and a second web surface on a stack that thinks
carefully about the one it already has.

**4. It rewrites the request, and here the request is the household's data.**
Compressing `git diff` output is a sensible trick for a coding CLI. The
assistants' prompts are the family's chores, menus and messages, and the
replies are read aloud in the kitchen. A proxy silently trimming context or
shortening answers is a correctness variable this stack cannot see — and the
verification convention here is that a check asserts a payload, not a status
code, precisely because invisible degradation is the failure mode that gets
missed.

**5. Its target clients are not these callers.** The supported list is Claude
Code, Codex, Cursor, Cline, Copilot, Gemini, OpenCode, Antigravity. The callers
in this stack are the assistant, the chat titler and two model probes. Nothing
in that list resembles them.

**6. "Which model answered" stops being a setting.** The admin page's picker is
a setting that reaches a container, and `IMPACT` exists so a household can see
which services a change touches. Automatic fallback across tiers means the
model that answered is decided at request time by quota state. That is a
feature for a coding tool and a regression for a page whose contract is that
what you chose is what runs.

## Where it would actually fit

**The Programmer space.** `opencode serve` is a coding client driven by a
person, which is exactly what 9Router is built and tested for, and token
compression on a coding harness is where the 20–40% claim is plausible. Even
there, note that putting a router in front changes which client the upstream
sees identifying itself — the specific thing Go's terms care about.

## And it needs no rewrite to try

`MODEL_PROVIDERS` already carries **`openai-compatible`** — "anything that
speaks the OpenAI API at a URL you give it: a vLLM or llama.cpp server on the
LAN, LM Studio, or a provider this package has never heard of". A 9Router
container on `nanobot-net` is precisely that. Point one role at it through
`cloud.openai_compatible`, leave the others where they are, and the comparison
is a config change rather than an architecture.

That ordering matters, because the proposal's real attraction is deleting
machinery — `MODEL_PROVIDERS`, `PROVIDER_REQUIREMENT`, the per-provider
credential checks, the admin page's key cards. That machinery is not there
because the house needs four providers at once. It is there because the admin
page lets a household **choose**, and a package meant to be given away cannot
assume every household will run a router. Collapsing it would trade a setting
other people need for a dependency this house has not measured yet.

**Measure first, in this order:** does `x-opencode-session` survive the hop;
does a tool-calling turn round-trip unchanged with RTK on and off; and what the
assistant's own suite says with the router in the path. If those pass, the
argument for it gets much stronger and the credential and terms-of-service
questions are still the ones to answer.
