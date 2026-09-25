"""Which model answers as which persona, and what that costs.

Two jobs, kept in one place because they are the same question asked twice:

  * **What can this house reach?** OpenCode Zen's roster, from models.dev,
    plus whatever the configured Ollama actually has pulled. One is billed per
    token; the other runs on hardware you already own and costs nothing per
    token.

    Zen, and deliberately not the OpenCode **Go** plan, which this stack used
    until 2026-09-01 and must not go back to. Go is a flat $10/month
    subscription, and its documentation says traffic is "monitored for abusive
    traffic that degrades the experience for other users" and that a client
    must be one that "properly identifies itself". This stack is not an
    interactive coding session: it is five containers on one key making
    something like 1,800 unattended runs a month -- notification triage every
    few minutes, cron reminders, memory consolidation, a morning summary --
    and that is the traffic shape a flat plan flags. The account can be
    blocked for it. Zen is the same key and an overlapping roster, billed per
    token, and it is the endpoint every part of this stack points at.

  * **Which one suits this persona?** The professions do genuinely different
    work — a code review is not a poster is not a one-line notification
    triage — and the model that is right for one is wasteful or wrong for
    another.

The recommendation is computed from the live catalogue rather than written
down, and that is the point. A frozen opinion goes stale silently: the rates
this stack shipped with had `gpt-5.6-luna` at $0.10/M input, and by the time
anybody looked it was $0.20 — with two models added and the cheapest one down
to $0.0175. A recommendation that cannot see a price change is a recommendation
about last quarter.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

MODELS_DEV = "https://models.dev/api.json"
# Zen's OpenAI-compatible endpoint. Not the flat Go plan's `/zen/go/v1` -- see
# the warning at the top of this file and in CLAUDE.md.
ZEN_API_BASE = "https://opencode.ai/zen/v1"
# Sent on every Zen request as x-opencode-session, required from 2026-09-06 --
# without it "requests may error". One id for the life of this process, which is
# the page: these are one-token probes with no history, so there is no
# conversation to key on, and a fresh id per probe would make one household look
# like a hundred callers. Same reasoning as `_SESSION` in deploy/models.py.
_ZEN_SESSION = f"home-stack-admin-{uuid.uuid4().hex}"


def _opencode_session(base: str) -> dict:
    """`{"x-opencode-session": ...}` for OpenCode's gateway, `{}` for anyone else.

    Matched on the host rather than the path, for the same reason the provider
    does it: Zen is `/zen/v1` and the flat plan is `/zen/go/v1`, the header is
    asked for either way, and keying on the path would be a second place that
    has to be right about which of the two is in use. Everything else this file
    talks to -- models.dev, together, openrouter, a household's own Ollama --
    has no idea what a session id is, and tagging them reads as a bug later.
    """
    return ({"x-opencode-session": _ZEN_SESSION}
            if "opencode.ai" in (base or "").lower() else {})


# What models.dev calls this provider in its own JSON. Not the same string as
# the `provider` field a model carries here -- that one has to match a key in
# SOURCES, and using this for both is what made the group render empty once
# already: the picker filtered on `opencode_zen` while every model said
# `opencode`, so thirty models sat in the cache and none were on offer.
#
# models.dev calls Zen `opencode` and the flat plan `opencode-go`, which are
# two different rosters -- 94 models against 33, sharing 17 -- at different
# prices for the models they do share. `opencode` is the one this stack reads.
MODELS_DEV_PROVIDER = "opencode"
PROVIDER = MODELS_DEV_PROVIDER          # kept: read by admin/app.py and tests

# How long a cached catalogue is considered current. The refresher below runs
# on the configured schedule; this is the backstop for a page load that finds
# the cache older than a week because the stack was switched off.
STALE_AFTER_S = 7 * 24 * 3600

# How this catalogue was *built*, as opposed to when. Bumped whenever the
# building changes in a way that makes an older cache wrong rather than merely
# old -- a source added or dropped, a filter removed, a roster taken from
# somewhere else.
#
# It exists because a deploy had no way to say so. The Zen roster moved from
# models.dev-plus-probing to the provider's own /v1/models, which took the
# picker from 5 models to 10 -- and the page went on showing 5 for two days,
# because the cache was 47 hours old against a 7-day life and nothing asks
# whether the code that wrote it still agrees with the code reading it. The
# household pressed Refresh, was told the fix had shipped, and still saw the old
# list; the answer each time was "press Refresh", which was no answer at all.
#
# A cache from a different version is stale by definition, whatever its age.
CATALOGUE_VERSION = 3


# --------------------------------------------------------------------------
# What each persona actually needs
# --------------------------------------------------------------------------
# Weights, not model names. Each is a claim about the *work*, which changes far
# more slowly than the roster does:
#
#   reasoning     getting it right matters more than getting it quickly
#   context       it reads long things -- a repo, a term's worth of material
#   output        it writes long things -- a whole HTML page, a full test
#   latency       somebody is watching a cursor blink
#   volume        it runs constantly, so input price dominates everything else
#   vision        it is handed images
#
# The comments name the persona file the claim comes from, so a change there
# and a change here stay in step.
# What each persona *requires*, and what this house has actually measured.
#
# The requirements are hard filters, and they are the only thing here derived
# from data: context window, output ceiling, image input. Everything else in
# models.dev is a price or a boolean, and there is no quality column — 25 of
# the 26 hosted models report `reasoning: true`, which tells you nothing about
# which one writes a correct code review.
#
# So this does not claim to know which model is *best*. It answers the question
# it can answer honestly: **the cheapest model that can do this persona's job
# at all**, plus whatever the house has measured for itself. A ranking that
# pretended to know more would be a number with an opinion painted on it.
# --- image generation ---------------------------------------------------------
#
# A separate list from the personas above, and separate for a reason: those are
# chat models and `recommend()` ranks them on context window and price per
# token. An image endpoint reports neither -- Together returns `context: 0`,
# no `max_output` and 0.0 for both prices -- so a slot that went through the
# same ranking would sort every one of them equally and recommend the first
# alphabetically.
#
# Matched on the family name rather than on those zeros. `context == 0` is what
# an image model happens to look like in this catalogue today; the name is what
# it *is*, and a text model that starts reporting zero context should not
# quietly become an option for drawing.
IMAGE_FAMILIES = (
    "flux", "dall-e", "stable-diffusion", "sdxl", "imagen", "qwen-image",
    "recraft", "ideogram", "playground", "kolors", "hidream",
)

# Providers that can generate an image. Deliberately not the local ones: an
# Ollama serving text models cannot draw, and offering it here would be a slot
# somebody fills in and nothing honours.
IMAGE_PROVIDERS = ("together", "openai", "openrouter")


def is_image_model(model: dict) -> bool:
    ident = str(model.get("id", "")).lower()
    if model.get("provider") not in IMAGE_PROVIDERS:
        return False
    return any(f in ident for f in IMAGE_FAMILIES)


def image_models(catalogue: dict) -> list:
    """Every cloud model in the catalogue that draws, best-known first."""
    found = [m for m in all_models(catalogue) if is_image_model(m)]
    found.sort(key=lambda m: (m.get("provider", ""), m.get("name", m["id"])))
    return found


IMAGE_SLOTS = {
    "image_high": {
        "label": "Images — best quality",
        "why": "Used when somebody asks for something to keep: a theme "
               "backdrop, a picture for a document. Slower and dearer per "
               "image, and worth it because the result is looked at.",
    },
    "image_normal": {
        "label": "Images — everyday",
        "why": "Everything else. Quick sketches, a thumbnail, an illustration "
               "in passing. This one runs far more often, so its price is the "
               "one that shows up on a bill.",
    },
}


# The four questions this list answers, which are not the same question.
#
# Flat, these thirteen mixed *what kind of work* (the professions), *who runs
# it* (the main agent or a spawned one), *what modality* (vision) and *what
# happens when it fails* (fallback) into one namespace. That is how "Background
# work" and "Sub-agents" came to describe each other, and how a role that
# serves the professions ended up recommended a small local model.
#
# Grouping does not change any routing. It changes what a person reads before
# choosing, which is where the mistake was made.
PERSONA_GROUPS = ("conversation", "profession", "unattended", "special")


# Roles that may never run on a model that costs nothing.
#
# Every one of these is answered by the *assistant*, and the assistant is five
# containers running unattended around the clock -- roughly 1,800 turns a month
# with nobody at a keyboard. Free capacity is not meant for that shape, which
# is the same reason CLAUDE.md refuses the flat Go plan for it, and a household
# that points a role here at a zero-cost model is spending its way toward a
# blocked account without being told.
#
# `programmer` is on the list, and the distinction is worth being exact about,
# because the obvious reading gets it backwards. A member with the Programmer
# switched on runs that space on opencode, where a free model is a legitimate
# choice -- but that is `cloud.opencode.model`, which this page does not
# configure at all. `assistant.models.programmer` is the *assistant's*
# Programmer: the children's, and the rescue when opencode is down. Assistant
# traffic, so the rule applies.
#
# This exists as a named rule rather than a side effect because free models are
# kept out of this page's Zen catalogue entirely today, which happens to
# protect these roles. The day somebody relaxes that filter -- to offer free
# models for a role that may have them -- this must still hold.
ZERO_COST_BARRED = frozenset({
    "everyday", "powerful", "notifications", "events", "subagent", "planner", "plan_steps",
    "vision", "fallback", "heartbeat", "classifier",
    "programmer", "teacher", "designer", "doctor", "legal",
    "titles", "documents",
})


def zero_cost_ok(role: str, model: dict | None) -> bool:
    """May this role run on this model?

    False only for a model this page knows to be free *and* a role the
    assistant answers. An unknown price is not a free one -- a model missing
    from the catalogue, or one whose price nobody published, is refused
    elsewhere for being unknown and must not be refused here for being cheap.

    **A local model is not what this bars.** The rule is about a *hosted* free
    tier, whose own documentation says collected data may be used to improve
    the model -- and what the assistant reads unattended is this household. A
    model on the box in the cupboard costs nothing for the opposite reason and
    is the most private option there is, not the least.

    Without that carve-out this refused `notifications`, `events` and `vision`
    on their Ollama models -- the three highest-volume roles in the house, the
    ones deliberately kept local, and every one of them already configured
    that way. The page rejected the household's own settled configuration and
    dropped those roles from the save.
    """
    if role not in ZERO_COST_BARRED or not isinstance(model, dict):
        return True
    if model.get("local") or model.get("provider") in LOCAL_SOURCES:
        return True
    price = model.get("input")
    return not (price is not None and float(price) == 0.0)


PERSONA_NEEDS = {
    "everyday": {
        "label": "Everyday Alfred",
        "model_type": "fast",
        "group": "conversation",
        "placement": "hosted",
        "why": "Ordinary chat. Somebody is waiting, and it runs more than "
               "anything except notification triage — so the input price, "
               "after caching, is most of the bill.",
        "requires": {"context": 200_000},
        "prefer": "cache_read",
        # What this house measured rather than what a table implies. Kept
        # because it is evidence, and dated because it ages.
    },
    # Called "Background work" until 2026-09-01, and it never was. `loop.py`
    # routes here on `powerful or is_space_session(chat_id)` -- a turn HomeCore
    # marks, or any turn inside a profession's chat. Cron sets neither: a
    # reminder firing from cron is an ordinary turn, and the morning summary is
    # a sub-agent. The old wording claimed the opposite of both, which is how
    # this role ended up pointed at a small local model -- the recommendation
    # followed the description rather than the code, and so did I.
    "powerful": {
        "label": "Harder turns",
        "model_type": "reasoner",
        "group": "conversation",
        "placement": "hosted",
        "why": "When getting it right is worth more than getting it quickly: "
               "turns HomeCore marks as hard, and every turn inside a "
               "profession's chat that does not name a profession of its own. "
               "A wrong figure or a wrong command costs more than the extra "
               "seconds. Ordinary chat stays on the fast model. This is not "
               "background work -- see Sub-agents for that.",
        "requires": {"context": 200_000, "max_output": 32_000},
        "prefer": "input",
    },
    "planner": {
        "label": "Planner",
        "model_type": "fast",
        "group": "conversation",
        "placement": "hosted",
        "why": "Work of several steps done in the chat -- cross-referencing two "
               "systems, checking a list -- is planned first: this model writes "
               "the steps, reads what each one found, changes the plan or asks, "
               "and writes the answer. A few calls a request, so its judgement "
               "matters more than its price. Left blank it follows Everyday Alfred.",
        "requires": {"context": 200_000},
        "prefer": "output",
    },
    "plan_steps": {
        "label": "Plan steps",
        "model_type": "fast",
        "group": "conversation",
        "placement": "local",
        "prefer_local": True,
        "why": "Carries out each step of a plan with Alfred's tools and skills: "
               "one clear instruction at a time, most of a plan's calls, so a "
               "local model here spends nothing per token. A step it cannot "
               "finish is done again by the Planner. Left blank it follows "
               "Sub-agents.",
        "requires": {},
        "prefer": "input",
    },
    "subagent": {
        "label": "Sub-agents",
        "model_type": "fast",
        "group": "conversation",
        "placement": "hosted",
        "why": "What the assistant hands work to rather than doing it in the "
               "turn: a background task, a memory consolidation, the morning "
               "summary. Nobody is watching the seconds, but it makes a lot of "
               "calls, so the input price is most of what it costs. "
               "Left blank it follows Everyday Alfred, which is the "
               "default worth having: a household that moves provider "
               "and does not think about sub-agents would otherwise "
               "leave every background task calling the one they left.",
        "requires": {"context": 200_000},
        "prefer": "input",
    },
    # Named for what it actually answers, which is no longer the whole
    # profession. A member with `programmer` switched on has that space on
    # opencode -- a different agent, on a model `cloud.opencode.model` names --
    # and this setting never reaches them unless opencode is unreachable or has
    # no agent yet. It is what everybody *else* gets, and what the two of them
    # fall back to.
    #
    # Called plain "Programmer" until 2026-09-02, which was true when it was
    # the only backend and became a lie the day it stopped being. A page naming
    # a model that never answers the person reading it is worse than one short
    # of options: they can choose, save, deploy, and see nothing change.
    "programmer": {
        "label": "Programmer (on the assistant)",
        "model_type": "reasoner",
        "group": "profession",
        "placement": "hosted",
        "why": "Code review and diagnosis, over whole files "
               "(personas/programmer.md). Answers the members who do not have "
               "the Programmer switched on, and rescues the ones who do when "
               "their opencode is down -- see docs/opencode-programmer.md.",
        "requires": {"context": 256_000, "max_output": 32_000},
        "prefer": "input",
        # The house's own bake-off, not a table's opinion. The long version is
        # `_comment_modelProfiles` in services/nanobot/config/config.json; this
        # is the part somebody choosing a model on this page needs.
        "measured": ("2026-08-16: two prompts (a 502-behind-Caddy diagnosis and "
                     "a SQL-injection review) across seven candidates. kimi-k3 "
                     "won both outright. Its code-specialised sibling "
                     "kimi-k2.7-code lost on prose, not on code -- two regional "
                     "registers in one sentence -- and was wrong about "
                     "`with sqlite3.connect()`, which is a transaction context "
                     "manager and not a closing one. deepseek-v4-pro was the "
                     "runner-up; minimax-m3 emitted its <think> block into the "
                     "reply."),
    },
    "teacher": {
        "label": "Teacher",
        "model_type": "long_output",
        "group": "profession",
        "placement": "hosted",
        "why": "Guides, tests and mark schemes: long output, and a format to "
               "follow exactly (personas/teacher.md).",
        "requires": {"max_output": 32_000, "context": 200_000},
        "prefer": "output",
    },
    "designer": {
        "label": "Designer",
        "model_type": "long_output",
        "group": "profession",
        "placement": "hosted",
        "why": "A whole HTML page in one answer, print CSS and all "
               "(personas/designer.md).",
        "requires": {"max_output": 32_000},
        "prefer": "output",
    },
    "notifications": {
        "label": "Notification triage — capped per block",
        "model_type": "small",
        "group": "unattended",
        "why": "The highest-volume turn in the house by an order of magnitude "
               "and the smallest — two thirds answer SILENCE in eight tokens. "
               "Input price is the whole bill.",
        "requires": {},
        "prefer": "input",
        # Bounded again as of 2026-09-02, and that is what changed the answer
        # here. The triage session used to carry the whole day -- 362 turns on
        # 2026-08-17, the prompt climbing +278 tokens each time from 24,782 to
        # 129,660 -- so 223 of them crossed 64k and the honest advice was
        # "hosted". HomeCore now rotates the session every
        # NOTIF_SESSION_TURNS turns and hands the next block a digest of what
        # was already said, which holds the prompt near its 25k floor.
        #
        # Kept "hosted" rather than flipped, deliberately: the cap is new, the
        # measurement behind it is a day old, and a role whose ceiling is
        # "how busy was today" earns local only once a busy day has been
        # watched under the cap. Revisit with a week of data.
        "placement": "hosted",
        "measured": ("2026-09-02: the session now rotates every 40 turns and "
                     "carries a digest forward, holding the prompt near 25k. "
                     "Before that, 2026-08-17: 86k prompt tokens on average for a 21-token "
                     "answer, because the session accumulates the whole day. "
                     "Capping that history is worth ~10x any model swap."),
    },
    # The house talking to itself. Chores falling due, somebody arriving home --
    # machine-triggered, nobody waiting, and *bounded*: measured over 631 runs
    # these peak at 32,416 tokens and have never once crossed 60k, because each
    # one is a single event and a short session rather than an accumulating
    # conversation. That bound is what makes them the one household job a small
    # local model can hold end to end.
    #
    # They had no role of their own until now, so they rode `everyday` -- which
    # also carries ordinary chat, and ordinary chat reaches 972k. One role
    # cannot be both, and the cheap half was paying hosted prices for it.
    "classifier": {
        "label": "The turn classifier — chat, action or complex?",
        "model_type": "small",
        "group": "unattended",
        "placement": "local",
        "why": "Before every chat turn and every unmarked sub-agent task, one "
               "short question: is this a quick answer, one action, or the "
               "kind of multi-step, multi-system work the strong model should "
               "start on. It is asked on every turn and somebody is waiting, "
               "so it has to be fast and free -- the box in the cupboard, at "
               "reasoning effort none. What it gets wrong, escalation after "
               "the fact catches; see docs/routing.md.",
        "requires": {"context": 8_000},
        "prefer": "input",
        "prefer_local": True,
    },
    "heartbeat": {
        "label": "The heartbeat — is there anything to do?",
        "model_type": "small",
        "group": "unattended",
        "placement": "local",
        "why": "Every thirty minutes, in every assistant, one question about "
               "a file a few hundred bytes long: is there an active task or "
               "not. Two thirds of the answers are 'no'. Nobody is waiting "
               "and nothing about it is hard, so it belongs on the box in the "
               "cupboard beside notification triage and household events.",
        "requires": {"context": 16_000},
        "prefer": "input",
        "prefer_local": True,
        "measured": ("2026-09-09: HEARTBEAT.md is 369 bytes, the turn answers "
                     "in ~1.5s, and it fires 48 times a day in each of six "
                     "containers. It had no role of its own until now, so it "
                     "rode `everyday` -- which is also ordinary chat, and "
                     "ordinary chat is not a 250-token question."),
    },
    "events": {
        "label": "Household events — always short",
        "model_type": "small",
        "group": "unattended",
        "placement": "local",
        "why": "Chores coming due and people arriving home: the house asking "
               "Alfred to phrase something, rather than a person asking for "
               "anything. Nobody is waiting, and each one is a single short "
               "session -- so this is the household job that fits on your own "
               "hardware without a caveat.",
        "requires": {"context": 60_000},
        "prefer": "input",
        "prefer_local": True,
        "measured": ("2026-09-01: 519 chore turns and 112 location turns, "
                     "average 25.8k tokens, largest 32,416, none above 60k."),
    },
    # Not the assistant: the portal names every conversation after its first
    # exchange, through home-core's own titler (CHAT_TITLE_MODEL). It is on
    # this page because it is a model the house runs and, until 2026-09-10, the
    # only one that could not be changed here -- it was a default in app.py.
    "titles": {
        "label": "Chat titles",
        "model_type": "small",
        "group": "unattended",
        "placement": "local",
        "why": "A 3-to-6-word name for each conversation in the portal, asked "
               "for once per chat with a 24-token budget. Nobody waits for it "
               "and nothing depends on it, so the only thing that matters is "
               "that the model is already loaded: a separate one evicts "
               "something doing real work. Left blank, the portal uses the "
               "model the text roles keep resident. Thinking is switched off "
               "on the call, because a reasoning model spends the whole "
               "budget thinking and the chat never gets a name.",
        "requires": {},
        "prefer": "input",
        "prefer_local": True,
        "measured": ("2026-09-10: ornith-1.5:9b, five real-shaped chats, "
                     "median 0.29s, titles at least as good as qwen3.5:4b's "
                     "at 0.20s -- and no second model on the card."),
    },
    "vision": {
        "label": "Vision",
        "model_type": "multimodal",
        "group": "special",
        "why": "Camera frames and any photo somebody sends. Local by default, "
               "where the images never leave the house and cost nothing.",
        "requires": {"vision": True},
        "prefer": "input",
        "prefer_local": True,
        "placement": "local",
    },
    # Paperless's built-in AI (PAPERLESS_AI_LLM_*): suggests a title, tags, a
    # correspondent, a type and a date. From the document's OCR *text* -- it
    # never sends the scan (paperless_ai/ai_classifier.py, 3.1.3) -- so this
    # is a small text model, not a vision one. It said vision until
    # 2026-09-12, and pinned minicpm-v:8b, which could not call tools and so
    # could never have answered Paperless's OpenAI client. Local, because
    # those are the household's documents.
    "documents": {
        "label": "Documents (Paperless)",
        "model_type": "small",
        "group": "special",
        "placement": "local",
        "why": "What Paperless's AI suggests a new document's title, tags, "
               "correspondent, type and date with. It reads the text "
               "Paperless got out of the scan, not the image, so any model "
               "that writes JSON will do -- and every page of every document "
               "goes through it, which is the strongest case in the house for "
               "a model that never leaves it. Left blank, Paperless's AI is off.",
        "requires": {},
        "prefer": "input",
        "prefer_local": True,
        "measured": ("2026-09-12: gemma4:e4b, a Spanish electricity bill, "
                     "10.6s, Spanish tags, the right correspondent and type."),
    },
    "doctor": {
        "label": "Doctor",
        "model_type": "careful",
        "group": "profession",
        "placement": "hosted",
        "why": "A profession, like Programmer and Teacher: health questions, "
               "read carefully and answered plainly. Its own model because "
               "each profession names one and uses it.",
        "requires": {"context": 200_000},
        "prefer": "input",
    },
    "legal": {
        "label": "Legal",
        "model_type": "careful",
        "group": "profession",
        "placement": "hosted",
        "why": "A profession: contracts and paperwork, where the whole "
               "document has to fit and the wording matters more than the "
               "speed.",
        "requires": {"context": 200_000, "max_output": 32_000},
        "prefer": "input",
    },
    "fallback": {
        "label": "Outage fallback",
        "model_type": "different",
        "group": "special",
        "placement": "elsewhere",
        "why": "The model every other hosted role swings onto when the one it "
               "asked for keeps answering with errors. It is not a cheaper "
               "tier or a second opinion -- it is the same job as Everyday "
               "Alfred, done on a bad day, so it needs to be able to do that "
               "job and it must not be a model already in use above. It may "
               "run on any provider -- and on a house with one provider it "
               "probably should run on another, because a fallback that "
               "shares a provider with every role goes down with it.",
        "requires": {"context": 200_000},
        "prefer": "cache_read",
    },
}


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

# Sent on every request out of this module. Without it urllib introduces
# itself as `Python-urllib/3.12`, and models.dev answers 403 to that -- so
# "Consultar ahora" reported the house as offline while every other thing on
# the box could reach the same URL. Any value at all gets a 200; a name is
# better than a browser string nobody here is, and it is what shows up in
# somebody else's logs when this asks them for a price list.
USER_AGENT = "home-stack-admin (+https://models.dev)"


def _get(url: str, timeout: int = 20, headers: dict | None = None) -> dict:
    # Default, not an override: fetch_ollama passes an Authorization header and
    # has to keep it.
    sent = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, headers=sent)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _zen_offered(api_key: str) -> set[str] | None:
    """The model ids this key is actually offered, or None if it cannot be read.

    Zen's own `/v1/models`, which answers the question models.dev cannot: that
    catalogue lists the whole roster -- 97 models -- and an account is offered a
    subset, with nothing in any field to say which. This key sees 12.

    A listing, not a probe. One GET, no generation, nothing billed. It replaces
    asking 97 models one token each and reading the refusals, which is how this
    was decided before -- and which hid models the household had gone and
    enabled, because a refusal was indistinguishable from a bad minute.

    None on any failure, and the caller then keeps the whole catalogue: a
    provider that will not say what it offers is not a reason to empty a picker.
    """
    try:
        data = _get(f"{ZEN_API_BASE}/models", timeout=20,
                    headers={"Authorization": f"Bearer {api_key}",
                             **_opencode_session(ZEN_API_BASE)})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    rows = data if isinstance(data, list) else (data.get("data") or [])
    ids = {str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")}
    return ids or None


# The flat plan's roster, and the one place in this stack allowed to hold it.
#
# `opencode-go` is a *models.dev* provider key, and models.dev is the only
# source this may use: the flat plan's own endpoint is `/zen/go/v1`, which
# CLAUDE.md forbids this stack from calling at all. Listing what the plan
# offers and calling it are different acts, and only the second is the one the
# account can be blocked for. Nothing here sends a request to Go.
#
# Deliberately NOT added to SOURCES. That tuple drives every role picker on
# this page, and a Go model reaching `assistant.models` is precisely the thing
# CLAUDE.md is about -- five containers on one key, unattended, around the
# clock. These belong to `cloud.opencode.model` and nowhere else, because that
# one is spent by a person sitting at a keyboard asking for code.
GO_PROVIDER = "opencode-go"

# Not `opencode_go`: that name belongs to RETIRED_SOURCE, and load_cache()
# migrates anything under it into `opencode_zen`. Reusing the string would make
# every Go model silently reappear in the Zen roster on the next page load,
# which is the one outcome this whole arrangement exists to prevent.
GO_CACHE_KEY = "code_go"


def fetch_opencode_go(data: dict | None = None) -> tuple[list[dict], str]:
    """(models, error) for the flat plan, from models.dev. Never raises.

    `data` is an already-fetched models.dev payload. Both OpenCode rosters come
    out of the same document, and fetching it twice per refresh was a second
    ~0.8s on a page somebody is waiting for.
    """
    if data is None:
        try:
            data = _get(MODELS_DEV)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return [], f"could not reach models.dev: {exc}"
    provider = data.get(GO_PROVIDER) or {}
    out = []
    for mid, m in (provider.get("models") or {}).items():
        limit = m.get("limit") or {}
        out.append({
            "id": mid,
            "name": m.get("name", mid),
            "provider": GO_CACHE_KEY,
            "context": limit.get("context"),
            "reasoning": bool(m.get("reasoning")),
            "vision": "image" in ((m.get("modalities") or {}).get("input") or []),
        })
    out.sort(key=lambda m: m["id"])
    return out, ""


def code_model_choices(catalogue: dict) -> list[dict]:
    """The picker for `cloud.opencode.model`: what opencode may answer as.

    Two groups, because they are two bargains. Go is flat-rate and is what the
    household already pays for; Zen is per token and is filtered to what the
    key is actually offered, since a model the account cannot reach fails as
    `Model is disabled` mid-question rather than at the moment of choosing.

    The value is `"<providerID>/<modelID>"` -- opencode's own spelling, written
    straight into the agent's front matter by the deployer.
    """
    groups = []
    go = [m for m in (catalogue.get(GO_CACHE_KEY) or []) if isinstance(m, dict)]
    if go:
        groups.append({"key": "go", "models": [
            {"id": f"{GO_PROVIDER}/{m['id']}", "label": m["id"],
             "context": m.get("context")} for m in go]})
    # Already trimmed to what this key is offered, by fetch_opencode_zen().
    zen = [m for m in (catalogue.get("opencode_zen") or []) if isinstance(m, dict)]
    if zen:
        groups.append({"key": "zen", "models": [
            {"id": f"opencode/{m['id']}", "label": m["id"],
             "context": m.get("context")} for m in zen]})
    return groups


def is_go_model(value: str) -> bool:
    """True for a flat-plan model, wherever the string turns up.

    The picker keeps these out of the role lists; this is what a POST gets, and
    the two are only the same until somebody edits the form or replays a save.
    """
    return str(value or "").strip().lower().startswith(f"{GO_PROVIDER}/")


def fetch_opencode_zen(api_key: str = "", verdicts: dict | None = None,
                       data: dict | None = None) -> tuple[list[dict], str]:
    """(models, error). Never raises: a page that cannot reach models.dev
    should show the cache and say it is old, not return a 500.

    `data` is an already-fetched models.dev payload -- see fetch_opencode_go().
    """
    if data is None:
        try:
            data = _get(MODELS_DEV)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return [], f"could not reach models.dev: {exc}"
    provider = data.get(MODELS_DEV_PROVIDER) or {}
    out = []
    for mid, m in (provider.get("models") or {}).items():
        cost = m.get("cost") or {}
        limit = m.get("limit") or {}
        out.append({
            "id": mid,
            "name": m.get("name", mid),
            # The SOURCES key, not the models.dev one. See above.
            "provider": "opencode_zen",
            "input": cost.get("input"),
            "output": cost.get("output"),
            "cache_read": cost.get("cache_read"),
            "context": limit.get("context"),
            "max_output": limit.get("output"),
            "reasoning": bool(m.get("reasoning")),
            "vision": "image" in ((m.get("modalities") or {}).get("input") or []),
            # Read off the same list as vision rather than guessed. Both
            # are filters on the roster, and a filter that guesses hides
            # models that would have worked.
            "audio": "audio" in ((m.get("modalities") or {}).get("input") or []),
            "release_date": m.get("release_date"),
        })
    # Every id models.dev knows, taken before anything is filtered. It is what
    # tells the roster check below the difference between "this account has a
    # model the catalogue has not heard of yet" and "this model was removed here
    # on purpose" -- and computing it after the free-tier filter would put the
    # free tier back, unpriced, which is both a policy undone and a lie about
    # the price.
    catalogued = {m["id"] for m in out}

    # Minus the free tier, and this is a policy filter rather than a technical
    # one -- the distinction matters, because these models *work*. Measured
    # 2026-09-02: `big-pickle` costs nothing and answers 200 on this account.
    #
    # It is refused for the same reason as the flat Go plan, written up at the
    # top of this file and in CLAUDE.md: free capacity is not meant for five
    # containers running unattended around the clock, roughly 1,800 turns a
    # month with nobody at a keyboard. Offering it on this page would be
    # inviting a household to spend its way into a blocked account, and a
    # picker that offers something the terms forbid is worse than one that is
    # merely short of options.
    out = [m for m in out if m.get("input")]

    # Minus what this key is not offered, which is the separate technical
    # question -- and the provider answers it directly. `grok-code` and
    # `glm-5-free` are not on this account; `deepseek-v4-flash` is. A model in
    # the picker that the key cannot route is a failed deploy later, and a model
    # missing from the picker that the household just enabled is an hour spent
    # looking for it. The roster settles both.
    offered = _zen_offered(api_key) if api_key else None
    if offered is not None:
        # Anything the account is offered but models.dev has never heard of is
        # kept, unpriced. A new model reaches the provider before the catalogue.
        out = [m for m in out if m["id"] in offered]
        out += [{"id": mid, "name": mid, "provider": "opencode_zen",
                 "input": None, "output": None, "cache_read": None,
                 "context": None, "max_output": None, "reasoning": False,
                 "vision": False, "audio": False, "release_date": None}
                for mid in sorted(offered - catalogued)]
    out = _zen_drop_unsupported(out, api_key, verdicts)
    return sorted(out, key=lambda m: (m["input"] if m["input"] is not None else 1e9)), ""


def fetch_ollama(base_url: str, api_key: str = "",
                 provider: str = "ollama") -> tuple[list[dict], str]:
    """What the configured Ollama has actually pulled.

    Asked rather than assumed: recommending a model that is not on the box is
    the same as recommending nothing, and the failure — a 404 on the first
    turn — reads as the assistant being broken.

    `provider` separates the two Ollamas. They speak the same API and mean
    different things: `ollama` is hardware you own and nothing leaves the
    house, `ollama_cloud` is ollama.com and what you send it does. Folding them
    into one list would put "free, and private" beside "billed, and not" under
    the same heading.
    """
    headers = {}
    if api_key and api_key != "ollama":
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        data = _get(f"{base_url.rstrip('/')}/api/tags", timeout=8, headers=headers)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return [], f"could not reach Ollama at {base_url}: {exc}"
    out = []
    for m in data.get("models") or []:
        name = m.get("name") or m.get("model") or ""
        if not name:
            continue
        details = m.get("details") or {}
        family = (details.get("family") or "").lower()
        # `ollama:`, `ollama-cloud:` and `ollama-vision:` are the prefixes
        # assistant.models uses (MODEL_PROVIDERS in deploy.py), so what this
        # page offers is exactly what the config takes -- and a model pulled
        # on the vision instance is never mistaken for one on the text one.
        prefix = {"ollama_cloud": "ollama-cloud",
                  "ollama_vision": "ollama-vision"}.get(provider, "ollama")
        out.append({
            "id": f"{prefix}:{name}",
            "name": name,
            "provider": provider,
            # A local Ollama is free at the point of use: your electricity, not
            # a per-token bill, and pretending otherwise would make every
            # comparison on this page meaningless. ollama.com is *not* free and
            # does not publish a per-model price here, so it is left unknown
            # rather than stated as zero -- a made-up zero is what would put it
            # top of every cheapest-first list.
            **({"input": 0.0, "output": 0.0, "cache_read": 0.0}
               if provider in ("ollama", "ollama_vision") else
               {"input": None, "output": None, "cache_read": None}),
            "context": None, "max_output": None,
            "reasoning": False,
            # `vl`/`vision`/`llava` in the family is how Ollama names its
            # multimodal builds.
            "vision": any(t in family or t in name.lower()
                          for t in ("vl", "vision", "llava", "minicpm-v")),
            # Ollama publishes no modality list, and there is no naming
            # convention for audio the way there is for `-vl`. Left False
            # rather than guessed at.
            "audio": False,
            "size": m.get("size"),
        })
    return sorted(out, key=lambda m: m["name"]), ""


def _rows(data) -> list:
    """The model list, whichever envelope it arrived in.

    OpenAI and OpenRouter answer `{"data": [...]}`; Together answers a bare
    array. Accepting both here rather than in three fetchers means a provider
    that changes its mind is one line.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, list):
            return rows
    return []


