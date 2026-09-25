# Where the tokens actually go

Measured on this household's own `usage.db`. **Read the second table, not the
first**: the stack changed underneath this file on 2026-09-02, and an average
taken across that date describes a system that no longer exists. This document
has been wrong twice for exactly that reason and both corrections are kept
below, because the mistake is easier to repeat than to spot.

## The change, and what it was worth

Around 2026-09-02–03 the event turns moved from `gpt-5.6-luna` to the
household's own `qwen3.5:4b` and `qwen3.5:9b`, and their prompts were cut to
suit a smaller model. The effect is not subtle:

| scope | before (August) | after (from 2026-09-03) |
|---|---|---|
| `ev-notif` | 462 turns, **31,905,938** prompt tokens, ~65k each | 2 turns, 4,100 — **~2,050 each** |
| `ev-task` | 472 turns, 12,306,700, ~25k each | 209 turns, 424,350 — **~2,030 each** |
| `ev-geo` | 57 turns, 1,373,014 | 63 turns, 129,150 |

**A 12× smaller prompt, on hardware that bills nothing.** One day alone —
2026-08-17, 362 `ev-notif` turns at a 78,631 mean — was 28.5 million prompt
tokens, roughly a third of everything recorded in three weeks.

That was the largest lever this stack had, and it has already been pulled.
Anything below is what is left.

## The current picture — 2026-09-03 to 09-08, 355 turns

| | |
|---|---|
| turns on **local** `qwen3.5` | **275 of 355 (77%)** — 559,650 prompt tokens, **5% of the total, billed at nothing** |
| turns billed | **80** — 11,388,137 prompt tokens |
| of the billed prompt, on tool turns | **10,556,633 (93%)**, only **14% cached** |
| tool-free billed turns | 24, median 29,910 |

By scope, share of all prompt tokens:

| scope | turns | prompt | share |
|---|---|---|---|
| `dsg` (the Designer) | 17 | **6,491,955** | **54%** |
| interactive chat | 47 | 3,866,541 | 32% |
| `sub` (delegations) | 17 | 1,031,691 | 9% |
| `ev-task` / `ev-geo` / `ev-notif` | 274 | 557,600 | 5% — **local, free** |

**Seventeen Designer turns are more than half the bill.** They are image work
— `describe_image`, `read_file`, `write_file`, `exec`, `skill:file-share` — at
300k–900k prompt tokens each, and on 2026-09-06 every one of them recorded
**0% cached** while the same space on MiniMax three days earlier cached up to
85%.

Treat that 0% as unconfirmed. The usage recorder did not know Anthropic's name
for a cache hit (`cache_read_input_tokens`) until 2026-09-08, and a name it
cannot read looks exactly like a model that does not cache. It reads all three
names now, so the next Designer turn settles it — and whether those 4.5 million
tokens were really paid at full price depends on the answer.

## What a compression layer is worth now

