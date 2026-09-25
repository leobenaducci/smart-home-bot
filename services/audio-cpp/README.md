# audio.cpp — one runtime for hearing and speaking

It was a bench and the measurements are below. It is a **service** now:
`services.audio-cpp.enabled` on the admin page, `./home-stack deploy
audio-cpp`, and **off in the shipped config** — nothing points at it, so
installing it everywhere would be weights downloaded to answer nobody.

**Turning it on changes nothing the family hears.** `TTS_ENGINE` in home-voice
still says `piper` — which is baked into the gateway's own image, is the
default engine, and is also `TTS_FALLBACK_ENGINE`, so it stays underneath
whatever else is chosen. Moving the house onto this is a separate, deliberate
act.

**It ships one TTS model, not the seven that were auditioned.**
`supertonic_3_q8_0` is what the profile picked; the whole field is 6.9 GB and
these two packages are ~1.6 GB. `tts_packages` in the config is how you add
the others back to re-run `test/quality_tts.py`.

**There is no model picker for it on the admin page, on purpose.**
`tts_packages` is the menu the server offers; the caller names one per request
(`{"model": "supertonic_3_q8_0", ...}`). Which voice suits *this* reply is a
per-call decision, and a household-wide setting is the wrong shape for it.

```bash
./home-stack deploy audio-cpp

# the profiles, against a running one
python3 test/bench.py --token <a gateway device token>       # one candidate, both halves
python3 test/profile_tts.py --token <token> --save-dir /tmp/clips  # speed, every TTS model
python3 test/quality_tts.py --token <token> --save-dir /tmp/clips  # and which speaks Spanish best
python3 test/profile_tts.py --token <token> --vram           # with backend: cuda, what it costs the card
```

One model per container start is how the VRAM numbers below were taken — so a
row reports that model's footprint and not the sum of everything the profile
loaded before it. `AUDIOCPP_TTS_PACKAGES=<one-package>` in the environment of a
hand-run `docker compose up -d --force-recreate` is the quickest way to do
that without touching the config.

## The question