def _per_million(value) -> float | None:
    """A price this page can compare, or None.

    Every price here is dollars per million tokens, because that is the unit
    the OpenCode Zen roster uses and a column that mixes units is worse than an
    empty one. OpenRouter quotes dollars *per token*, as a string. None for
    anything unparseable: `_cheapest` sorts None last and `_reason` omits it,
    so an unknown price costs a recommendation rather than a wrong number.
    """
    try:
        # Rounded, because the multiplication is where the noise comes from:
        # 0.0000008 * 1e6 is 0.7999999999999999, and the roster prints the
        # value as-is. Six places keeps the sub-cent prices that some of these
        # actually charge per million.
        return round(float(value) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None


def fetch_openrouter(api_key: str) -> tuple[list[dict], str]:
    """openrouter.ai's roster: hundreds of models behind one key.

    The richest of the three -- prices, context window, output ceiling and the
    input modalities -- so these are the only bought-in models besides OpenCode
    Go that this page can actually rank.
    """
    try:
        data = _get("https://openrouter.ai/api/v1/models", timeout=20,
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else None)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return [], f"could not reach openrouter.ai: {exc}"
    out = []
    for m in _rows(data):
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        arch = m.get("architecture") or {}
        top = m.get("top_provider") or {}
        out.append({
            "id": f"openrouter:{mid}",
            "name": m.get("name") or mid,
            "provider": "openrouter",
            "input": _per_million(pricing.get("prompt")),
            "output": _per_million(pricing.get("completion")),
            "cache_read": _per_million(pricing.get("input_cache_read")),
            "context": m.get("context_length") or top.get("context_length"),
            "max_output": top.get("max_completion_tokens"),
            "reasoning": bool(m.get("supported_parameters")
                              and "reasoning" in (m.get("supported_parameters") or [])),
            # Stated by the provider rather than guessed from the name, which
            # is what the Ollama fetcher has to do.
            "vision": "image" in (arch.get("input_modalities") or []),
            "audio": "audio" in (arch.get("input_modalities") or []),
        })
    return sorted(out, key=lambda m: m["name"]), ""


# What together.ai says when a model exists, is priced, is in `/v1/models`, and
# cannot be called on a serverless key. It is the ONLY 400 that means this --
# `This model only supports streaming` is another one, and those models work.
_TOGETHER_DEDICATED = "non-serverless"

# The refusal OpenCode Zen gives for a model this key cannot route. Measured
# 2026-09-02 against the live account: `glm-5-free`, `grok-code`,
# `kimi-k2.5-free`, `mimo-v2.5-free`, `ling-3.0-flash-free` and
# `longcat-2.0-free` all answer 401 `Model … is not supported`, while
# `deepseek-v4-flash` and -- notably -- the $0 `big-pickle` answer 200.
#
# So neither the price nor the name is the tell. Zero cost does not mean
# unusable (big-pickle is free and works) and the `-free` suffix does not mean
# unusable either (grok-code has no suffix and is refused). The only reliable
# discriminator is asking, which is what Together already needed for the same
# reason -- see `_together_callable` above and the verdict cache that makes it
# affordable.
# Two refusals, both arriving as a 401. "is not supported" is the key that
# cannot route the model; "Model is disabled" is the gateway having withdrawn
# it -- on 2026-09-02 Zen served 7 models to this account while models.dev
# still listed 56, so 49 of the picker's options answered nothing. The roster
# comes from models.dev, which describes OpenCode's whole catalogue rather
# than what one key may call, and this probe is the only thing that knows the
# difference.
_ZEN_UNSUPPORTED = ("is not supported", "model is disabled")


# Zen models this house cannot actually use, whatever the catalogue says.
#
# Measured 2026-09-03, each one repeatedly, and on *two* different 67-character
# keys (the env file's and the one `opencode serve` holds in its own auth.json)
# -- byte-identical results, so this is not a key entitlement and not the
# gateway flapping. `_zen_callable` deliberately keeps a 5xx model on the list
# because a 5xx usually is a bad night; these are the exception it could not
# know about, and they fail every time.
#
# Six of them are one bug, not six: they answer **200 on `/v1/responses`** and
# 500 on `/v1/chat/completions`, and nanobot only ever posts to the latter --
# `_should_use_responses_api()` in the provider returns False for any gateway
# that is not literally api.openai.com, so the `gpt-5` test right below it
# never runs. Fix that and this list loses six entries; until then a household
# picking one of them gets an assistant that cannot answer.
#
# `gemini-3.8-flash` is not that: it 500s on both paths and has no way through.
# What this house has measured about particular Zen models. A **note**, not a
# filter: these were hidden from the picker for a while and the household could
# not find models it had deliberately enabled, which is worse than offering one
# that may not answer. Kept because the knowledge is real and was expensive --
# it took a full sweep of 64 models to establish -- and shown beside the model
# so the choice is made with it rather than against it.
_ZEN_NOTES = {
    # The gpt-5 family is no longer listed here. They answer on /v1/responses
    # and 500 on /chat/completions, and nanobot now routes them accordingly --
    # `_should_use_responses_api` treats an opencode.ai base as a split by
    # model. A note that says "this will not work" about a model that now works
    # is worse than no note: it moves a household off a model for a reason that
    # has been fixed.
    #
    # muse-spark stays, and for a precise reason: it needs /responses too, but
    # its name carries no `gpt-5`, so the family test that routes the others
    # does not catch it. It is free-tier and therefore out of the picker anyway;
    # this is here so the next person to wonder does not have to measure it
    # again.
    "muse-spark-1.3-contributor-free":
        "needs /v1/responses, and the gpt-5 name test does not catch it",
    "gemini-3.8-flash": "500s on /chat/completions and /responses alike",
}


# Whether this page may call a provider to find out what works.
#
# Off. It was on, and it is why the picker was short: every refresh asked ~150
# Together models and 64 Zen ones whether they answer, and anything that said no
# was dropped. That is real protection -- two roles in this house were once
# configured to models that 400 on every turn -- but it is protection bought by
# firing paid requests at two providers on a schedule, and by hiding models the
# household had deliberately enabled and then could not find.
#
# The knowledge is kept and shown (`_ZEN_NOTES`, and any cached verdict) rather
# than acted on. Set HOME_STACK_PROBE_MODELS=1 to have the page ask again.
PROBE_MODELS = os.environ.get("HOME_STACK_PROBE_MODELS", "0") not in ("0", "", "false")


def _annotate_zen(rows: list[dict]) -> list[dict]:
    """Attach what is known about a model instead of removing it."""
    for row in rows:
        note = _ZEN_NOTES.get(row.get("id"))
        if note:
            row["note"] = note
    return rows


def _zen_callable(api_key: str, model_id: str) -> bool | None:
    """Ask Zen one token about *model_id*. True / False / None for "cannot tell".

    Same conservatism as the Together probe: a model is dropped only on the
    specific refusal that means this key cannot route it. A 5xx, a timeout, a
    rate limit or an upstream error leaves it on the list -- `deepseek-v4-flash
    -free` answers 400 "Error from provider (Console): Upstream request…",
    which reads like a bad night rather than a permanent no, and emptying the
    picker on one of those teaches a household to distrust the page.
    """
    body = json.dumps({"model": model_id, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ok"}]}).encode()
    req = urllib.request.Request(
        f"{ZEN_API_BASE}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT,
                 "Content-Type": "application/json",
                 **_opencode_session(ZEN_API_BASE)})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return None
        try:
            detail = (exc.read() or b"").decode(errors="replace").lower()
        except Exception:  # noqa: BLE001 - a gateway may answer badly
            return None
        return False if any(m in detail for m in _ZEN_UNSUPPORTED) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _zen_drop_unsupported(rows: list[dict], api_key: str,
                          verdicts: dict | None = None) -> list[dict]:
    """Take out the Zen models this key cannot route.

    `verdicts` is the cache and it is what makes this affordable: the first run
    asks every model and takes about a minute, and every run after it asks only
    what is new. Only a definite answer is remembered -- "could not tell" stays
    unknown so the next refresh asks again, rather than freezing one bad minute
    into a permanent verdict.
    """
    rows = _annotate_zen(rows)
    if not api_key or not PROBE_MODELS:
        return rows
    seen = verdicts if verdicts is not None else {}
    ask = [r for r in rows if r["id"] not in seen]
    if ask:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            for row, verdict in zip(ask, pool.map(
                    lambda r: _zen_callable(api_key, r["id"]), ask)):
                if verdict is not None:
                    seen[row["id"]] = verdict
    return [r for r in rows if seen.get(r["id"], True) is not False]


def _together_serverless(api_key: str) -> set[str]:
    """Models together.ai's endpoint registry marks serverless.

    Sound and incomplete: nothing this lists has failed a real call, but it
    covered 16 of 170 chat models on the day it was written -- and the four it
    missed included the model this household runs everything on. So it is used
    to *skip* probes, never to hide a model.
    """
    try:
        data = _get("https://api.together.xyz/v1/endpoints", timeout=20,
                    headers={"Authorization": f"Bearer {api_key}"})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return set()
    return {(e.get("model") or "").strip() for e in _rows(data)
            if (e.get("type") or "") == "serverless" and e.get("model")}


def _together_callable(api_key: str, model_id: str) -> bool | None:
    """Ask the model one token. True / False / None for "could not tell".

    None is the important one. A model is dropped only on the specific refusal
    that means it needs a dedicated endpoint; a 5xx, a timeout, a rate limit or
    a shape this has never seen leaves it on the list. A bad night at the
    provider must not empty the picker -- the household would read that as
    "these models are gone" and rewrite a working config.
    """
    body = json.dumps({"model": model_id, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ok"}]}).encode()
    req = urllib.request.Request(
        "https://api.together.xyz/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            return None
        try:
            detail = (exc.read() or b"").decode(errors="replace").lower()
        except Exception:  # noqa: BLE001 - a gateway may answer badly
            return None
        return False if _TOGETHER_DEDICATED in detail else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _together_drop_dedicated(rows: list[dict], api_key: str,
                             verdicts: dict | None = None) -> list[dict]:
    """Take out the models a serverless key cannot call.

    together.ai lists 170 chat models and serves a minority of them on a
    serverless key; the rest need a dedicated endpoint you rent by the hour.
    Nothing in `/v1/models` says which is which -- a working model and a
    refused one were diffed field by field and differ only in name, context
    length and price. The catalogue price is not the tell either: models at
    $1.74, $0.95 and $0.45 per million are all refused.

    So they are asked. Two roles in this household were configured from this
    list to models that answer 400 on every turn, and one of them --
    `programmer` -- gets no retry and no fallback, because a 400 is neither
    transient nor model-specific. It simply fails, with the household reading
    it as "the assistant is broken".
    """
    if not api_key or not PROBE_MODELS:
        # Everything together.ai lists. A model that needs a dedicated endpoint
        # is still shown: the household asked to see the whole roster, and a
        # short list with no explanation is harder to act on than a long one.
        return rows
    # `verdicts` is the cache and it is why this is affordable: the first run
    # asks ~150 models and takes half a minute, and every run after it asks
    # only what is new. Without it the models page blocks for 33 seconds on a
    # fresh install, which is the one visit where it must not.
    seen = verdicts if verdicts is not None else {}
    known = _together_serverless(api_key) if any(
        r["id"] not in seen for r in rows) else set()
    ask = [r for r in rows
           if r["id"] not in seen
           and r["id"].split(":", 1)[-1] not in known
           and not r.get("audio") and not r.get("vision")]
    if ask:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            for row, verdict in zip(ask, pool.map(
                    lambda r: _together_callable(api_key, r["id"].split(":", 1)[-1]),
                    ask)):
                # Only a definite answer is remembered. "Could not tell" stays
                # unknown so the next refresh asks again, rather than freezing
                # a rate-limited minute into a permanent verdict.
                if verdict is not None:
                    seen[row["id"]] = verdict
    return [r for r in rows if seen.get(r["id"], True) is not False]


def _ollama_cloud_callable(base_url: str, api_key: str, model_id: str) -> bool | None:
    """Ask ollama.com for one token. True / False / None for "could not tell".

    `/api/tags` on ollama.com lists the whole cloud roster, not the part a
    given key may call, and nothing on the row says which is which. Measured
    on this household's key: 20 models listed, 4 callable. `gpt-oss:20b`,
    `gpt-oss:120b`, `gemma4:31b` and `nemotron-3-nano:30b` answer; every
    deepseek, glm, kimi and qwen variant answers

        402 {"error": {"message": "this model requires a subscription or
             usage credits, upgrade for access at ..."}}

    402 is the whole signal and it is unambiguous -- payment required, not a
    bad night -- so it is the only verdict that drops a model. A 5xx, a
    timeout, a rate limit or a shape this has never seen returns None and
    leaves the model on the list, for the same reason the Zen and together
    probes do: an outage must not empty the picker, because the household
    reads an empty picker as "these models are gone" and rewrites a working
    config.
    """
    body = json.dumps({"model": model_id, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ok"}]}).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT,
                 "Content-Type": "application/json"})
    # 429 is retried rather than shrugged at. ollama.com answers
    # `{"error": "too many concurrent requests"}` under any real parallelism --
    # measured 7 of 20 models at eight workers -- and a throttled probe that
    # returned None would leave a paywalled model sitting in the picker, which
    # is the exact failure this function exists to prevent. Serially each model
    # answers in 0.3-0.8s, so a short backoff is enough to get a real verdict.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 402:
                # A reasoning model spends a 1-token budget on reasoning and
                # returns empty content, which is still a 200 and still
                # callable. Only the paywall is a no.
                return False
            if exc.code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
    return None


def _ollama_cloud_drop_paywalled(rows: list[dict], base_url: str, api_key: str,
                                 verdicts: dict | None = None) -> list[dict]:
    """Take out the cloud models this key cannot call.

    Same failure this file already handles twice over, on a third provider:
    a role configured from the full roster lands on a model that 402s on every
    turn. Everyday Alfred was pointed at a deepseek variant and answered
    nothing, which is what prompted this.

    Unlike the local Ollama -- where every listed model is pulled and therefore
    callable -- the cloud list is a shop window. Only `ollama_cloud` is probed;
    `ollama` and `ollama_vision` are left alone.
    """
    if not api_key or not PROBE_MODELS:
        # The whole roster, as asked for. A paywalled model still shown is
        # better than a short list with no explanation -- and with no key there
        # is nothing to probe with anyway.
        return rows
    seen = verdicts if verdicts is not None else {}
    ask = [r for r in rows if r["id"] not in seen]
    if ask:
        from concurrent.futures import ThreadPoolExecutor
        # Two workers, not the eight the other probes use. ollama.com rejects
        # real parallelism with 429 "too many concurrent requests", and at
        # eight it throttled 7 of 20 -- so a wider pool finished no sooner and
        # answered less. Twenty models at ~0.5s each is a few seconds on a cold
        # cache and nothing on a warm one.
        with ThreadPoolExecutor(max_workers=2) as pool:
            for row, verdict in zip(ask, pool.map(
                    lambda r: _ollama_cloud_callable(
                        base_url, api_key, r["id"].split(":", 1)[-1]),
                    ask)):
                if verdict is not None:
                    seen[row["id"]] = verdict
    return [r for r in rows if seen.get(r["id"], True) is not False]


def fetch_together(api_key: str, verdicts: dict | None = None) -> tuple[list[dict], str]:
    """together.ai's roster, minus what a serverless key cannot call.

    Their prices are already per million, unlike OpenRouter's, so they are
    taken as given -- and anything that will not parse becomes None rather than
    a number in the wrong unit. Every field is still read defensively, but the
    shape below is now one measured against a live account rather than one read
    off the documentation, which corrected three things:

    `cached_input` is published and was being thrown away. `cache_read` was
    hard-coded to None here, so the column was empty for every Together model
    and the two personas that rank on it -- Everyday Alfred and the outage
    fallback, the two that re-send a conversation -- were ranking on a number
    nobody had. It is the price that decides those roles.

    `type` is declared, and 108 of the 280 rows are not chat: 38 video, 29
    image, 15 audio, plus embeddings, rerankers and transcribers. They price in
    megapixels or seconds of footage and report `input: 0`, which is not a
    free chat model -- it is a price this API does not express in tokens. Kept
    on the row as `kind` so `recommend()` can leave them out of a ranking they
    would otherwise win outright.

    `type == "image"` was being read as "takes images". It means the opposite:
    those are the endpoints that *draw* one. The roster's Images column says
    "reads images", so FLUX and Imagen were ticked in a column they cannot do.
    """
    if not api_key:
        return [], ""
    try:
        data = _get("https://api.together.xyz/v1/models", timeout=20,
                    headers={"Authorization": f"Bearer {api_key}"})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return [], f"could not reach together.ai: {exc}"
    out = []
    for m in _rows(data):
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        pricing = m.get("pricing") or {}

        def dollars(key):
            try:
                return float(pricing[key])
            except (KeyError, TypeError, ValueError):
                return None

        out.append({
            "id": f"together:{mid}",
            "name": m.get("display_name") or mid,
            "provider": "together",
            "input": dollars("input"),
            "output": dollars("output"),
            "cache_read": dollars("cached_input"),
            "context": m.get("context_length"),
            # Published on 9 of together.ai's 172 chat models, and absent on
            # the rest -- so it is read where it exists and left unknown where
            # it does not.
            "max_output": ((m.get("config") or {}).get("max_output_length")
                           or None),
            # None, not False. together.ai publishes no reasoning flag at all,
            # and `False` is a claim it never made -- it says "this model does
            # not reason", which would rank a reasoner below a non-reasoner for
            # a role that wants one, on evidence that does not exist. Unknown
            # is the honest value and the page renders it as unknown.
            "reasoning": None,
            "kind": (m.get("type") or "").strip().lower(),
            "vision": "vision" in mid.lower() or "-vl" in mid.lower(),
            "audio": (m.get("type") or "") == "audio"
                     or "audio" in mid.lower() or "whisper" in mid.lower(),
        })
    return sorted(_together_drop_dedicated(out, api_key, verdicts),
                  key=lambda m: m["name"]), ""


def fetch_openai(api_key: str) -> tuple[list[dict], str]:
    """What this OpenAI account can reach.

    Ids and nothing else: `/v1/models` publishes no price, no context window
    and no note about images. So the capabilities come back unknown, the same
    as an OpenAI-compatible server -- which means these are selectable but
    never *recommended* for a role with a hard requirement, because nothing
    here can promise it.
    """
    if not api_key:
        return [], ""
    try:
        data = _get("https://api.openai.com/v1/models", timeout=15,
                    headers={"Authorization": f"Bearer {api_key}"})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return [], f"could not reach api.openai.com: {exc}"
    out = []
    for m in _rows(data):
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        out.append({
            "id": f"openai:{mid}",
            "name": mid,
            "provider": "openai",
            "input": None, "output": None, "cache_read": None,
            "context": None, "max_output": None,
            "reasoning": False, "vision": False, "audio": False,
        })
    return sorted(out, key=lambda m: m["name"]), ""


# The two self-hosted slots, and how a role writes each one. They share this
# fetch because they speak the same API; they are separate sources so a
# household can run both and tell them apart in the picker.
_SELF_HOSTED_PREFIX = {"openai_compatible": "openai-compatible",
                       "freetoken": "freetoken"}


def fetch_openai_compatible(base_url: str, api_key: str = "", label: str = "",
                            provider: str = "openai_compatible"
                            ) -> tuple[list[dict], str]:
    """Whatever a server that speaks the OpenAI API says it has.

    `GET /v1/models` is the one endpoint every one of them implements -- vLLM,
    llama.cpp's server, LM Studio, LocalAI, and most hosted providers. It
    returns ids and almost nothing else: no price, no context window, no note
    about whether a model can see an image. So the capabilities come back
    unknown rather than guessed.

    Unknown is not the same as no, and `meets()` already treats a missing limit
    as "do not hold it against it" -- Ollama reports none either. So a model
    from here is never recommended for a *vision* role, which is a declared
    capability it cannot claim, but it can be recommended for a long-context
    one on a limit nothing here published. That is the trade: the household
    knows what it pointed this at, and this page does not.

    The URL is taken as given, minus a trailing `/v1`, because people paste
    both forms and a doubled `/v1/v1/models` is a 404 that reads as the server
    being down.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        data = _get(f"{root}/v1/models", timeout=10, headers=headers)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return [], f"could not reach {label or base_url}: {exc}"
    out = []
    for m in data.get("data") or []:
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        out.append({
            "id": f"{_SELF_HOSTED_PREFIX[provider]}:{mid}",
            "name": mid,
            "provider": provider,
            # No price is published here, and a zero would sort this to the top
            # of every cheapest-first list on a server that may well bill.
            "input": None, "output": None, "cache_read": None,
            "context": None, "max_output": None,
            "reasoning": False, "vision": False, "audio": False,
            "label": label,
        })
    return sorted(out, key=lambda m: m["name"]), ""


def cache_is_stale(catalogue: dict, max_age_s: float) -> bool:
    """Should this cache be rebuilt -- by age, or because older code wrote it?

    Two reasons, and the second is the one that was missing. A cache 47 hours
    into a 7-day life is fresh by every measure the page had, and was still
    wrong: the code that built it filtered the Zen roster by probing, and the
    code reading it no longer does. Nothing connected the two, so a deploy that
    fixed the roster changed nothing anybody could see, and the advice was to
    press a button that rebuilt it for reasons unrelated to why it needed
    rebuilding.
    """
    if int(catalogue.get("catalogue_version") or 0) != CATALOGUE_VERSION:
        return True
    return (time.time() - (catalogue.get("checked_at") or 0)) >= max_age_s


def load_cache(path: Path) -> dict:
    """The cached catalogue, with each model's `provider` normalised.

    A cache written before these models carried `opencode_zen` holds
    `opencode-go` or `opencode` on the model, and the picker filters on the
    source key -- so without this the group stays empty until somebody happens
    to press Refresh. The cache is keyed by source already, so the source is
    the answer; the field is only repeated on each model for the template's
    benefit.

    The old top-level key is carried across for the same reason: a household
    that upgrades between two refreshes has a cache full of Go models under
    `opencode_go`, and dropping it on the floor would empty the page rather
    than showing something a little stale. They are re-fetched from Zen on the
    next refresh and the old key is not written again.
    """
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if "opencode_zen" not in cached and cached.get(RETIRED_SOURCE):
        cached["opencode_zen"] = cached.pop(RETIRED_SOURCE)
        cached.pop(f"{RETIRED_SOURCE}_at", None)
        cached.pop(f"{RETIRED_SOURCE}_error", None)
    # The second Ollama "for vision" is gone: vision runs on a setup, listed
    # with the house's other local models. The refresh has written this
    # source empty since, but a cache from before that kept offering the old
    # server's models in their own group, under a server that no longer
    # answers (2026-09-24).
    cached["ollama_vision"] = []
    cached.pop("ollama_vision_error", None)
    # ...and the per-role price lists computed from it at the last check.
    for entry in (cached.get("priced_roles") or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get("models"), list):
            entry["models"] = [
                r for r in entry["models"]
                if not str((r or {}).get("id", "")).startswith("ollama-vision:")
                and (r or {}).get("provider") != "ollama_vision"]
    for source in SOURCES:
        for model in cached.get(source) or []:
            if isinstance(model, dict):
                model["provider"] = source
    # The free tier again, on the way *out* of the cache as well as on the way
    # in. `fetch_opencode_zen` keeps it out of anything fetched from now on,
    # but a catalogue written before that filter existed still holds it, and it
    # would go on being offered on every list this page draws -- the roster,
    # every role's picker and the suggestion table -- until somebody happened
    # to press Refresh. Refused for the reasons in that fetcher: these models
    # work, and using them for this stack's traffic is what the terms forbid.
    cached["opencode_zen"] = [m for m in (cached.get("opencode_zen") or [])
                              if not isinstance(m, dict) or m.get("input")]
    return cached


def save_cache(path: Path, catalogue: dict) -> None:
    # Stamped on the way out, so a cache always says which code wrote it.
    catalogue["catalogue_version"] = CATALOGUE_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


# Every roster this page can hold, in the order it reads best. Named here so
# the fetcher, the cache and the template cannot disagree about what a source
# is -- which is how the admin page came to look for `cloud.ollama.mode` long
# after the config stopped having one.
SOURCES = ("opencode_zen", "openrouter", "together", "openai",
           "ollama", "ollama_cloud", "ollama_vision", "openai_compatible",
           "freetoken")

# What this source was called while the stack was on the flat Go plan. Read
# from an old cache, never written. See load_cache().
RETIRED_SOURCE = "opencode_go"

# Which of those bill per token, and which run on hardware the household
# already owns. This split is the whole reason `recommend()` returns two lists
# rather than one ranking: "cheapest" means a different thing on each side, and
# a free 4B model on the box in the cupboard is not a cheaper alternative to a
# hosted one -- it is a different answer to the question.
#
# `openai_compatible` and `freetoken` sit with Ollama because both are pointed
# at rather than deployed and both are somebody's own hardware. Neither
# publishes a price, so they sort to the end of their own list rather than to
# the top of everybody's -- which is what a zero would have done.
#
# Derived from SOURCES rather than listed twice, so a source added above cannot
# be quietly missing from the recommendation. That is the bug this pair exists
# to stop: `recommend()` read `catalogue["opencode_zen"]` directly, so a house
# that had moved every role to together.ai got an empty "cheapest that fits"
# for all twelve personas -- a page that looks like it has no opinion rather
# than one that was never asked.
# `ollama_vision` is the second ollama on the same hardware (see
# cloud.ollama.vision): local for every purpose this split cares about.
LOCAL_SOURCES = ("ollama", "ollama_vision", "openai_compatible", "freetoken")


def is_local_ollama(source: str) -> bool:
    """`ollama`, `ollama_vision` or any further instance (`ollama_<id>`) --
    the household's own Ollamas, never ollama.com."""
    return source == "ollama" or (bool(re.fullmatch(r"ollama_[a-z][a-z0-9]{0,15}", source or ""))
                                  and source != "ollama_cloud")
HOSTED_SOURCES = tuple(s for s in SOURCES if s not in LOCAL_SOURCES)

# The model kinds that can answer a turn. Only together.ai declares one today;
# every other fetcher leaves the field off, which is why "" is on the list --
# absent means "not stated", and excluding a whole roster on a field it never
# sets would be a filter about the catalogue rather than about the model.
#
# This is a hard filter and it belongs next to `meets()` in spirit: an image
# endpoint does not answer chat slowly, it does not answer chat. It is separate
# from IMAGE_FAMILIES below, which asks the opposite question -- which models
# can *draw* -- and matches on the name because an image slot has to work on
# providers that declare no kind at all.
CHAT_KINDS = ("chat", "language", "code", "")


def price_roles(catalogue: dict, state_dir: str) -> dict:
    """`{role: {"assumed": bool, "models": [...] }}` -- the costed advice.

    Computed with the roster rather than on every page load, and stored beside
    it, so it moves on the same clock the household already controls: the
    refresh button, and `assistant.price_check`. Two reasons that is the right
    clock rather than a performance trick.

    A recommendation that changes between two page loads is not a
    recommendation. Somebody comparing two options, scrolling away and coming
    back should find the same five in the same order, and the roster is what
    would move them.

    And a price is a fact about a rate card and a month of traffic, neither of
    which changes by the minute. Recomputing it per render would spend a
    database read and a pass over every model to produce the same number the
    page showed a second ago.
    """
    volumes = role_volumes(state_dir)
    every = {m["id"]: m for m in all_models(catalogue)}
    out: dict = {}
    for role, spec in PERSONA_NEEDS.items():
        volume = volumes.get(role)
        priced = []
        for m in every.values():
            if m.get("kind", "") not in CHAT_KINDS:
                continue
            if not meets(m, spec.get("requires") or {}):
                continue
            priced.append((monthly_cost(m, volume or ASSUMED_VOLUME), m))
        # Kind before price. Cheapest-that-fits was answering "which costs
        # least" while the page asked "which should I pick": `programmer` wants
        # a strong reasoner and was being handed the cheapest model that
        # qualifies on context alone. Three bands -- matches the kind, catalogue
        # does not say, documented not to -- and cost orders within each, so a
        # provider that publishes nothing is neither promoted nor punished for
        # its silence.
        kind = spec.get("model_type", "")
        band = {True: 0, None: 1, False: 2}
        priced.sort(key=lambda x: (band[matches_type(x[1], kind)],
                                   x[0] is None, x[0] or 0, x[1]["id"]))
        known = [c for c, _ in priced if c is not None]
        cheapest = min(known) if known else None
        out[role] = {
            "assumed": volume is None,
            "models": [
                # `note` travels with the model. It is what this house has
                # measured about it -- needs another endpoint, 500s on both --
                # and the whole point of keeping it as a note rather than a
                # filter is that somebody reads it while choosing. Dropped
                # here, it reached the page nowhere and the picker showed
                # exactly what the old filter had hidden, minus the reason.
                {"id": m["id"], "provider": m.get("provider", ""), "cost": cost,
                 **({"note": m["note"]} if m.get("note") else {}),
                 **advise(m, spec, volume or ASSUMED_VOLUME, cheapest)}
                for cost, m in priced
            ],
        }
    return out


def refresh(path: Path, endpoints: dict | None = None,
            recheck: bool = False, state_dir: str = "") -> dict:
    """Fetch every configured roster and cache them. Returns the new catalogue.

    `endpoints` is {source: {"url", "key", ...}} for the sources that are
    switched on -- see admin/app.py's model_endpoints(). A source that is off
    is simply absent, and one that is on and fails keeps whatever the cache had
    for it: half a catalogue is more useful than none, and the page says which
    half is old and why.

    `recheck` throws away what is remembered about which together.ai models a
    serverless key can call, so the next fetch asks all of them again. Off by
    default because that is ~150 requests and half a minute; on when somebody
    presses Refresh, which is the one moment they are asking for exactly that
    and are watching a spinner they started.
    """
    endpoints = endpoints or {}
    previous = load_cache(path)

    zen = endpoints.get("opencode_zen") or {}
    zen_verdicts = {} if recheck else dict(previous.get("opencode_zen_routable") or {})
    # One document, two rosters. Fetched here so the page pays for models.dev
    # once per refresh instead of once per OpenCode plan.
    try:
        _dev = _get(MODELS_DEV)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        _dev = None          # both fetchers then fetch and report it themselves
    fetched: dict[str, tuple[list, str]] = {
        "opencode_zen": fetch_opencode_zen(zen.get("key", ""), zen_verdicts, _dev),
        # The flat plan, for the Code picker only. Fetched unconditionally
        # rather than behind a key, because it takes no key: models.dev is a
        # public catalogue and the Go endpoint itself is never called.
        GO_CACHE_KEY: fetch_opencode_go(_dev)}
    # Which cloud models this key could actually call, last time anyone asked.
    # Carried across refreshes like the Zen and together caches, for the same
    # reason: the answer does not go stale the way a price does.
    cloud_usable = {} if recheck else dict(previous.get("ollama_cloud_usable") or {})
    # Every local instance, as one roster. They are the same kind of server,
    # usually sharing one model store, and which instance a role runs on is a
    # separate choice on the page ("runs on") -- so a model pulled once is
    # offered once, under `ollama:`, with the instances that have it. The
    # vision roster is kept, empty, for the pages and caches that name it.
    local_rows: dict[str, dict] = {}
    local_errs = []
    for source, ep in endpoints.items():
        if not is_local_ollama(source) or not (ep and ep.get("url")):
            continue
        rows, err = fetch_ollama(ep["url"], ep.get("key", ""), provider="ollama")
        if err:
            local_errs.append(err)
        for row in rows:
            merged = local_rows.setdefault(row["id"], {**row, "instances": []})
            merged["instances"].append(ep.get("instance") or source)
    fetched["ollama"] = (sorted(local_rows.values(), key=lambda r: r["id"]),
                         "; ".join(local_errs) if not local_rows else "")
    fetched["ollama_vision"] = ([], "")
    for source in ("ollama_cloud",):
        ep = endpoints.get(source)
        if not (ep and ep.get("url")):
            fetched[source] = ([], "")
            continue
        rows, err = fetch_ollama(ep["url"], ep.get("key", ""), provider=source)
        # Only the cloud roster is a shop window. A model on a local Ollama is
        # pulled, and therefore callable, so probing it would cost a GPU load
        # to learn nothing.
        if source == "ollama_cloud" and rows:
            rows = _ollama_cloud_drop_paywalled(
                rows, ep["url"], ep.get("key", ""), cloud_usable)
        fetched[source] = (rows, err)
    # Two self-hosted slots, same fetch. FreeToken speaks the same API and is
    # listed separately only so a household can run both at once and tell them
    # apart in the picker.
    for source in ("openai_compatible", "freetoken"):
        ep = endpoints.get(source)
        fetched[source] = (
            fetch_openai_compatible(ep["url"], ep.get("key", ""),
                                    ep.get("label", ""), provider=source)
            if ep and ep.get("url") else ([], ""))

    # The three keyed providers. Absent from `endpoints` means no key, which
    # means not on offer -- there is nothing to configure beyond the key, so
    # the key is the switch.
    # What a serverless key could and could not call, last time anyone asked.
    # Carried across refreshes so only models new since then are probed.
    serverless = {} if recheck else dict(previous.get("together_serverless") or {})
    for source, fetch in (("openrouter", fetch_openrouter),
                          ("together", fetch_together),
                          ("openai", fetch_openai)):
        ep = endpoints.get(source)
        if not (ep and ep.get("key")):
            fetched[source] = ([], "")
            continue
        fetched[source] = (fetch(ep["key"], serverless) if source == "together"
                           else fetch(ep["key"]))

    catalogue = {"checked_at": int(time.time())}
    for source in SOURCES:
        models, err = fetched.get(source, ([], ""))
        # A source that is configured off keeps nothing: leaving its last
        # roster in the cache would go on offering models from a provider the
        # household has switched off, which is a choice that then fails at the
        # first turn.
        configured = (source == "opencode_zen" or source in endpoints
                      or (source == "ollama" and any(is_local_ollama(k) for k in endpoints)))
        catalogue[source] = (models or previous.get(source, [])) if configured else []
        catalogue[f"{source}_error"] = err
    # Stored outside the SOURCES loop, because it is deliberately not a source:
    # that tuple is what every role picker reads. Kept here means the Code card
    # has a roster; kept out of SOURCES means no role can offer one.
    _go, _go_err = fetched.get(GO_CACHE_KEY, ([], ""))
    catalogue[GO_CACHE_KEY] = _go or previous.get(GO_CACHE_KEY, [])
    catalogue[f"{GO_CACHE_KEY}_error"] = _go_err
    catalogue["opencode_zen_at"] = (catalogue["checked_at"]
                                    if fetched["opencode_zen"][0]
                                    else previous.get("opencode_zen_at"))
    # Kept under its old name as well. `ollama_error` is read by the page and
    # by test_models.py, and renaming it would have been a silent "no error".
    catalogue["ollama_error"] = catalogue.get("ollama_error", "")
    # Priced last, because it reads the roster this run just wrote.
    catalogue["priced_roles"] = price_roles(catalogue, state_dir or "")
    catalogue["priced_at"] = catalogue["checked_at"]
    # Both verdict caches are kept even when their source is switched off:
    # switching it back on should not cost the half-minute again, and a verdict
    # about whether a key can route a model does not go stale the way a price
    # does.
    catalogue["opencode_zen_routable"] = zen_verdicts or (
        previous.get("opencode_zen_routable") or {})
    catalogue["together_serverless"] = serverless or (
        previous.get("together_serverless") or {})
    catalogue["ollama_cloud_usable"] = cloud_usable or (
        previous.get("ollama_cloud_usable") or {})
    save_cache(path, catalogue)
    return catalogue


def all_models(catalogue: dict) -> list[dict]:
    out = []
    for source in SOURCES:
        out += list(catalogue.get(source) or [])
    return out


# --------------------------------------------------------------------------
# The recommendation
# --------------------------------------------------------------------------

def meets(model: dict, requires: dict) -> bool:
    """Can this model do the job at all.

    A hard filter, not a preference. A model without image input does not
    answer the camera questions slowly — it answers them with an error, and a
    model whose output ceiling is below what a guide needs truncates it
    silently, which is worse.
    """
    if requires.get("vision") and not model.get("vision"):
        return False
    for field in ("context", "max_output"):
        want = requires.get(field)
        if want is None:
            continue
        have = model.get(field)
        # An unknown limit is not a failure: Ollama does not report one, and
        # excluding every local model on a missing field would be a filter
        # about the catalogue rather than about the model.
        if have is not None and have < want:
            return False
    return True


def _cheapest(models: list[dict], prefer: str) -> list[dict]:
    """Order by the price that actually dominates this persona's bill.

    `cache_read` for anything that re-sends a conversation, `output` for
    anything that writes at length, `input` otherwise. Ties break on the other
    two, so the order is total and stable.
    """
    def key(m):
        def num(v):
            return v if v is not None else float("inf")
        primary = num(m.get(prefer))
        return (primary, num(m.get("input")), num(m.get("output")), m["id"])
    return sorted(models, key=key)


def _recommended(models: list[dict], spec: dict, prefer: str) -> dict | None:
    """The one to pick when price is not the only question.

    `_cheapest` answers "what is the least this can cost", which is the right
    default and the wrong only answer: the cheapest model that clears a role's
    limits is often the one that clears them by the least. This answers "what
    would you actually put here", from the fields the catalogue carries -- and
    only from those. There is no quality score in this data and inventing one
    from price would be a claim this page cannot support.

    What it uses, in order, and why each is a real signal rather than a proxy:

    * **`reasoning`, for a role whose `model_type` is `reasoner`.** A declared
      capability, not a guess. Code review and diagnosis are the roles that
      say so.
    * **`max_output`, for a `long_output` role.** A guide or a mark scheme that
      is cut off at the cap is not a cheaper answer, it is a wrong one.
    * **`release_date`.** Newer within a provider is the closest thing to
      "better" this data has, and it is honest about being a heuristic.
    * **price**, last, to break ties.
    """
    if not models:
        return None
    kind = spec.get("model_type", "")

    def rank(m):
        return (
            # Negated: sorted() is ascending and these are all "more is better".
            -(1 if kind == "reasoner" and m.get("reasoning") else 0),
            -(m.get("max_output") or 0) if kind == "long_output" else 0,
            -_release_sort_key(m),
            # Same price as `_cheapest` ranks on, as the tie-break only.
            m.get(prefer) if m.get(prefer) is not None else float("inf"),
        )
    return sorted(models, key=rank)[0]


def _release_sort_key(m: dict) -> float:
    """`release_date` as a sortable number, 0 when it is missing or unparsable.

    Missing sorts oldest rather than newest: a model that does not say when it
    landed should not win a tie-break on a date nobody published.
    """
    raw = str(m.get("release_date") or "")
    try:
        return float(raw.replace("-", ""))
    except ValueError:
        return 0.0


def recommend(persona: str, catalogue: dict, limit: int = 3) -> dict:
    """What to run this persona on.

    `{"hosted": [...], "local": [...], "requires": {...}, "measured": str}`.

    Two lists, never one ranking: a local model is free and a hosted one is
    not, so a single ordering that weighs price puts a 3B model on your own box
    at the top of every list — including code review, where the honest answer
    is that it cannot do the job. Free is not a capability.

    Within a list the order is cheapest-that-qualifies, which is the only
    ordering this data supports. `measured` carries what the house tested
    itself, and that is the part worth reading.
    """
    spec = PERSONA_NEEDS.get(persona)
    if not spec:
        return {"hosted": [], "local": [], "requires": {}, "measured": ""}
    requires, prefer = spec.get("requires", {}), spec.get("prefer", "input")

    def usable(source):
        return [m for m in (catalogue.get(source) or [])
                if m.get("kind", "") in CHAT_KINDS and meets(m, requires)]

    hosted = [m for source in HOSTED_SOURCES for m in usable(source)]
    local = [m for source in LOCAL_SOURCES for m in usable(source)]

    # The same models again, grouped by where they come from, best two each.
    #
    # The flat lists above rank on price across every provider at once, which
    # answers "what is cheapest" and hides "what does each of these offer".
    # With one provider configured those were the same question. With four --
    # this house has OpenCode Zen, together.ai and two Ollamas -- the cheapest
    # three can all come from one, and a household reading the page never
    # learns that the provider they are already paying for has a model that
    # fits.
    #
    # Two per provider and they answer different questions: the cheapest that
    # clears the role's limits, and the one worth paying for. The second is not
    # "the next cheapest" -- that is the same question again, a little louder.
    #
    # Paid providers only. A local model is free at the point of use, so
    # "cheapest" is not a question about it, and the flat `local` list already
    # says what your own hardware can do.
    # Every source this role can use, with the role's own placement first.
    #
    # Walking only the paid ones was wrong, and wrong exactly where it matters:
    # `events` and `vision` are `placement: local, prefer_local: True` -- this
    # house runs them on its own hardware on purpose -- and a per-provider block
    # that skipped local showed those two roles nothing but providers that
    # charge. A page that steers a role away from the placement its own spec
    # declares is worse than one that says less.
    #
    # `prefer_local` decides the order rather than the membership: a hosted
    # model is still a legitimate choice for a local-preferring role, it is
    # simply not the one to read first.
    order = ((*LOCAL_SOURCES, *HOSTED_SOURCES) if spec.get("prefer_local")
             else (*HOSTED_SOURCES, *LOCAL_SOURCES))
    by_provider = {}
    for source in order:
        fits = _cheapest(usable(source), prefer)
        if not fits:
            continue
        picks, seen = [], set()
        best = _recommended(fits, spec, prefer)
        if source in LOCAL_SOURCES:
            # One pick. `_cheapest` orders these by a price they do not have,
            # so "cheapest" and "recommended" would be two labels on an
            # arbitrary pair.
            fits = [best] if best else []
        # Cheapest first, then the recommendation -- and only when they differ.
        # Showing one model twice under two labels reads as a page that does
        # not know its own mind.
        # "cheapest" is a claim about price, so a local provider's single pick
        # does not get that label -- your own electricity is not a price this
        # page can compare, and saying "cheapest" beside a model that costs
        # nothing to run reads as a bargain rather than as a placement.
        labelled = ([("recommended", best)] if source in LOCAL_SOURCES
                    else [("cheapest", fits[0]), ("recommended", best)])
        for label, m in labelled:
            if m is None or m["id"] in seen:
                continue
            seen.add(m["id"])
            picks.append({**m, "pick": label,
                          "reason": _reason(m, requires, prefer)})
        # Whether this provider publishes anything a recommendation could be
        # made on. together.ai and Ollama Cloud carry neither `reasoning` nor
        # `release_date` -- for those, price is genuinely the only ordering
        # this data supports, and the page says so instead of dressing the
        # second-cheapest up as a recommendation.
        by_provider[source] = {
            "picks": picks,
            "ranked": any(m.get("reasoning") or m.get("release_date")
                          for m in fits),
            # Hardware you already own is free at the point of use, so
            # "cheapest" is not a question about it and the page should not
            # print a price comparison as though it were.
            "local": source in LOCAL_SOURCES,
        }

    return {
        "hosted": [{**m, "reason": _reason(m, requires, prefer)}
                   for m in _cheapest(hosted, prefer)[:limit]],
        "local": [{**m, "reason": _reason(m, requires, prefer)}
                  for m in _cheapest(local, prefer)[:limit]],
        # `{source: [best, second]}`, for the page to show what each provider
        # would put here rather than only the cheapest across all of them.
        "by_provider": by_provider,
        "requires": requires,
        "prefer": prefer,
        "measured": spec.get("measured", ""),
        "prefer_local": bool(spec.get("prefer_local")),
        # Where this persona belongs, as an i18n key suffix. Separate from
        # `prefer_local`, which decides the *ranking*: this is the sentence
        # that says why, and three of the thirteen say "local" while the rest
        # explain what a local model cannot do for them.
        "placement": spec.get("placement", ""),
        "group": spec.get("group", "special"),
        # The shape of model this job wants, said out loud beside the picker.
        # `prefer` already ranks on the price that dominates, and `requires`
        # already filters on limits -- but neither is readable as "put a small
        # one here", which is the sentence somebody choosing actually needs.
        "model_type": spec.get("model_type", ""),
        "why": spec.get("why", ""),
        "label": spec.get("label", persona),
    }


def _reason(model: dict, requires: dict, prefer: str) -> list:
    """Why this model, as (i18n key, args) pairs for the page to join.

    Pairs rather than a sentence, and for the same reason `advise()` returns
    them: this module has no translator and must not have one -- it is read by
    the deployer as well as the page -- so the words are chosen where the
    locale is known. Built as English here, this line was the last thing on a
    Spanish page still speaking English.
    """
    bits = []
    if model["provider"] == "ollama":
        bits.append(("reason.local", {}))
    else:
        price = model.get(prefer)
        if price is not None:
            # One key per unit rather than a name interpolated into a shared
            # one: "cached in" is not a noun that survives being dropped into
            # another language's sentence.
            bits.append((f"reason.price_{prefer}", {"price": f"{price:g}"}))
        if prefer != "input" and model.get("input") is not None:
            bits.append(("reason.price_input", {"price": f"{model['input']:g}"}))
    if requires.get("context") and model.get("context"):
        bits.append(("reason.context", {"k": model["context"] // 1000}))
    if requires.get("max_output") and model.get("max_output"):
        bits.append(("reason.output", {"k": model["max_output"] // 1000}))
    if requires.get("vision"):
        bits.append(("reason.vision", {}))
    return bits


# --------------------------------------------------------------------------
# What a role would cost on a given model
# --------------------------------------------------------------------------
# The page ranks models by the price that dominates each role, which answers
# "which is cheapest" and not "what will this actually cost me". Those are
# different questions, and only the second is answerable: HomeCore records one
# row per completed run in `usage.db`, so the house already knows how many
# tokens each *kind* of turn spends in a month.
#
# Read-only, and a missing or unreadable database is simply no estimate rather
# than an error -- this is a decoration on a picker, and a settings page that
# will not open because a stats file moved is a worse outcome than one that
# cannot price a dropdown.

# Which role serves which recorded scope. The scopes come from HomeCore's
# `_alfred_notify(scope=…)` calls and the session keys nanobot writes; the
# roles are the ones in PERSONA_NEEDS. Kept here rather than derived because
# the routing lives in another service, and stated as a map so a scope that
# moves is one line rather than a silent mis-estimate.
SCOPE_ROLES = {
    "ev-notif": "notifications",   # notification triage
    "ev-task": "events",           # chores -- EVENT_PROFILE since 2026-09-02
    "ev-geo": "events",            # location -- same role
    "sub": "subagent",             # spawned background work
    "dev": "programmer",           # the programmer profession
    "fin": "powerful",             # a profession without its own roster entry
    "whatsapp": "everyday",        # relayed messages run as ordinary chat
    "": "everyday",                # a conversation id: ordinary chat
}

_VOLUME_DAYS = 30                  # a month, and the window the page quotes

# What to price a role by when nothing has been recorded for it -- a fresh
# install, or a role nobody has used yet. A round million input tokens a month,
# and the two splits are the honest ones rather than flattering ones:
#
#   * none of it cached. A cache hit rate is a property of how a role is
#     actually used and cannot be guessed, so assuming zero prices the role at
#     what it costs before any discount rather than after one it may not get.
#   * output at 1% of input, which is what this house measures across every
#     role it has data for (0.68M output against 102M input).
#
# Marked as an assumption on the page, because a number nobody measured must
# not read like one that was.
ASSUMED_TOKENS = 1_000_000
ASSUMED_VOLUME = (float(ASSUMED_TOKENS), 0.0, ASSUMED_TOKENS * 0.01)


def role_volumes(state_dir: str, days: int = _VOLUME_DAYS) -> dict:
    """`{role: (uncached, cached, output)}` tokens over the last *days*.

    Scaled to a 30-day month so the number on the page is the number on a
    bill. A role nothing has been recorded for is absent rather than zero:
    "no estimate" and "free" are different claims and the page must not make
    the second one by accident.
    """
    path = Path(state_dir) / "home-core" / "data" / "usage.db"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:                       # noqa: BLE001 - see the note above
        return {}
    try:
        cutoff = int(time.time()) - days * 86400
        rows = conn.execute(
            "SELECT scope, SUM(prompt_tokens), SUM(cached_tokens), "
            "SUM(completion_tokens), COUNT(DISTINCT day) FROM token_usage "
            "WHERE ts >= ? GROUP BY scope", (cutoff,)).fetchall()
    except Exception:                       # noqa: BLE001
        return {}
    finally:
        conn.close()

    out: dict[str, list] = {}
    for scope, prompt, cached, completion, seen_days in rows:
        role = SCOPE_ROLES.get(scope or "")
        if not role or not seen_days:
            continue
        # Per day, then times thirty. Averaging over the days that *have* data
        # rather than over the window: a database three days old would
        # otherwise read as a tenth of the traffic rather than as three days of
        # it, and quote a tenth of the bill.
        scale = 30.0 / seen_days
        bucket = out.setdefault(role, [0.0, 0.0, 0.0])
        bucket[0] += (int(prompt or 0) - int(cached or 0)) * scale
        bucket[1] += int(cached or 0) * scale
        bucket[2] += int(completion or 0) * scale
    return {role: tuple(v) for role, v in out.items()}


def monthly_cost(model: dict, volume: tuple | None) -> float | None:
    """What *model* would cost for that volume, or None if it cannot be said.

    None where any price the volume actually uses is unpublished -- a local
    Ollama, an OpenAI-compatible server, an image endpoint. Charging $0 for
    those would put every one of them at the top of a cost column, which is
    the same mistake `_cheapest` already refuses to make.
    """
    if not volume:
        return None
    uncached, cached, output = volume
    rate_in, rate_out = model.get("input"), model.get("output")
    if rate_in is None or rate_out is None:
        return None
    # A published zero on a hosted provider is not a price, and treating it as
    # one puts it at the top of every cheapest-first list -- the same mistake
    # `_cheapest` already refuses to make about an *absent* price. Together
    # lists MiniMax-M1-40k, Ternary-Bonsai-27B and others at 0; of those,
    # M1-40k answers 400 "The dedicated endpoint … is not running" and
    # Ternary-Bonsai answers 200, so the zero says nothing about whether the
    # model is usable, let alone what a month of it costs.
    #
    # Local sources are the exception and the reason this checks the provider:
    # hardware you already own really is free at the point of use, which is
    # what `advice.local` says instead of a figure.
    if not rate_in and model.get("provider") not in LOCAL_SOURCES:
        return None
    # An unpublished cache price is not free: it is the input price, which is
    # what a provider without a cache discount charges.
    rate_cache = model.get("cache_read")
    if rate_cache is None:
        rate_cache = rate_in
    return (uncached * rate_in + cached * rate_cache + output * rate_out) / 1e6


def matches_type(model: dict, kind: str) -> bool | None:
    """Does this model look like the *kind* the role wants? None when unknown.

    Three values, and the third is the point. `reasoning` is published by
    models.dev for every Zen model and by together.ai for none of them, so a
    two-valued answer would rank a reasoner below a non-reasoner for a role
    that wants one, on evidence that does not exist.

    Deliberately narrow. This reads published capability flags -- does it
    reason, how much can it write, does it take images -- and does not attempt
    quality: no catalogue carries a quality column, and 25 of 26 hosted models
    report `reasoning: true`, which says nothing about which one writes a
    correct code review. What it can do is stop a role that wants a reasoner
    being handed the cheapest model that is documented not to be one, and stop
    a role that must not think being handed one that does.
    """
    reasons = model.get("reasoning")
    if kind in ("reasoner", "careful"):
        return reasons
    if kind == "small":
        # Triage and household events want the opposite: a model that thinks
        # spends its whole budget doing it and answers nothing.
        return None if reasons is None else (not reasons)
    if kind == "long_output":
        ceiling = model.get("max_output")
        return None if ceiling is None else ceiling >= 32_000
    if kind == "multimodal":
        return bool(model.get("vision"))
    # `fast` and `different` have no capability flag behind them -- one is a
    # price question the cost column already answers, the other is about the
    # rest of this household's roster rather than about the model.
    return None


def advise(model: dict, spec: dict, volume: tuple | None,
           cheapest: float | None = None) -> dict:
    """Why this model, and why not, for one role. Facts only.

    There is no quality column in any catalogue and this does not invent one --
    the same reason `recommend()` refuses to rank on anything but price and
    hard limits. Every line below is read off the roster, off the role's own
    requirements, or off what this house measured itself, and a model with
    nothing to say gets an empty list rather than filler.
    """
    pros: list[tuple[str, dict]] = []
    cons: list[tuple[str, dict]] = []
    cost = monthly_cost(model, volume)
    cached_share = None
    if volume and (volume[0] + volume[1]):
        cached_share = round(100 * volume[1] / (volume[0] + volume[1]))

    if cost is not None:
        if cheapest is not None and cost <= cheapest + 1e-9:
            pros.append(("advice.cheapest", {"cost": f"{cost:.2f}"}))
        elif cheapest:
            pros.append(("advice.costs", {"cost": f"{cost:.2f}",
                                          "times": f"{cost / cheapest:.1f}"}))
        else:
            pros.append(("advice.costs_flat", {"cost": f"{cost:.2f}"}))
    elif model.get("provider") in LOCAL_SOURCES:
        pros.append(("advice.local", {}))
    else:
        cons.append(("advice.unpriced", {}))

    # The cache price is the one that decides a re-sending role, and a provider
    # that publishes none charges the full input rate for it.
    if cached_share is not None and cached_share >= 25:
        if model.get("cache_read") is not None:
            pros.append(("advice.caches", {"share": cached_share}))
        elif model.get("input") is not None:
            cons.append(("advice.no_cache", {"share": cached_share}))

    want = (spec.get("requires") or {}).get("context")
    have = model.get("context")
    if want and have:
        headroom = have / want
        if headroom >= 2:
            pros.append(("advice.roomy", {"times": f"{headroom:.0f}",
                                          "context": have // 1000}))
        elif headroom < 1.25:
            cons.append(("advice.tight", {"context": have // 1000}))

    # Whether it is the kind of model this role wants, which is the question
    # the list is now ordered by -- so the row must say which of the three
    # answers it got, including "the catalogue does not say".
    kind = spec.get("model_type", "")
    verdict = matches_type(model, kind)
    if verdict is True and kind in ("reasoner", "careful"):
        pros.append(("advice.reasons", {}))
    elif verdict is False and kind in ("reasoner", "careful"):
        cons.append(("advice.no_reasoning", {}))
    elif verdict is None and kind in ("reasoner", "careful", "small", "long_output"):
        cons.append(("advice.kind_unknown", {}))
    if verdict is True and kind == "small":
        pros.append(("advice.no_thinking", {}))
    elif verdict is False and kind == "small":
        cons.append(("advice.thinks", {}))
    if verdict is True and kind == "long_output":
        pros.append(("advice.writes_long", {"tokens": (model.get("max_output") or 0) // 1000}))
    elif verdict is False and kind == "long_output":
        cons.append(("advice.writes_short", {"tokens": (model.get("max_output") or 0) // 1000}))

    if model.get("vision"):
        pros.append(("advice.vision", {}))
    if model.get("provider") in LOCAL_SOURCES and model.get("context") is None:
        cons.append(("advice.unknown_limits", {}))
    return {"pros": pros, "cons": cons, "cost": cost}


# --------------------------------------------------------------------------
# Does this role's model actually work?
# --------------------------------------------------------------------------
# Written on 2026-09-02, after a day in which three separate faults each broke
# a role while the page beside them looked perfectly healthy:
#
#   * `reasoning_effort: "none"` -- required by qwen3.6-35b-a3b, rejected
#     outright by openai/gpt-oss-20b, so every event turn answered 400.
#   * `content: null` on the assistant message carrying a tool call -- fine on
#     four of this house's six models, a 400 on gpt-oss-20b and unparseable on
#     GLM-5.3. It is the *follow-up* request that fails, so a role only breaks
#     once it uses a tool.
#   * harmony channel markup returned in `content`, which reached the family
#     as "finalJuana salió de casa."
#
# A probe that sends one prompt and checks for a 200 would have passed all
# three. So this one does the whole round trip a real turn does: ask, call a
# tool, hand back the result, and read what comes out.
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "say", "description": "Say something to the household.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    },
}
# Markers that must never survive into a reply. Harmony channel names first,
# because those are the ones the household actually received.
# Harmony leaks, in the three shapes this household has actually been sent.
# They differ by how much of the markup the gateway ate on the way out, and a
# check that knows only one of them passes a model that is broken:
#
#   1. <|channel|>final<|message|>Juana salió de casa.      nothing eaten
#   2. finalJuana salió de casa.                            markers eaten
#   3. analysisWe need to...assistantfinalHecho.           all of it eaten
#
# These mirror `nanobot/utils/helpers.py`. They are duplicated rather than
# imported because this page must not import the assistant -- but they are the
# same rules, and the samples above are in both test suites so a change to one
# that is not made in the other fails somewhere.
_PROBE_MARKERS = ("<|channel|>", "<|message|>", "<|end|>", "<|start|>",
                  "<|return|>", "<think>", "<thought>")
# A channel name opening the reply, run straight into the next word. Real
# prose starting with "Final" has a space after it.
_PROBE_CHANNEL_PREFIX = re.compile(
    r'^(analysis|commentary|final)(?=[A-Z\[<(\x22\x27])')
# ...and the boundary into a final channel *anywhere* in the reply, for the
# shapes that do not open on one: "assistantfinalHecho." on its own, and a
# leak that follows real content. `(?<!\s)` is what keeps "the finalAnswer
# variable" out of it -- a welded marker has no space before it either.
_PROBE_CHANNEL_JOIN = re.compile(
    r'(?<!\s)((?:assistant)?final)(?=[A-Z\[<(\x22\x27])')


def harmony_leak(text: str) -> str:
    """What raw template markup this reply carries, or "" when it is clean.

    The household reads whatever comes back, so this asks the question that
    matters: is there anything in here that a person should never have seen?
    """
    found = [m for m in _PROBE_MARKERS if m in text]
    if found:
        return ", ".join(found)
    opening = _PROBE_CHANNEL_PREFIX.match(text)
    if opening:
        return f"a bare {opening.group(1)!r} channel name"
    joined = _PROBE_CHANNEL_JOIN.search(text)
    if joined:
        return f"a bare {joined.group(1)!r} channel boundary"
    return ""


def _probe_post(base: str, key: str, body: dict, timeout: int = 60) -> tuple[dict | None, str]:
    """POST a chat completion. (payload, "") or (None, why it failed)."""
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 **_opencode_session(base),
                 **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}"), ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads((exc.read() or b"{}").decode(errors="replace"))
            detail = str((payload.get("error") or {}).get("message") or "")[:200]
        except Exception:  # noqa: BLE001 - a gateway may answer with anything
            detail = ""
        return None, f"HTTP {exc.code}{': ' + detail if detail else ''}"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _wants_responses(model: str) -> bool:
    """True for the models OpenCode's gateway serves on `/v1/responses` only.

    The third copy of one rule, and deliberately not shared: the other two live
    in `nanobot/providers/openai_compat_provider.py` (which does the routing)
    and `deploy/models.py` (which sweeps the roster). Those ship as separate
    services with their own images and their own release cadence, so there is no
    module all three can import -- and a helper vendored into one of them is a
    fourth thing to keep in step, not a fewer. Change one, change all three; the
    test names them.
    """
    name = (model or "").lower()
    return any(token in name for token in ("gpt-5", "gpt-6", "o1", "o3", "o4"))


def probe_model(model_id: str, base_url: str, api_key: str,
                effort: str = "", timeout: int = 60) -> dict:
    """Run a real two-step turn against *model_id* and report what happened.

    Every step is a fault this house has actually had. `ok` is false as soon as
    one fails, and `steps` says which -- a probe that only reported pass/fail
    would send somebody back to the logs, which is where this page exists to
    stop them going.
    """
    steps: list[dict] = []
    started = time.time()

    def fail(name: str, detail: str) -> dict:
        # If this house already knows why this model fails, say so here rather
        # than leaving a bare 500. Six Zen models answer only on /v1/responses
        # and this probe posts /chat/completions -- deliberately, because that
        # is what nanobot posts, so the verdict matches what the assistant would
        # actually get. Without the note the page reports "500 from the
        # provider", which reads as an outage and sends somebody to check a
        # service that is fine.
        note = _ZEN_NOTES.get(model_id)
        if note and detail:
            detail = f"{detail} — {note}"
        steps.append({"step": name, "ok": False, "detail": detail})
        return {"ok": False, "model": model_id, "steps": steps,
                "ms": int((time.time() - started) * 1000)}

    if not base_url:
        return fail("endpoint", "no base URL for this model's provider")

    # A model this gateway serves only on /v1/responses cannot be checked by
    # this probe, and saying so is the honest answer. Every step below is built
    # on the chat/completions shape -- `messages`, `tool_calls`, `choices` --
    # and the Responses API shares none of it, so running it anyway posts a
    # request the model was never going to accept and reports a working model
    # as a 500. The assistant reaches these through `_should_use_responses_api`
    # and they answer fine; measured 2026-09-04, gpt-5.6-luna and gpt-5.6-terra
    # both return 200 on /responses while this shape 500s.
    #
    # Not a fail(): a fail is a verdict about the model, and this is a limit of
    # the probe. Reporting it as broken is the lie that costs an afternoon.
    if "opencode.ai" in (base_url or "").lower() and _wants_responses(model_id):
        return {"ok": None, "model": model_id, "ms": 0, "steps": [{
            "step": "endpoint", "ok": None,
            "detail": "this gateway serves this model on /v1/responses, which "
                      "this probe does not speak; the assistant uses that "
                      "endpoint and is unaffected"}]}

    common = {"model": model_id, "tools": [_PROBE_TOOL], "max_tokens": 256}
    if effort:
        common["reasoning_effort"] = effort

    # 1. It answers at all -- and accepts the reasoning effort this role sets.
    first, why = _probe_post(base_url, api_key, dict(
        common, messages=[{"role": "user",
                           "content": "Use the say tool to say: ready"}]), timeout)
    if first is None:
        hint = ""
        if effort and "validation" in why.lower():
            hint = (f" -- this role sends reasoning_effort={effort!r}, which "
                    f"this model may not accept")
        return fail("answers", why + hint)
    choice = ((first.get("choices") or [{}])[0].get("message") or {})
    calls = choice.get("tool_calls") or []
    # Checked on this reply too, not only the last one. A model that leaks
    # markup does it wherever it answers, and the household reads both.
    leak = harmony_leak(choice.get("content") or "")
    if leak:
        return fail("answers", f"the reply carries {leak} -- raw template "
                               f"markup, which is what the household is shown")
    steps.append({"step": "answers", "ok": True,
                  "detail": f"reasoning_effort={effort or 'unset'}"})

    # 2. The follow-up carrying a tool result. This is the shape that broke
    #    every household event turn, and a one-shot probe never reaches it.
    if not calls:
        steps.append({"step": "tool round trip", "ok": True,
                      "detail": "model chose not to call a tool; not a fault"})
        second = first
    else:
        call = calls[0]
        second, why = _probe_post(base_url, api_key, dict(
            common, messages=[
                {"role": "user", "content": "Use the say tool to say: ready"},
                # "" and not None: null here is a 400 on gpt-oss-20b and is
                # not valid JSON at all from GLM-5.3.
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call.get("id") or "call_1",
                 "content": "said"},
            ]), timeout)
        if second is None:
            return fail("tool round trip", why)
        steps.append({"step": "tool round trip", "ok": True,
                      "detail": "the follow-up after a tool result was accepted"})

    # 3. What comes back is something a person can read.
    final_message = ((second.get("choices") or [{}])[0].get("message") or {})
    text = (final_message.get("content") or "").strip()
    leak = harmony_leak(text)
    if leak:
        return fail("clean reply",
                    f"the reply carries {leak} -- raw template markup, which "
                    f"is what the household is shown")
    if not text and not (final_message.get("tool_calls") or []):
        # Neither words nor a tool call. This is the answerless shape: a turn
        # that costs a request, returns nothing, and reads to a family like
        # the assistant ignoring them.
        return fail("clean reply", "answered with neither text nor a tool call")
    steps.append({"step": "clean reply", "ok": True,
                  "detail": (text[:80] or "a tool call, no text -- fine")})
    return {"ok": True, "model": model_id, "steps": steps,
            "ms": int((time.time() - started) * 1000)}


# ---------------------------------------------------------------------------
# Role probes -- testing what a role is actually for
# ---------------------------------------------------------------------------
#
# `probe_model` above asks every role the same thing: call the say tool with
# "ready". That answers "is this model wired up and does it speak the
# chat/completions shape", which is a real question and the reason it exists.
# It is not the question the picker asks. `powerful` is labelled "Harder
# turns" and `notifications` "Notification triage" -- a probe that hands both
# a trivial sentence tells you they are both reachable and nothing about
# whether either can do its job.
#
# So each role gets the shape of turn it actually serves, and the answer is
# scored against criteria that can be checked here rather than judged. No
# second model grades this: a judge costs a call per test, returns a different
# number for the same answer twice, and is one more thing to pin and have
# break. Every criterion below is an assertion about the payload -- the same
# rule the health checks follow, for the same reason.
#
# Open-ended roles are checked structurally (does it answer, in the right
# language, without markup leaking, inside a length a person would read) and
# not for quality. That is the honest limit of a deterministic rubric and it
# is written here so nobody reads a 4/4 as "this model is good at design".

def _last_int(text: str) -> int | None:
    """The last whole number in *text*, which is where an answer lands.

    A reasoning model that was told to reply with only a number often shows
    its arithmetic first, so the first number in the string is usually one of
    the intermediate steps and the last one is the answer.
    """
    found = re.findall(r"-?\d+", (text or "").replace(",", ""))
    return int(found[-1]) if found else None


def _json_payload(text: str):
    """Parse *text* as JSON, tolerating the fence models wrap it in."""
    body = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
    if fence:
        body = fence.group(1)
    try:
        return json.loads(body)
    except ValueError:
        return None


def _c_answered(text: str, message: dict) -> tuple[bool, str]:
    """There is an answer at all.

    First and never dropped, because the failure it catches is the one this
    house keeps meeting: a reasoning model with no `reasoning_effort` spends
    the whole budget thinking and returns empty content with finish_reason
    "length". Nothing errors. Scored as a criterion so it reads as a failure
    on the page rather than as a probe that broke.
    """
    if text:
        return True, f"{len(text)} characters"
    if message.get("tool_calls"):
        return True, "a tool call and no text"
    return False, "empty content -- the model spent its budget reasoning"


def _c_clean(text: str, message: dict) -> tuple[bool, str]:
    """No raw template markup in what the household would be shown."""
    leak = harmony_leak(text or "")
    return (False, f"carries {leak}") if leak else (True, "no markup leaked")


def _c_no_tool(text: str, message: dict) -> tuple[bool, str]:
    """It answered from what it knows instead of reaching for a tool.

    The tool is offered on this probe on purpose. A model that calls it to
    answer a question of plain fact is the one that told this household "let
    me check the weather" for a question SOUL.md says to answer directly.
    """
    calls = message.get("tool_calls") or []
    if not calls:
        return True, "answered directly"
    name = ((calls[0].get("function") or {}).get("name")) or "a tool"
    return False, f"reached for {name} to answer a question of fact"


def _brief(limit: int):
    def check(text: str, message: dict) -> tuple[bool, str]:
        n = len(text or "")
        return (n <= limit), f"{n} characters (limit {limit})"
    check.__name__ = f"_brief_{limit}"
    return check


ROLE_PROBES: dict[str, dict] = {
    # Harder turns. Multi-step and arithmetic, with one right answer, so
    # "did it think" is checked rather than asserted: a model that reaches for
    # the obvious 288 has skipped the second half of the question.
    "powerful": {
        "why": "a multi-step count with one right answer",
        # 4096, not 1024, and the difference is the whole score. A reasoning
        # model needs ~1000 completion tokens to work this through --
        # qwen3.6-35b-a3b was measured at 993 and 1023 on consecutive runs --
        # so a 1024 cap made the verdict a coin flip on the cap rather than a
        # judgement on the model: 3/3 one minute, 1/3 the next, reported as
        # "returns empty content". That is a probe failing a working model,
        # which is the failure this file is most careful about elsewhere.
        #
        # Headroom rather than production parity: this house sends
        # maxTokens 32768, which would let a slow model run for minutes on a
        # page somebody is watching. 4x the measured need is enough to make
        # the cap stop being the thing under test.
        "max_tokens": 4096,
        "messages": [{"role": "user", "content":
                      "Six containers each send one heartbeat every 30 "
                      "minutes, all day. Two of those six are switched off "
                      "for 8 hours each day. How many heartbeats are sent in "
                      "one 24-hour day? Reply with only the number."}],
        "criteria": [
            ("answers at all", _c_answered),
            ("gets it right", lambda t, m: (
                _last_int(t) == 256,
                f"answered {_last_int(t)}, expected 256"
                + (" -- the count before the two switched off"
                   if _last_int(t) == 288 else ""))),
            ("no markup", _c_clean),
        ],
    },
    # Ordinary chat: a question of plain fact, with a tool offered it should
    # not need, and an answer short enough to read in a chat bubble.
    "everyday": {
        "why": "a question of fact, answered directly and briefly",
        "max_tokens": 512,
        "tools": True,
        "messages": [{"role": "user", "content":
                      "How many days does February have in a leap year? "
                      "Answer in one short sentence."}],
        "criteria": [
            ("answers at all", _c_answered),
            ("gets it right", lambda t, m: ("29" in (t or ""),
                                            "says 29" if "29" in (t or "")
                                            else "does not say 29")),
            ("answers directly", _c_no_tool),
            ("stays brief", _brief(200)),
            ("no markup", _c_clean),
        ],
    },
    # Background work: the turn a sub-agent is given is an instruction with a
    # shape to hit, and the caller parses what comes back. A model that
    # narrates around the JSON breaks the caller, not the reader.
    "subagent": {
        "why": "an instruction with a machine-readable shape to hit",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content":
                      'Return a JSON object with exactly the keys "task" and '
                      '"steps". "task" is the string "tidy". "steps" is a list '
                      'of exactly the three strings "a", "b" and "c". Reply '
                      "with only the JSON."}],
        "criteria": [
            ("answers at all", _c_answered),
            ("is parseable JSON", lambda t, m: (
                _json_payload(t) is not None,
                "parsed" if _json_payload(t) is not None
                else "not JSON, even allowing for a code fence")),
            ("hits the shape", lambda t, m: (
                _json_payload(t) == {"task": "tidy",
                                     "steps": ["a", "b", "c"]},
                "exactly the object asked for"
                if _json_payload(t) == {"task": "tidy",
                                        "steps": ["a", "b", "c"]}
                else f"got {json.dumps(_json_payload(t))[:80]}")),
        ],
    },
    # Triage, and the answer two thirds of the time is nothing. A model that
    # cannot say SILENCE cheaply is the wrong one here whatever else it does:
    # this is the highest-volume turn in the house.
    "notifications": {
        "why": "triage that should decide these are not worth a word",
        "max_tokens": 256,
        "messages": [{"role": "user", "content":
                      "Triage these notifications for a household. If none is "
                      "worth interrupting anybody for, reply with exactly the "
                      "single word SILENCE and nothing else.\n"
                      "- System update available for a laptop\n"
                      "- A newsletter arrived\n"
                      "- Weekly backup completed successfully"}],
        "criteria": [
            ("answers at all", _c_answered),
            ("stays silent", lambda t, m: (
                (t or "").strip().upper().strip(".") == "SILENCE",
                "said SILENCE" if (t or "").strip().upper().strip(".") == "SILENCE"
                else f"said {(t or '')[:60]!r} instead")),
            ("stays brief", _brief(40)),
        ],
    },
    # Household events, and the one this house has actually been burned by.
    # `qwen3.5:4b` rendered "left work" as "se ha ido a trabajar" (went TO
    # work) and "ha dejado su trabajo" (quit her job) -- fluent, confident and
    # backwards, with nothing logged. Spanish on purpose: this is the string
    # the family reads, and the failure only exists in the translation.
    "events": {
        "why": "the Spanish phrasing a geofence event is read as",
        "max_tokens": 512,
        "messages": [{"role": "user", "content":
                      "Mora has just LEFT the place tagged `trabajo` (work). "
                      "Write the one-line notification the household sees, in "
                      "Spanish. Reply with only that line."}],
        "criteria": [
            ("answers at all", _c_answered),
            ("says who", lambda t, m: ("mora" in (t or "").lower(),
                                       "names Mora" if "mora" in (t or "").lower()
                                       else "does not name Mora")),
            ("says leaving, not arriving", lambda t, m: (
                bool(re.search(_LEFT_OK, (t or ""), re.I))
                and not re.search(_LEFT_WRONG, (t or ""), re.I),
                _events_detail(t))),
            ("stays brief", _brief(160)),
        ],
    },
}


# Every way Spanish says she left, and every way it says the opposite.
#
# Split out and named because the first version of this checked only `salió`
# and `ha salido`, and marked "Mora acaba de salir del lugar trabajo" -- a
# correct answer, in the commonest phrasing of all -- as a failure. A rubric
# that fails a right answer is worse than no rubric: it sends somebody to
# replace a model that was doing its job.
# `se fue` was in the first version of this rule, lost in the refactor that
# named these constants, and cost qwen3.5:9b a point for "Mora se fue de la
# etiqueta trabajo" -- which is correct. Second time this rule has failed a
# right answer; both times by being narrower than Spanish is. Add phrasings
# here rather than trimming them.
_LEFT_OK = (r"sal(?:i[oó]|ir|ido|e)\b|se (?:ha )?march|abandon[oó]|"
            r"se (?:fue|ha ido) de\b|ya no est[aá] en\b")
# "dejó el trabajo" is deliberately not here: it reads as quitting as readily
# as leaving a place, and an ambiguous pass is not a pass.
_LEFT_WRONG = (r"se ha ido a trabajar|fue a trabajar|va a trabajar|"
               r"ha dejado su trabajo|dej[oó] su trabajo|renunci|"
               r"lleg[oó] a|ha llegado")


def _events_detail(text: str) -> str:
    """Why the geofence line passed or failed, in the words that matter."""
    body = text or ""
    for wrong, meaning in ((r"se ha ido a trabajar", "went TO work"),
                           (r"fue a trabajar", "went TO work"),
                           (r"ha dejado su trabajo", "quit her job"),
                           (r"dej[oó] su trabajo", "quit her job"),
                           (r"renunci", "resigned")):
        if re.search(wrong, body, re.I):
            return f"reads as {meaning!r} -- the opposite of what happened"
    if re.search(_LEFT_OK, body, re.I):
        return "reads as leaving, which is what happened"
    return "does not clearly say she left"


def score_model(role: str, model_id: str, base_url: str, api_key: str,
                effort: str = "", timeout: int = 120) -> dict:
    """Run *role*'s own probe against *model_id* and score what comes back.

    Returns the same `ok`/`steps`/`ms` shape `probe_model` does, so the page
    renders it without a second branch, plus `score`/`max` and the role's
    `why`. A role with no probe of its own is the caller's problem: ask
    `ROLE_PROBES` first.
    """
    spec = ROLE_PROBES[role]
    started = time.time()
    body = {"model": model_id, "max_tokens": spec.get("max_tokens", 512),
            "messages": spec["messages"]}
    if spec.get("tools"):
        body["tools"] = [_PROBE_TOOL]
    if effort:
        body["reasoning_effort"] = effort

    payload, why = _probe_post(base_url, api_key, body, timeout)
    elapsed = time.time() - started
    ms = int(elapsed * 1000)
    if payload is None:
        hint = ""
        if effort and "validation" in why.lower():
            hint = (f" -- this role sends reasoning_effort={effort!r}, which "
                    f"this model may not accept")
        return {"ok": False, "model": model_id, "role": role, "ms": ms,
                "score": 0, "max": len(spec["criteria"]), "why": spec["why"],
                "tokens": None, "tok_s": None,
                "steps": [{"step": "answers", "ok": False,
                           "detail": why + hint}]}

    # Output rate, and what it is NOT: this divides the model's completion
    # tokens by the *whole* round trip -- connection, prompt processing, any
    # queueing behind another request, and the decode. It is what this role
    # would actually feel, which is the number worth showing beside a score.
    # It is not a decode benchmark and must not be compared against one: a
    # FreeToken decode figure of ~29 tok/s and this measuring ~12 on the same
    # model are both right and are measuring different things. A model that
    # spends its budget reasoning before answering scores badly here on
    # purpose -- those tokens are real and the household waits for them.
    usage = payload.get("usage") or {}
    out_tokens = usage.get("completion_tokens")
    try:
        out_tokens = int(out_tokens) if out_tokens is not None else None
    except (TypeError, ValueError):
        out_tokens = None
    # None rather than 0 when the provider does not report usage -- Ollama and
    # FreeToken both do, but a zero would render as a real measurement of a
    # very slow model rather than as an absence.
    tok_s = (round(out_tokens / elapsed, 1)
             if out_tokens and elapsed > 0 else None)

    message = ((payload.get("choices") or [{}])[0].get("message") or {})
    text = (message.get("content") or "").strip()
    steps, score = [], 0
    for name, check in spec["criteria"]:
        try:
            ok, detail = check(text, message)
        except Exception as exc:                              # noqa: BLE001
            ok, detail = False, f"could not be checked: {exc}"
        score += 1 if ok else 0
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:200]})

    return {"ok": score == len(spec["criteria"]), "model": model_id,
            "role": role, "ms": ms, "score": score,
            "max": len(spec["criteria"]), "why": spec["why"],
            "tokens": out_tokens, "tok_s": tok_s,
            "reply": text[:400], "steps": steps}


# ---------------------------------------------------------------------------
# Where a score is kept
# ---------------------------------------------------------------------------
#
# The latest run per (role, model) and no history. What this answers is "is
# the model this role points at good enough for it", which is a question about
# now -- and a model's score only moves when its weights or its endpoint move,
# neither of which happens often enough to plot. Keeping every run would need
# retention rules and somewhere on the page to show a trend, for a line that
# is flat until the day it is not.
#
# Beside the catalogue, so it is state and lands where the catalogue does
# rather than in the deploy directory. See `guard_state_paths` in deploy.py
# for why that distinction is not cosmetic.

SCORES_VERSION = 1


def load_scores(path: Path) -> dict:
    """{role: {model: record}}, or empty when there is nothing yet."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if int(stored.get("version") or 0) != SCORES_VERSION:
        return {}
    return dict(stored.get("scores") or {})


def record_score(path: Path, role: str, model: str, result: dict) -> dict:
    """Store *result* as the standing score for (role, model). Returns all."""
    scores = load_scores(path)
    scores.setdefault(role, {})[model] = {
        "score": result.get("score"), "max": result.get("max"),
        "ok": result.get("ok"), "ms": result.get("ms"),
        "tokens": result.get("tokens"), "tok_s": result.get("tok_s"),
        "at": int(time.time()),
        "steps": [{"step": s.get("step"), "ok": s.get("ok")}
                  for s in (result.get("steps") or [])],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": SCORES_VERSION,
                                    "scores": scores}, indent=1) + "\n",
                        encoding="utf-8")
    except OSError:
        # A score that cannot be written is not a reason to fail the test the
        # person just ran -- they are looking at the answer on the page.
        pass
    return scores