[Headroom](https://github.com/headroomlabs-ai/headroom) — Apache-2.0, measured
rather than claimed figures, available as a library, a drop-in proxy, a CLI
wrapper or an MCP server. It compresses tool output, JSON, code and files, and
its own documentation says prose compresses poorly.

Against the **current** regime, not the three-week average:

| | |
|---|---|
| billed prompt above the tool-free baseline, on tool turns | **8,881,673 — 78% of the billed prompt** |
| of that, not already cached | **7,610,322 — 67%** |
| a compressor taking 30–60% of it | **20%–40% of the billed bill** |

**That reverses the earlier verdict in this file, twice.** The first version
said "not worth adding" and put the saving near zero. The second measured it
at 4–7%. Both were computed over a window in which cheap, prose-heavy,
enormous-volume event traffic dominated the totals — and that traffic is now
local and free. What is left billed is almost entirely tool-heavy and barely
cached, which is precisely the workload Headroom is built for.

**So it is now worth trying**, with three caveats that are not small:

- **The sample is 80 billed turns over six days**, and 17 of them are one
  space. This is "the Designer is the bill" more than it is a stable profile.
- **The 14% cache figure may be a reporting artefact** — see above. If those
  turns are really caching, the uncached part shrinks and so does the prize.
- A compressor sits in the path of every call, decides what to drop, and can
  drop the row somebody asked about. `docs/provider-routing.md` has the other
  standing question about anything in that position: whether
  `x-opencode-session` survives the hop.

**The trial that costs nothing to run:** point the Designer alone at it through
the `openai-compatible` provider slot, leave every other role where it is, and
compare this table before and after. One space, one config change, and the
space that is 54% of the bill.

## What this file got wrong, and why

Twice, the same way: an average over a window in which the system changed.
"`ev-notif` spends 3,000 prompt tokens per output token" was true of August and
false by the time it was written down. Any future number here should name its
window and check whether a scope's model changed inside it — `SELECT scope,
model, day` is the query that would have caught both.

# 2026-09-21: the cache is on, and the header was never the cause

The section below this one is kept, as the file's own rule says, because it
was acted on: it named `x-opencode-session` as the suspect and proposed a
day-long test. Measured directly on 2026-09-21 instead, the hypothesis is
wrong on every axis it stated.

**Direct to Zen, same 1.5k-token prefix, two calls each:** `cached_tokens`
was 1280 of 1475 on the second call **with no header, with the same session
id on both, and with a different id on each** -- the header changes nothing.
Streamed responses carry the same `prompt_tokens_details.cached_tokens` in
their final chunk, which nanobot requests (`stream_options.include_usage`)
and normalises. A 30-tool `tools` array caches too (5888 of 6013); only
*reordering* it breaks the prefix, and `ToolRegistry.get_definitions` walks an
insertion-ordered dict, so nanobot's order is stable.

**Through nanobot, the real prompt (86,348 system chars + 33,623 tool chars,
~31.5k tokens):** the second turn of a session cached **31,488 of 31,706** on
`deepseek-v4-flash`, and the *first turn of a brand-new session* cached
31,488 of 31,695 -- the system+tools prefix is identical across every chat of
a member, so a fresh conversation inherits the cache a previous one wrote.
`deepseek-v4-pro` cached 23,674 of 31,560 on its second turn.

**From `usage.db`, the seven days to 2026-09-21, billed models only:**

| model | turns | prompt | cached | hit |
|---|---|---|---|---|
| `deepseek-v4-flash` | 74 | 8,305,467 | 6,262,784 | **75%** |
| `deepseek-v4-pro` | 4 | 126,219 | 23,674 | 19% (3 of 4 were cold probes) |

By day it has been 32-63% since the 12th. The three models at 0% in the
table below -- `gpt-6-astra`, `gpt-5.6-luna`, `gpt-5.6-terra` -- are the
OpenAI family through Zen, and on 2026-09-21 all three answer *Upstream
request failed: Endpoint is unavailable*; nothing in the house names them any
more. Whether their zeros were a Zen limitation or a reporting gap is now
unanswerable and no longer matters.

**Where the remaining misses are.** Hit rate against the gap since the
previous turn on the same model: 80% under a minute, 75-87% up to 15 min,
69% at 15-30 min, then **33-34% from 30 min to 4 h**. The cache goes cold
after roughly half an hour to an hour of quiet, and the first turn after that
pays full input price on ~31k tokens: $0.0044 on flash, $0.055 on pro. At 11
cold turns a week that is five cents. A keep-warm ping every 20 minutes would
cost more than it saves.

**What this means for the bill.** Zen prices a cached read at $0.028 against
$0.14 input on flash (5x), $0.006 against $0.30 on v4.1-flash (50x) and
$0.145 against $1.74 on pro (12x). The week above cost about **$0.47 in
prompt tokens** on flash -- 2.04M uncached at $0.14 plus 6.26M cached at
$0.028. At 78 billed turns a week the prompt cache is not where the money is;
the Designer image turns named earlier in this file still are.

# (superseded above) The cache is off on Zen, and that is the whole optimisation

Measured 2026-09-09. Two questions were asked together -- plan the token
optimisation, and check the cache works on every provider -- and the second
answered the first.

## Cache hit rate by model, since 2026-09-03

| model | turns | prompt | cached | hit |
|---|---|---|---|---|
| `gpt-6-astra` | 10 | 4,787,542 | 0 | **0%** |
| `gpt-5.6-luna` | 39 | 3,691,261 | 0 | **0%** |
| `gpt-5.6-terra` | 5 | 605,508 | 0 | **0%** |
| `MiniMaxAI/MiniMax-M3` | 3 | 1,215,572 | 965,504 | 79% |
| `Qwen/Qwen3.8-Flash` | 16 | 505,445 | 452,480 | 90% |
| `deepseek-ai/DeepSeek-V4-Flash` | 9 | 755,980 | 457,030 | 60% |
| `deepseek-v4-flash` | 3 | 111,019 | 27,904 | 25% |
| `qwen3.5:4b` / `:9b` | 291 | 592,450 | 0 | n/a — local |

**9.1 million of the 11.9 million billed prompt tokens in that window are on
the three models at zero.** Everything not on Zen caches between 60% and 90%.

## It used to work, and stopped on a date

`gpt-5.6-luna`, by day:

| | turns | prompt | cached | hit |
|---|---|---|---|---|
| 2026-08-16 → 08-24 | 1,090 | 56,297,000 | 25,507,511 | **36–68%** |
| 2026-09-04 → 09-08 | 39 | 3,691,261 | **0** | **0%** |

Nothing in between: luna was idle from 25 August while the events moved to
local models.

## The suspect

`x-opencode-session` was added to every request to `opencode.ai` on 2026-09-03
(`b77dd67`). Caching is zero from 09-04.

The correlation is exact along both axes: **every model that gets the header
is at 0%, every model that does not is at 60–90%**, and the one model with
history on both sides of the change cached 36–68% before it and 0% after.

The mechanism is a hypothesis, not a measurement. The id is
`uuid.uuid4().hex`, fixed for the life of a provider instance -- which the
code's own comment calls "coarser than the one stable ID per conversation
OpenCode asks for". If Zen scopes its prompt cache to that id, then a value
that is new per instance is a new, cold cache namespace every time a container
restarts or an instance is rebuilt, and an implicit prefix cache may be
disabled outright once an explicit session is supplied.

**The test that settles it**, in order:

1. Send a *stable* id -- the same value across restarts, derived from the
   assistant instance -- and watch a day of `cached_tokens`.
2. If that does not recover it, send the conversation's own key, which is what
   OpenCode actually asks for and what `chat_titles.session_id` already holds.
3. If neither, drop the header for one assistant and compare. It cannot simply
   be removed -- OpenCode says "requests may error" without it -- but one
   instance for one day is a measurement, not a policy.

Whatever the answer, **it is worth more than every other optimisation on this
page put together**, and it is a header value rather than an architecture.

## What a turn is actually made of

Measured on the same window, so the plan below is proportionate.

| kind | prompt tokens | |
|---|---|---|
| a notification or event | **2,050**, flat -- min = median = max | local model, billed at nothing |
| a chat "hello" (the floor) | **24,598** | |
| a median chat turn (`luna`) | **109,700** | |
| a median Designer turn | **471,979** | |

The floor decomposes as:

| | tokens |
|---|---|
| skill catalogue — 47 front matters | 5,486 |
| `SOUL.md` | 4,566 |
| `AGENTS.md` | 3,262 |
| `USER.md` | 629 |
| `FAMILY.md` | 558 |
| `TOOLS.md` | 396 |
| *files subtotal* | *14,897* |
| everything else — tool schemas, framework, date/context | **9,701** |
| **observed floor** | **24,598** |

## The plan, in the order the numbers put it

1. **Fix the Zen cache.** ~9.1M tokens a window at full price, on traffic that
   was 36–68% discounted three weeks ago. One header value.
2. **History.** A median luna turn is 109,700 against a 24,598 floor: **85,000
   tokens of conversation**, 4.5× everything else combined. Windowing or
   summarising it is the only lever of that size, and it is also the one that
   interacts with (1) -- a stable prefix is what a cache rewards.
3. **The skill catalogue, 5,486 tokens.** Bigger than `SOUL.md`, and it is a
   menu rather than knowledge. `CLAUDE.md` already records that an env-unmet
   skill ships its name and full description into every prompt, marked
   unavailable. Counting how many of the 47 cannot run here is an afternoon.
4. **The 9,701 unaccounted tokens.** Larger than `SOUL.md` and `AGENTS.md`
   together and not yet explained -- five configured tools should not cost
   that. Worth reading before accepting.
5. **Not `USER.md` and `FAMILY.md`.** Together 1,187 tokens: 4.8% of a first
   hello, **1.1%** of a median turn, 0.25% of a Designer turn. Moving them to
   a skill or a retrieval step also moves them out of the cacheable prefix, so
   the saving is small and probably negative. This was the question that
   started the exercise and the measurement is the answer to it.

# The plan: saving tokens without breaking anything

Written 2026-09-09, after the cache finding above was corrected. Ordered by
value divided by risk, and every phase has a gate that has to pass before the
next one starts and a rollback that is a config revert. Nothing here is a
proxy in the request path and nothing moves the household's data out of the
cached prefix -- both were evaluated and both cost more than they save.

**The rule that orders everything:** a prompt cache is paid for the *uncached*
tokens. Once the cache is warm, the standing prefix is the cheap part of a turn
and the conversation history is the expensive part. So the prefix is not the
first thing to shrink -- it is the thing to keep *stable*, because every change
to it is a cold cache for every conversation on the next deploy.

## Phase 0 — measure before touching anything

*Risk: none. Cost: one deploy and a day.*

1. **Deploy nanobot** with the two fixes already committed (`4a85c2e`): the
   Responses parser reads `input_tokens_details.cached_tokens`, and every
   Responses request carries `prompt_cache_key`.
2. **Watch a day of `cached_tokens` for luna, astra and terra.** The gate is a
   number, not a feeling: if they land at 60–90% like MiniMax and DeepSeek,
   the reporting hole was the whole story and 9.1M tokens were already
   discounted. If they stay at 0%, send a session id that survives restarts
   (one line, `_session_id` from a file rather than `uuid4()`) and watch
   another day. Only if *that* fails is the header worth suspecting.
3. **Account for the 9,701 tokens** in the chat floor that no file explains.
   Capture one real request body -- nanobot's debug secret exists for this --
   and count tool schemas, framework text and injected context. Bigger than
   SOUL and AGENTS together; not something to optimise around without reading.
4. **Count the skills that cannot run here.** 47 ship their description into
   every prompt; `CLAUDE.md` already records that env-unmet ones ship marked
   unavailable. The number decides whether Phase 2 is worth a cache reset.

*Rollback: none needed -- nothing changes behaviour.*

## Phase 1 — free wins, no behaviour change

*Risk: negligible. Saving: makes every later saving stick.*

- **A session id that survives a restart.** Today it is `uuid4()` per provider
  instance, so every deploy is a new `prompt_cache_key` and a cold cache for
  every conversation. Deriving it from the assistant instance name makes a
  deploy cost nothing at the provider. Verify with the existing header tests
  plus one asserting the value is the same across two constructions.
- **Do not touch the prefix in this phase.** Ordering matters here: a stable
  key first, so that Phase 2's one-time reset is the *only* reset.

*Rollback: revert one function.*

## Phase 2 — the standing prefix, in one deploy

*Risk: low if batched, moderate if dribbled. Saving: up to ~7k of a 24.6k
floor, but see the rule above for what that is worth once cached.*

Everything in this phase changes the cached prefix, and the docs are explicit
that the prefix must be byte-identical -- model, tools, tool *order*, schemas,
reasoning effort, verbosity. So: **all of it in one deploy**, not one change a
day. Each change is a cold cache for every conversation; ten changes over ten
days is ten of them.

1. **Prune unavailable skills** via `disabledSkills` in the instance config.
   Not by deleting: the config is the documented default and a household with
   the service switched on wants them back. Verify with
   `services/nanobot/tests/` and one real turn that uses a skill still on the
   list. Keep the remaining skills in their existing order.
2. **Tighten `SOUL.md` and `AGENTS.md`**, 7,828 tokens between them. Prose,
   and it tightens -- but it is prose the household has tuned, so this is a
   reviewed diff and not a rewrite. The three things that must survive are
   already pinned by tests: the language token, the morning greeting having
   no English literal, and the house instance naming its language.
3. **Leave `USER.md`, `FAMILY.md` and `TOOLS.md` alone.** 1,583 tokens, all
   in the cached part, and `USER.md` is what memory consolidation writes to.

*Gate:* the chat floor drops (measure it: `MIN(prompt_tokens)` for chat since
the deploy); every suite passes; a chat turn, a notification and the next
morning's greeting all arrive in Spanish. *Rollback:* revert the config and
the two files, one deploy.

## Phase 3 — history, which is where the tokens are

*Risk: moderate, and the one phase that can change what Alfred remembers.
Saving: the largest available -- 85,000 of a 109,700 median turn.*

nanobot consolidates a session at 65,536 tokens (`TURN_TIMEOUT`'s comment in
the manifest is where this house learned it: the kitchen sits around 37k).
Everything below that threshold is re-sent, uncached, every turn.

1. **Lower the consolidation threshold for chat sessions only** -- the events
   already run at 2,050 and do not need it -- and do it in steps: 65,536 to
   32,768 first. It is a config knob and a redeploy, which is also the
   rollback.
2. **Test memory, not just tokens.** Before and after: a scripted conversation
   that mentions a fact in turn 1, talks about something else for twelve
   turns, and asks about the fact in turn 14. That is what consolidation can
   break, and it is the test that would have caught it. Add it to the suite.
3. **Measure the median, not the floor.** The floor does not move in this
   phase; the median should roughly halve.

*Gate:* the memory test passes at the new threshold; median chat prompt drops;
nobody in the house reports Alfred forgetting. *Rollback:* the threshold, one
config line.

## Phase 4 — the Designer, last and separately

*Risk: unknown until measured. Saving: 54% of the current bill lives here.*

Seventeen turns at a 471,979 median. That is not history in the ordinary
sense -- it is `describe_image`, `read_file` and `exec` payloads, hundreds of
kilobytes of tool output per turn. Two levers, in order:

1. **Per-space consolidation**, once Phase 3 has shown it is safe: the
   Designer space can have its own, lower threshold.
2. **Trim tool output at the tool** -- a `read_file` that returns the first
   N lines and says so, a `describe_image` that caps its answer -- rather than
   a compressor in the request path. The tool knows what it is returning; a
   proxy has to guess.

Only after Phases 0–3, because the Designer's 14% cache rate may also be the
reporting hole, in which case its real cost is a fraction of what it looks
like today.

## Not on the list, and why

- **Moving `USER.md` / `FAMILY.md` to a skill or retrieval.** 1,187 tokens,
  1.1% of a median turn, out of the cached prefix and into full-price
  per-turn retrieval. Negative.
- **A compression proxy** (Headroom, or a hand-rolled one). It rewrites the
  request, which resets the prefix cache on every turn by construction, and
  it decides what to drop from the household's own data. Evaluated above;
  the Designer's tool output is the one place it could pay, and Phase 4's
  tool-side trimming gets the same saving without a proxy.
- **Changing models to cheaper ones.** Not a token saving, and the Models
  page already owns that choice.

## What "without breaking anything" means, concretely

Every phase ends with the same four checks, run by hand because two of them
need a real model: the packaging suites green; a chat turn answered in
Spanish; a notification delivered at ~2,050 tokens; and the next morning's
greeting in Spanish with no English literal. Phase 3 adds the memory test.
Anything that fails one of those is rolled back before the next phase starts,
and the rollback for every phase is a config revert and one deploy.

# Phase 0 and 1, done — and two of the plan's premises were wrong

Run 2026-09-09.

## Phase 0 gate: the cache was never off

The two fixes deployed, then two identical 6,776-token requests through the
real provider, per model:

| model | first call | second call |
|---|---|---|
| `gpt-5.6-luna` | 0 cached | **6,773 of 6,776 — 99.96%** |
| `gpt-6-astra` | 0 cached | **6,773 of 6,776** |
| `gpt-5.6-terra` | 0 cached | **6,773 of 6,776** |

So the 0% was the reporting hole, exactly as the corrected diagnosis said, and
those 9.1M prompt tokens were very likely discounted all along. **The header
was never the problem and `x-opencode-session` is exonerated.** What remains
unknown is the historical rate -- the count was dropped, not the discount, and
nothing recorded it.

## Phase 1: the session id survives a restart

`uuid4()` per provider instance, which is also `prompt_cache_key`, so every
deploy of the assistants was a cold prefix cache for every conversation.
Derived from `NANOBOT_INSTANCE` plus endpoint and model now, hashed. Verified
in the deployed container: same id across two constructions, and still caching
at 99.96%.

## Phase 0 step 4 kills its own phase-2 item

Asked the real `SkillsLoader` rather than reading front matter:

| | |
|---|---|
| skills shipped | **43** |
| usable here | **40** |
| cannot run here | **3** — `n8n` (no `NANOBOT_N8N_API_KEY`), `github` (no `gh`), `summarize` (no `summarize`) |
| the catalogue in every prompt | **18,487 chars ~4,621 tokens** |
| of which unavailable | **973 chars ~243 tokens** |

**Disabling every skill that cannot run saves 243 tokens.** The earlier guess
was "a third of 47, so maybe 1,800", and it was wrong twice over: there are 43
not 47, and 40 of them work. The catalogue is expensive because this household
uses a lot of skills, not because it is full of dead entries. Phase 2's first
item is therefore not worth a cache reset on its own -- it goes in only if
Phase 2 happens anyway for the prose.

## Phase 0 step 3: the floor, now fully accounted

| | tokens |
|---|---|
| skills summary (measured, not estimated) | 4,621 |
| `SOUL.md` | 4,566 |
| `AGENTS.md` | 3,262 |
| **tool schemas — 11 definitions, 9,531 chars** | **2,382** |
| `USER.md` | 629 |
| `FAMILY.md` | 558 |
| `TOOLS.md` | 396 |
| *subtotal* | *16,414* |
| still unexplained | ~8,200 |
| observed floor | 24,598 |

The tool schemas are 2,382 tokens across 11 definitions -- `grep` alone is 534.
Real, but a quarter of what was unaccounted, and not obviously trimmable: a
tool description is what stops the model calling it wrongly.

About 8,200 tokens remain unexplained. Candidates not yet measured: the
framework's own system text, the date and context injection, the conversation
seed, and the fact that a 4-chars-per-token estimate is generous for Spanish
prose with accents. **Worth one more measurement before anyone edits a prompt
file on the strength of it.**

## What this does to the plan

- **Phase 2 is now much weaker.** Skill pruning is 243 tokens. What is left is
  `SOUL.md` and `AGENTS.md` at 7,828 -- real, but it is the cached part of the
  prompt, so the saving is the cache-write price once per conversation rather
  than the full price every turn. Do it for clarity, not for money.
- **Phase 3 is now the whole plan.** History is 85,000 of a 109,700-token turn
  and it is the *uncached* part by construction: every turn appends, so every
  turn has a new suffix. That is where the money is, and the cache being
  healthy makes it more true rather than less.
- **Phase 4 unchanged**, and its 14% figure should be re-read now that the
  Responses parser reports properly.