The household's voice path is two things: `faster-whisper` small on the CPU for
listening, and Piper `es_ES-davefx-medium` on the CPU for speaking, shelled out
at `gateway.py:345`. [audio.cpp](https://github.com/0xShug0/audio.cpp) is a
C++/ggml runtime for both, so it is the first candidate that can answer the
whole question rather than half of it.

**This supersedes the OmniVoice bench**, and not because OmniVoice was wrong:
audio.cpp *runs* OmniVoice — `OmniVoice-GGUF` is in its model repo. The change
is the runtime. OmniVoice through torch meant CUDA, a multi-gigabyte image, no
CPU path and no streaming. Through this it is one GGUF file among thirty.

## Why CPU, and why that is the whole point

Two things measured on this box in September 2026 settled it:

- **Offload cannot rescue a module that does not fit.** z-image's bf16 weights
  (`docs/local-generation.md`) sat at 10,730 MiB with
  `enable_model_cpu_offload()` on and OOM'd anyway,
  because model-level offload moves whole modules. Quantisation is what fits —
  which is exactly what GGUF is.
- **The card is spoken for.** Ollama alone held **9.2 GB** across two warm
  models, and z-image fell back to the paid API with 1.5 GB left. Piper's real
  advantage over every GPU TTS is that it costs none of that.

So the candidate is `qwen3_tts_0_6b_base_q8_0` and `qwen3_asr_0_6b_q8_0`:
**1.99 GB and 1.15 GB** on disk, both on the CPU, **zero VRAM**. (Measured
with `model_manager_v2.py sizes`. Estimating from the parameter count said
600 MB each and was wrong — a package carries a codec and a tokenizer too.)

If audio.cpp needs the card to win, it has lost — that is a bench result, not
a setback.

**It does win on the card, and "needs" turned out to be the wrong word for
637 MiB.** See "On the card" below: that rule was written expecting a
z-image-shaped answer, where the useful configuration wanted 4,760 MiB and the
card had 1.5 GB left (`docs/local-generation.md`). A model that speaks in 0.07 s inside 637 MiB is not the
thing the rule was aimed at. The rule stands for anything that wants gigabytes;
it does not settle this.

## What is measured

`test/bench.py` makes both stacks do the same job on the same short Spanish
lines and times them:

```
audio.cpp TTS -> WAV -> audio.cpp ASR      the candidate, both halves
piper         -> WAV -> whisper            what the family hears today
```

Every clip also goes through the house's whisper, which is what separates *a
bad voice* from *a bad transcriber* when a round trip misses.

**Short lines on purpose.** The published figures for this class of model are
long-form real-time factors, and a voice assistant's replies are a handful of
words. A model that is 40× real time on a paragraph but slow to start is worse
here than one half as fast that begins immediately: what a person waits for is
the first audio, not the last.

The pass mark is the same one `services/home-voice/test/smoke.py` already
uses — a word from the *middle* of the sentence has to survive the round trip.
A transcriber that catches only the beginning is a failure this would otherwise
score as a pass.

`profile_tts.py` adds the mirror image of that rule, because the profile below
found the mirror image of that failure: the **first** word has to survive too.
The fastest model in the field is fast partly because it does not say the whole
sentence.

## What it measured — 2026-09-07, CPU only

**Split verdict. The TTS is a no. The ASR is the interesting half.**

*Read "Seven TTS models" below before quoting the 30x. That figure is
`qwen3_tts_0_6b_base_q8_0`, which turned out to be the fourth-slowest of the
seven models this runtime will serve. The gap to piper is real and it is
**4.5x**, not 30x.*

Same sentence, same box, warm (first call excluded — `lazy_load` is on):

| | time | audio produced | real-time factor |
|---|---|---|---|
| **piper** (today) | **0.6 s** | 2.2 s | **0.28** |
| audio.cpp `qwen3_tts_0_6b_base_q8_0` | **17–19 s** | 2.0–2.3 s | **~8** |

Roughly **30× slower than piper**, and eight times slower than real time — a
family member would wait eighteen seconds after the assistant already knew
what to say. The server says so itself on startup: *"audio.cpp is optimized
for CUDA. The cpu server backend is intended for portability and testing, but
performance and model coverage may be lower than CUDA."*

That is a no by this bench's own rule: if it needs the card to win, it has
lost, because the card is already three services deep.

Transcribing the same clips, six runs each, warm:

| | median | range | transcript |
|---|---|---|---|
| **audio.cpp** `qwen3_asr_0_6b_q8_0` | **2.18 s** | 1.77–2.58 | exact, accents intact |
| house `faster-whisper` small | **3.61 s** | 3.25–4.02 | exact, accents intact |

**~1.65× faster than whisper on the same CPU, with an identical transcript.**

Measure warm or not at all: whisper's *first* call was 9.6 s and would have
made audio.cpp look 2.8× faster. That is the same mistake the z-image bench
made in the other direction, and it is why every number above is a median of
repeats rather than one sample.

### So the honest answer to "can one runtime replace both"

No — but it might replace the listening half. The ASR is faster than what runs
today, transcribes identically, costs 1.1 GB of weights and no VRAM, and its
spec claims streaming, which whisper here does not do. That is worth its own
follow-up. The speaking half is not close, and Piper stays.

## Seven TTS models — 2026-09-08, CPU only

**Still a no, and for a different reason than yesterday's.** The 17–19 s above
is not what this runtime costs to speak. It is what *that model* costs. Of the
33 TTS families audio.cpp ships, 12 list Spanish; these are the seven whose
packages are small enough for a CPU to have a chance, all on the same box, the
same three sentences, the same 4 threads:

| package | on disk | median warm | audio | RTF | cold | mid | onset |
|---|---|---|---|---|---|---|---|
| **piper** `es_ES-davefx-medium` (today) | — | **0.56 s** | 2.52 s | **0.22** | — | 3/3 | 3/3 |
| `pocket_tts_spanish_q8_0` | 122 MiB | 1.17 s | **1.76 s** | 0.66 | 2.7 s | 3/3 | **2/3** |
| **`supertonic_3_q8_0`** | 433 MiB | **2.50 s** | 2.91 s | **0.86** | 10.0 s | **3/3** | **3/3** |
| `audio8_tts_preview_0_6b_q8_0` | 1.33 GiB | 11.61 s | 2.11 s | 5.50 | 24.2 s | 3/3 | 3/3 |
| `qwen3_tts_0_6b_base_q8_0` | 1.86 GiB | 12.37 s | 1.88 s | 6.58 | 19.5 s | 3/3 | 3/3 |
| `moss_tts_nano_100m_q8_0` | 184 MiB | 30.71 s | 2.48 s | 12.38 | 44.2 s | 3/3 | **2/3** |
| `magpie_tts_q8_0` | 1.45 GiB | 50.79 s | 2.60 s | 19.53 | 60.5 s | 3/3 | 3/3 |
| `omnivoice_q8_0` | 1.26 GiB | 140.14 s | 2.28 s | 61.46 | 154.1 s | 3/3 | 3/3 |

**Package size does not predict speed.** The 184 MiB model is **12× slower**
than the 433 MiB one, and the 1.86 GiB one is 2.5× faster than the 184 MiB
one. End to end the field spans **120×** — 1.17 s to 140 s for the same
sentence — and the order barely correlates with the download size at all.
Architecture does, and the only way to know which is which is to run them.

**The candidate is Supertonic 3.** 2.50 s for a line piper says in 0.56 s —
4.5× off, not 30× — with the transcripts coming back clean, no reference clip
needed, and an RTF under 1 on four CPU threads. It is also the one family here
whose spec lists `streaming` and `built_in_voices`, which matters more than the
median: what a person waits for is the *first* audio, and 2.5 s of latency
before a 2.9 s sentence is a different experience from 2.5 s of silence.

**PocketTTS is the trap, and the onset rule is why it did not win.** It is the
only model faster than Supertonic and it scores 2/3 on the first word. Its
audio is 1.76 s where piper needs 2.52 s for the same sentence — *it is fast
because it is not saying all of it*. "Enciende la luz de la cocina" comes back
from whisper as "de la luz de la cocina": a light command with no verb. On a
repeat run it answered "¿Qué hay para la cena de mañana?" with **"¡Adiós!"**
By speed alone it is second place; it is not a candidate.
(`audiocpp_cli --inspect` on the *Spanish* package reports
`variant=english / languages=english`, which is upstream metadata rather than
the weights — but it is not nothing, on the model that mangled Spanish.)

Two more things a Spanish household should know before reading the table as a
ranking: `magpie_tts` said *"la cena de Mana"* — it drops the `ñ` — and
`omnivoice` truncates the *end* ("sacar la basura del mar"). Both score 3/3 on
both word rules. The rules catch a model that mangles a specific word; they do
not catch a model that mangles a specific *letter*, and there is no substitute
for listening, which is what `--save-dir` is for.

### The check that failed the incumbent

The onset rule scored **piper** 2/3 on its first run, and piper is what the
family already hears. whisper returns "que hay para la cena" for "¿Qué hay…";
the word is there and the acute accent is not, and a literal comparison called
that a dropped word. A check that fails the thing already in production is a
broken check, not a finding — `_norm()` now strips accents and case before
comparing, and the transcripts are printed underneath so the accents can still
be judged by eye. Every score in the table above is that corrected rule applied
to the transcripts the 2026-09-08 run recorded.

### Adding one to the profile

`AUDIOCPP_TTS_PACKAGES` in `docker-compose.yml` is a comma-separated list of
**package ids**, and the server registers every one of them; the model id it
serves under *is* the package id, so a row in the profile names the thing you
would install. The family is read from the spec by `model_manager_v2.py info`
rather than declared next to the package — that used to be a second
environment variable, which is a second place to get it wrong, and
`qwen3_tts_0_6b_base_q8_0` with `AUDIOCPP_TTS_FAMILY=qwen3_asr` is a config
the server accepts and then fails on.

What a model needs in the request body is a property of the model, and
`LADDER` in `profile_tts.py` records what each family actually answered:
PocketTTS and Qwen3 Base refuse to speak without a reference clip, Supertonic
and MOSS take the text alone, Magpie wants a packaged voice name and a
language. An unfamiliar family gets the rungs tried in order and the one that
worked is printed in its row.

### What this cost to run

Seven TTS models plus the ASR registered in one server, walked end to end:
**12.4 GiB resident** at peak and 6.9 GB of weights on disk. That is the
profile's cost, not a deployment's — the house would register one. `lazy_load`
means a registered model costs a path until something calls it, which is what
makes walking eight of them in one process possible at all.

## On the card — 2026-09-08, RTX 3060 12 GB, models under 4 GB

**Six of them beat piper, and the two fastest beat it by eight times.** The CPU
profile above ranked the field and rejected all of it; the same field on the
same box with a CUDA build is a different answer, and the gap is not subtle.

Each row is that model **alone on the card** — one package per container start,
because in a run that registered all seven the loaded models accumulate and the
fourth one OOMs on the leftovers of the first three, which measures the profile
rather than the model:

| package | on disk | **VRAM** | median warm | RTF | cold | mid | onset | CPU median | speed-up |
|---|---|---|---|---|---|---|---|---|---|
| `pocket_tts_spanish_q8_0` | 122 MiB | **441 MiB** | 0.05 s | 0.03 | 1.2 s | 3/3 | **2/3** | 1.17 s | 23× |
| **`supertonic_3_q8_0`** | 433 MiB | **637 MiB** | **0.07 s** | **0.02** | 7.2 s | **3/3** | **3/3** | 2.50 s | **36×** |
| `cosyvoice3_q8_0` | 2.10 GiB | 1,897 MiB | 0.65 s | 0.30 | 1.5 s | 3/3 | 3/3 | — | — |
| `qwen3_tts_1_7b_base_q8_0` | 2.51 GiB | 3,393 MiB | 0.66 s | 0.34 | 9.4 s | 3/3 | 3/3 | — | — |
| `audio8_tts_preview_0_6b_q8_0` | 1.33 GiB | 1,921 MiB | 0.72 s | 0.33 | 5.0 s | 3/3 | 3/3 | 11.61 s | 16× |
| `magpie_tts_q8_0` | 1.45 GiB | 1,263 MiB | 0.85 s | 0.35 | 10.0 s | 3/3 | 3/3 | 50.79 s | 60× |
| `moss_tts_nano_100m_q8_0` | 184 MiB | 527 MiB | 1.14 s | 0.42 | 4.1 s | 3/3 | **2/3** | 30.71 s | 27× |
| `omnivoice_q8_0` | 1.26 GiB | 1,755 MiB | 1.42 s | 0.65 | 5.5 s | 3/3 | 3/3 | 140.14 s | 99× |
| **piper** `es_ES-davefx-medium` | — | **0** | 0.57 s | 0.23 | — | 3/3 | 3/3 | 0.56 s | — |

The two rows with no CPU median were left out of the CPU profile for being too
big to have a chance there. On the card they are mid-table.

**Supertonic 3 is the answer, and now it is not a close one.** 0.07 s to speak
a line piper needs 0.57 s for — **8× faster than what the family hears today**,
about 40× real time, 3/3 on both word rules, no reference clip, in **637 MiB**.
That is 5% of this card. z-image, which was a deployed service on the same
GPU until it was removed, peaked at 4,760 MiB.

### VRAM does not track package size, and the exception is the model this bench started on

`qwen3_tts_0_6b_base_q8_0` — the model the whole first bench was built around —
**does not fit**. Given a card with **7,586 MiB free** it climbed to 11,609 MiB
and died asking for another 665 MiB:

```
ggml_backend_cuda_buffer_type_alloc_buffer: allocating 665.18 MiB on device 0:
    cudaMalloc failed: out of memory
ggml_gallocr_reserve_n_impl: failed to allocate CUDA0 buffer of size 697487616
```

So it wants **north of 5.9 GB** from 1.86 GiB of weights. The 1.7B model of the
same family — bigger on disk by 640 MiB — runs in **3,393 MiB** and answers in
0.66 s. The smaller package needs nearly twice the card of the larger one, and
nothing on the outside of either file says so.

That is the same lesson the CPU profile learned about wall time, in the
dimension that decides whether a thing runs at all. **Measure the footprint;
the parameter count and the download size are both wrong.**

### What did not fit, and why each one did not

| | |
|---|---|
| `voxcpm2_q8_0` | Runs, at **7,228 MiB** — outside any 4 GB budget, and 2/3 on the middle word besides. |
| `qwen3_tts_0_6b_base_q8_0` | Above. Over 5.9 GB, from a 1.86 GiB package. |
| `chatterbox_q8_0` | **Not a memory problem.** Every request shape returns *"Chatterbox supports VoiceCloning and VoiceConversion"* — it does not answer the `tts` route at all, whatever it is given. It wants a voice-cloning task, which is a different entry in `--task`, and this profile only speaks `tts`. Recorded rather than chased: a model the assistant would have to be re-plumbed for is not a drop-in for piper. |

### The measurement the card kept ruining

Three rows had to be taken twice. `qwen3_tts_1_7b` and `voxcpm2` failed with
allocation errors on the first pass, and the reason was not the models: Ollama
reloaded `qwen3-vl:4b` partway through the run, took 5.2 GB back, and the
bench measured the leftovers. The vision model is loaded *on demand* for
Alfred's vision turns, so "how much card is free" is not a constant on this box
— it is a function of whether somebody asked the assistant about a photo in the
last few minutes.

Two consequences worth keeping. **A failed allocation is not proof a model is
too big** — it is proof it was too big for what was left, and those are
different sentences. And any deployment of this would be a *permanent* tenant
on a card whose other tenants already collide: `docs/migration.md` and
`docs/local-generation.md` both record what that costs when it goes wrong.

### What the GPU build costs

The image. **2.89 GB against the CPU image's 187 MB** — 15×, and the CPU
image's size is one of the things this bench exists to defend. It is a separate
image (`audio-cpp:cuda`, built from `docker-compose.gpu.yml`), so
`audio-cpp:latest` is untouched and the CPU path stays what it was. Both are
built from the same Dockerfile with `BUILD_BASE`, `RUNTIME_BASE`,
`AUDIOCPP_BACKEND` and `CUDA_ARCH` as build args; `CUDA_ARCH=86` emits code for
this box's 3060 and nothing else, which is the difference between a few minutes
and half an hour.

### So where the speaking half stands now

`supertonic_3_q8_0` on the CPU is 2.50 s and a no. The same model on the card is
0.07 s, 637 MiB and eight times faster than what the house runs today, with the
words intact. That is not the "it only wins on the GPU, so it has lost" the
first bench expected to find, because the cost is a phone-sized slice of the
card rather than the whole of it — but it *is* a GPU dependency on a machine
whose GPU is contended, for a path that currently has none. The honest summary
is that the speaking half now has a real candidate with a real price, and the
price is the thing to decide about rather than the speed.

## The voice, not the clock — 2026-09-08, GPU, hard Spanish

Once the card settled the speed question, speed stopped deciding anything:
eight models answer in under 2 s and the slowest of those is faster than a
person notices. So `test/quality_tts.py` asks the other question, on six
sentences chosen for what actually breaks a Spanish TTS — `ñ`, accented
vowels, inverted marks, numbers it has to *say*, a proper noun, and the
English brand names a household says out loud every day:

> *Recuérdale a Sofía que el cumpleaños de su abuela es el próximo miércoles.*
> *La película empieza en Netflix a las nueve y el wifi del salón va lento.*

Every line is spoken twice and transcribed by the house's own whisper. WER is
every word, not one from the middle; accents are then checked separately, with
the diacritics on, on the words whose diacritics are the point.

| model | **WER** | worst line | perfect | accents | speak | VRAM |
|---|---|---|---|---|---|---|
| `omnivoice_q8_0` | **0.023** | 0.167 | 9/12 | 32/32 | 1.98 s | 1,755 MiB |
| **`supertonic_3_q8_0`** | **0.037** | 0.167 | 8/12 | **32/32** | **0.17 s** | **637 MiB** |
| `qwen3_tts_1_7b_base_q8_0` | 0.059 | 0.167 | 6/12 | 32/32 | 1.53 s | 3,393 MiB |
| `audio8_tts_preview_0_6b_q8_0` | 0.071 | 0.167 | 4/12 | 31/31 | 1.19 s | 1,921 MiB |
| `cosyvoice3_q8_0` | 0.076 | 0.167 | 6/12 | 32/32 | 0.90 s | 1,897 MiB |
| **piper** `es_ES-davefx-medium` (today) | 0.098 | **0.316** | 6/12 | 31/31 | 0.70 s | — |
| `pocket_tts_spanish_q8_0` | 0.119 | 0.231 | 3/12 | 30/30 | 0.10 s | 441 MiB |
| `magpie_tts_q8_0` | 0.203 | 0.267 | 0/12 | 8/8 | 1.53 s | 1,263 MiB |
| `moss_tts_nano_100m_q8_0` | 0.205 | 0.533 | 1/12 | 27/28 | 1.89 s | 527 MiB |

**Five models are more intelligible than what the house speaks with today**,
and the incumbent's worst line is the worst of the top six by a factor of two:
piper reads *"Cierra la puerta del garaje"* and whisper hears **"del legaje"**.

**Supertonic 3 is the recommendation on both axes at once.** 0.037 WER against
piper's 0.098, a worst line half as bad, 8 of 12 utterances transcribed
perfectly, every accented word intact, and it does that in **0.17 s** and
**637 MiB** — a quarter of the next-best model's error rate at a tenth of its
time. `omnivoice` is the most accurate thing here and costs 1.98 s and 2.8×
the VRAM for it; that is the trade if accuracy is the only axis.

`magpie`'s 8/8 accents is not a good score, it is a small denominator: accents
are only counted on words the model actually said, and it dropped most of them
(0/12 perfect lines). It also renders `película` as *"pelcula"*.

### The metric charged everyone for whisper's habits, first time round

whisper writes "9" for *nueve* and "Wi-Fi" for *wifi*. Scored literally that is
two word errors per line for saying them correctly, and it made the Netflix
sentence the worst line for five of the eight models for a reason that had
nothing to do with any of them — piper included. `_CANON` in `quality_tts.py`
now folds the handful of forms this corpus provokes, and only those; a general
number-to-words expander would be a second thing to get wrong. The ranking
barely moved. The absolute numbers halved, and they are the honest ones.

### What this still does not measure

Timbre. Whether the voice is pleasant, whether it sounds like somebody's
neighbour or a call centre, whether the household wants it at seven in the
morning. WER narrows nine models to two; ears pick between them, and
`--save-dir` writes every clip for that.

## Every Spanish family on the card — 2026-09-09, RTX 3060

The earlier tables profiled a shortlist. This is **all 18 TTS families whose
spec claims Spanish**, one GGUF package each (the family's recommended one),
each alone on the card so the VRAM figure is its own. `mid`/`onset` are the
two word rules; `shape` is what the request had to carry.

| package | warm | RTF | cold | VRAM | mid | onset | shape |
|---|---|---|---|---|---|---|---|
| `pocket_tts_spanish_q8_0` | **0.05 s** | 0.03 | 1.6 s | **441M** | 3/3 | **1/3** | clone |
| **`supertonic_3_q8_0`** | **0.12 s** | 0.04 | 9.9 s | **615M** | **3/3** | **3/3** | plain |
| `cosyvoice3_q8_0` | 0.67 s | 0.30 | 1.1 s | 1889M | 3/3 | 3/3 | clone |
| `audio8_tts_preview_0_6b_q8_0` | 0.73 s | 0.34 | 13.1 s | 5340M | 3/3 | 3/3 | clone |
| `qwen3_tts_0_6b_base_q8_0` | 0.75 s | 0.38 | 18.3 s | 2401M | 3/3 | 3/3 | clone |
| `moss_tts_nano_100m_q8_0` | 0.81 s | 0.46 | 3.7 s | 437M | **2/3** | 3/3 | plain |
| `higgs_audio_tts_4b_q8_0` | 0.84 s | 0.41 | 1.5 s | **7059M** | 3/3 | 3/3 | clone |
| `magpie_tts_q8_0` | 0.85 s | 0.35 | 16.1 s | 1209M | 3/3 | 3/3 | voice_es |
| `qwen3_tts_1_7b_base_q8_0` | 0.86 s | 0.45 | 23.5 s | 3417M | 3/3 | 3/3 | clone |
| `fireredtts3_instruct_q8_0` | 1.24 s | 0.55 | 1.6 s | 4547M | **1/3** | **0/3** | clone |
| `omnivoice_q8_0` | 1.41 s | 0.62 | 11.6 s | 1755M | 3/3 | 3/3 | clone |
| `voxcpm2_q8_0` | 1.66 s | 0.94 | 23.2 s | 4139M | **2/3** | **2/3** | plain |
| `index_tts2_q8_0` | 2.12 s | 0.74 | 3.1 s | 1798M | **2/3** | **0/3** | clone |
| `dots_tts_soar_q8_0` | 2.34 s | 0.61 | 20.7 s | 4443M | 3/3 | 3/3 | plain |
| `fish_audio_s2_pro_q8_0` | 2.50 s | 1.10 | 22.3 s | 6305M | 3/3 | 3/3 | plain |
| `outetts_1_0_1b_q8_0` | 4.83 s | 1.44 | 23.3 s | 2355M | 3/3 | 3/3 | plain |

**Two do not run here at all:**

| | |
|---|---|
| `chatterbox_q8_0` | *"Chatterbox supports VoiceCloning and VoiceConversion"* — to every request shape, reference clip included. It does not answer the `tts` route; it wants `clon` or `vc`, a different task and a different caller. Not a slow model, a wrong one. |
| `moss_tts_local_v1_5_q8_0` | *"failed to allocate moss.audio_tokenizer.decoder backend weight buffer"* on a card with 11.2 GB free. A 7 GB package that wants more than a 12 GB card has spare. |

**Supertonic is still the answer, and the field is why.** 0.12 s in 615 MiB
with both word rules clean is the only row that is fast, small and correct at
once. `pocket_tts` is twice as fast and drops two of three opening words --
1/3 here, its worst score yet. `cosyvoice3` is the runner-up on merit: 0.67 s,
3/3 on both, 1.9 GB, cloning from a reference rather than offering a menu.

**The quality columns are not decoration.** `fireredtts3` scores 1/3 and 0/3
-- it produced sound whisper could barely read -- and `index_tts2` 2/3 and
0/3. Both sit mid-table on speed and both are unusable. A table sorted by
seconds alone would have recommended either.

**Cold is not the same shape as warm.** `cosyvoice3` loads in 1.1 s and
`qwen3_tts_1_7b` in 23.5 s for warm times within 0.2 s of each other. On a box
where a model unloads between uses, that gap is the whole experience.

## What would make it a yes

Time to speak a short line, on the CPU, no worse than piper's, **with the
assistants running** — an idle-machine number is not one this house will ever
see. And the Spanish round trip at least as accurate as whisper's on the same
audio.

Supertonic is the first model to get within sight of that, and it still has
three things to prove before it is one: **time to first audio** in streaming
mode rather than the whole-file median measured here, which is the metric its
spec's `streaming` tag is actually about; the same number **with the assistants
running**, since these were measured on a box holding 12 GB of models but
answering nobody; and a **listening test** — 3/3 through whisper says the words
survived, not that the household wants this voice reading to them at seven in
the morning. `--save-dir` writes the clips for exactly that.

On the CPU that is still the list. **On the card it already clears the speed
bar by 8×**, and what is left to decide is whether a permanent 637 MiB tenant
on a contended GPU is worth 0.5 s a sentence — which is a question about this
household's card, not about the model.

## What would make it a no

The voice is not better than davefx by ear. Or the GGUF conversion is audibly
worse than the reference model. For the speaking half on the **CPU**: 2.5 s
against piper's 0.56 s is a 4.5× regression on a daily path, and unless
streaming turns that into a faster *first* syllable it stays a no however good
the voice is.

"It only wins on the GPU" was on this list and has been struck off as a
*criterion*, because it is now a measured fact with a number attached rather
than a disqualification: 637 MiB and 8× faster. What replaces it is narrower
and harder — the card is contended, Ollama took 5.2 GB back mid-run twice
while these numbers were being taken, and a TTS model resident on it is one
more thing for a vision turn to lose a race against.

## Risks worth naming

The CPU image is **187 MB** — the binaries strip from ~600 MB each, and without
that this would have shipped at 1.33 GB. For comparison the voice gateway is
~1 GB and a torch+CUDA image is several. The CUDA image is **2.89 GB**, which
is the torch+CUDA comparison landing on this bench's own head: the small image
is a property of the CPU build, not of audio.cpp.

It is **0.7.2, released 2026-09-04**, three weeks after 0.6. Weeks old, some
paths marked experimental, and 2.3k stars is interest rather than track record.
The voice panels are a daily path for this household, so the bar for moving
them is higher than "the bench looked good once".

Its published speed claims are its own — "200×+ real time" is Supertonic 3 on
a CUDA card, and the same model measured here on four CPU threads is 1.2×
real time. Both numbers are true and only one of them is about this house.

A thirty-model zoo is not thirty good models, and the profile above is the
receipt: seven Spanish-capable packages, one usable, one actively dangerous
(PocketTTS drops the verb and still scores 3/3 on the middle word), and a
120× spread that has little to do with size. z-image was the same cautionary
tale from the other side, where four quoted prompts on one fixed seed scored
4/4 and the honest rate was nothing like that — `docs/local-generation.md`.
