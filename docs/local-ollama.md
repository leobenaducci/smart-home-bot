# The household's own Ollama: any number of servers

**Since 2026-09-23 the servers are a list the stack manages**:
`cloud.ollama.instances` in the config, edited on the admin page's Models tab
("This house's Ollama servers"). Each entry is one `ollama serve` with its own
cards, port, window per slot, parallel slots, models kept loaded, KV cache
type and keep-alive. A role picks its server with "Runs on" beside its model,
which is written as the model's prefix (`ollama-<id>:<model>`).

- `./home-stack ollama` -- the cards, what already sits on each (from
  `nvidia-smi`, by unit or container), each server, and what an apply would
  change. Writes `gpus.json` and `ollama-plan.json` beside the config, which
  is how the page knows the cards.
- `./home-stack ollama --import` -- writes the list from the `ollama*.service`
  units running now, so it starts out describing exactly what is there.
- `./home-stack ollama --apply` -- one drop-in per managed unit,
  `/etc/systemd/system/<unit>.service.d/zz-home-stack.conf`, sorting last so it
  wins over the hand-written ones below without deleting them; `DeviceAllow=`
  is reset before the pin, because device rules accumulate across drop-ins. A
  server with no unit yet gets one (`User=ollama`, the shared model store).
  Then daemon-reload and a *restart* of what changed.
- `sudo ./home-stack ollama --install-trigger` -- once. Installs
  `home-stack-ollama.path`/`.service` and a root-owned copy of the two files
  in `/usr/local/lib/home-stack-ollama`, so the page's Apply button can do what
  `--apply` does. Re-run it when the page says the helper is out of date.

The page's VRAM view: each card's size and other tenants come from the host's
last look; each server's share is estimated from its models' metadata
(`admin/ollama_vram.py`: weights from the file, less a per-layer embedding
table that stays in RAM; KV from the layers, heads and sliding window) or
measured with its Measure button (`/api/ps` plus the unit's real total from the
host, the difference being CUDA context and compute buffers -- 1.3 GiB for
gemma4 at 40k x 1). The sections below are the measurements that shaped the
layout before it was a list, kept for their reasoning.

## Setups, engines and the model library

**A setup** (`cloud.ollama.setups`, "Local models" on the Models page) is a
model at a window, a KV cache type, a number of slots and a thinking switch,
with a name and optionally a card. Only setups a role uses run -- one server
each, kept loaded, placed on a card by the page when Card is Auto. A role names
one as `ollama-<setup id>:<model>`.

**An engine** per setup:

| Engine | Runs | Why pick it |
|---|---|---|
| Ollama | Ollama library names, `hf.co/owner/repo:TAG` | The default; one server per setup |
| llama.cpp | a GGUF: `hf:owner/repo/file.gguf` (downloaded into `/var/lib/home-stack/models`), a `.gguf` there, or an Ollama name whose blob it reads | Window per slot, slot count and KV type per task, not per server |
| llama.cpp (PrismML) | the same | The only runtime for Bonsai's ternary packings (PQ2_0, PTQ1_0) |

The llama.cpp engines run as `llamacpp-<id>` units from a root-owned copy of a
build `./home-stack llamacpp build [vanilla|prism]` made. Builds are pinned in
`deploy/llamacpp.py`, compiled in a CUDA container for these cards and this CPU
(`GGML_NATIVE`), with NCCL off and CUDA's runtime libraries shipped beside the
binary. A setup's `context` is per slot; the unit's `-c` is context x slots.

**The model library** (`deploy/model_library.py`, the page's Library tab)
lists what is on disk in both stores and **tests each model on each engine that
could run it** -- loaded on the CPU with a small window, asked for one token --
right after it arrives. It exists because two Ollama-library models applied on
llama.cpp did not load, and the roles on them went down. A setup's model is
picked from the library, each marked by how it tested on the engine chosen
beside it; one that failed there cannot be picked. "Other" takes a name the
library does not hold yet.

```bash
./home-stack ollama --library        what is on disk and how each tested
./home-stack ollama --library-run    run the page's queued pulls, downloads, deletes, tests
```

**The Preview button** runs the save's placement on the unsaved form and draws
the cards, so a change can be seen fitting (or not) before anything restarts.

### Benchmarking a model the house does not run

A benchmark of a model already loaded for the house reuses that server. One
that is not needs a card, and the page frees one step by step, measuring the
card again after each and stopping as soon as the model fits:

1. the local text setups, their roles detoured to the everyday cloud model
   until the run ends (`/shared-state/model-detours.json`, read by every
   assistant);
2. `audio-cpp` stopped (no spoken replies meanwhile);
3. `faster-whisper` stopped (no voice input meanwhile);
4. the vision setup (camera descriptions pause).

Everything is put back in reverse order when the run ends, whatever failed.

---

## Before the list: two instances, one card

**Since 2026-09-21 both instances sit on GPU1**, and audio sits alone on GPU0.
The layout below this section is the one measured on 2026-09-11/12 and kept
for its reasoning; the numbers that are true today are these, measured after
`~/single-gpu-ollama.sh` ran (text `1 x 40960`, vision `1 x 8192`):

| GPU1 (`04:00.0`, `/dev/nvidia1`) | resident |
|---|---|
| text `:11434` -- `gemma4:e4b`, 40k x 1 slot, q4 KV | 4.58 GB |
| vision `:11435` -- `qwen3-vl:4b`, 8k x 1 slot (unloads after 5 idle min) | 4.35 GB |
| home-cameras YOLO (`yolo26m` motion; `yolo26x` scene once Alfred asks) | 0.84-1.28 GB |
| `embeddinggemma` (Paperless AI, second slot of the text instance) | 0.6 GB when loaded |
| **everything loaded** | **~10.8 GB** of 12.29 |

| GPU0 (`03:00.0`, `/dev/nvidia0`) | resident |
|---|---|
| audio-cpp -- Supertonic only since 2026-09-21 (`asr_packages: none`; faster-whisper transcribes) | **0.67 GB** (2.2 GB when it also held PocketTTS + Qwen3-ASR) |

Why audio is not on the same card: it was, for twenty minutes. With text,
vision and YOLO resident (10.5 GB) a synthesis pushed GPU1 to 11.2 GB and the
transcription that followed failed -- `failed to allocate
qwen3_asr.thinker.weights backend weight buffer`. Audio is the one transient
consumer, it needs 2.2 GB not the 1.7 estimated, and it does not have to
share a card with anything; `services.audio-cpp.gpu_device: "0"` is the whole
change. Every LLM and both detectors are on one card, which was the point.

Why the vision instance dropped from 5.9 GB to 4.35: its unit ran
`OLLAMA_CONTEXT_LENGTH=65536` -- a 64k KV cache for calls that use ~3k --
while this file said "small window". `describe_image` asks for 8192 and
`cloud.ollama.vision.context` now says so, so nothing reloads.

The pins: `ollama-vision.service.d/gpu.conf` now allows `/dev/nvidia1`, the
same card as `ollama.service`; `tuning.conf` on the text unit carries
`OLLAMA_NUM_PARALLEL=1` and `OLLAMA_CONTEXT_LENGTH=40960`; a `tuning.conf`
drop-in on the vision unit carries `OLLAMA_CONTEXT_LENGTH=8192`. The cameras
and audio containers name their card with `services.<svc>.gpu_device`, and
`cloud.ollama.local.context: 40960` is what every caller that names a window
(Paperless, `describe_image`) now names -- a different number would reload
the model on each call.

---


**This stack does not deploy Ollama.** `cloud.ollama` in the config only says
where it is. So how it runs -- which card, how many slots, what window -- lives
in systemd on the machine and in nobody's repository, and a reinstall or a
second machine gets none of it. This file is that record: what this house runs,
why, and what each choice was measured at. Every number here is from this box,
two RTX 3060 12 GB, on 2026-09-11/12, Ollama 0.33.3.

## The layout

| Card | Instance | Holds | Measured, loaded |
|---|---|---|---|
| GPU1 (`04:00.0`, `/dev/nvidia1`) | text, `:11434` (`ollama.service`) | `gemma4:e4b` for the local text roles and Paperless's AI; `embeddinggemma` for Paperless's index | 4.5 GiB at 1 slot x 48k, 5.8 GiB at 3 x 64k |
| GPU0 (`03:00.0`, `/dev/nvidia0`) | vision, `:11435` (`ollama-vision.service`) | `qwen3-vl:4b` for photos and camera clips | 6.6 GiB at 64k, beside 1.1 GiB of camera detectors and 0.6 GiB of voice |

### Why two instances

`OLLAMA_NUM_PARALLEL` and `OLLAMA_CONTEXT_LENGTH` are **server-wide**: one
server gives every model it loads the same slots and the same window. The text
roles want several slots so a notification is not queued behind a heartbeat; an
image turn wants one slot and a large window of its own. Two servers is the only
way to have both. They share one model store, so a model pulled once is
available to both.

### Why each is pinned to a card

Unpinned, Ollama's `llama-server` splits even a small model across both cards,
with pipeline parallelism, and keeps compute buffers on each:

| `gemma4:e4b`, 3 slots x 64k | VRAM |
|---|---|
| split across both cards | 7.3 GiB (compute buffers 2.3 GiB) |
| on one card | **5.8 GiB** (compute buffers 0.9 GiB) |

Pinned, the text roles also cannot compete with the camera detectors, voice or
an image turn, and where a model lands stops depending on what happened to be
loaded when it arrived. Unpin only for a model that does not fit one card --
and then only the text instance.

## The files

`/etc/systemd/system/ollama.service.d/tuning.conf`:

```ini
[Service]
Environment=OLLAMA_FLASH_ATTENTION=1
Environment=OLLAMA_KV_CACHE_TYPE=q4_0
Environment=OLLAMA_NUM_PARALLEL=2
Environment=OLLAMA_MAX_LOADED_MODELS=2
Environment=OLLAMA_CONTEXT_LENGTH=65536
```

`MAX_LOADED_MODELS=2` is what lets `embeddinggemma` sit beside `gemma4:e4b`
without evicting it.

`/etc/systemd/system/ollama-vision.service` is a unit of its own, not a drop-in:
`OLLAMA_HOST=0.0.0.0:11435`, flash attention, `q4_0`, `NUM_PARALLEL=1`,
`MAX_LOADED_MODELS=1`, `CONTEXT_LENGTH=65536`.

The pins, `/etc/systemd/system/ollama.service.d/gpu.conf` and
`/etc/systemd/system/ollama-vision.service.d/gpu.conf`:

```ini
[Service]
DevicePolicy=closed
DeviceAllow=/dev/nvidia1 rw        # nvidia0 in the vision unit's copy
DeviceAllow=/dev/nvidiactl rw
DeviceAllow=/dev/nvidia-uvm rw
DeviceAllow=/dev/nvidia-uvm-tools rw
DeviceAllow=/dev/nvidia-modeset rw
```

(The text unit's copy still says "3 slots x 64k" in its comment. It is 2.)

**`CUDA_VISIBLE_DEVICES` cannot pin Ollama.** It discovers cards through NVML,
which ignores that variable, and then sets it itself for every runner,
overwriting whatever it inherited. Deny the device node instead. Which node is
which card is in `/proc/driver/nvidia/gpus/*/information` ("Device Minor").

Changing any of these is `sudo systemctl daemon-reload` **and a restart** --
reload alone does not move a running process between cgroups. The restart
unloads whatever that instance holds, mid-request. To confirm where it landed:

```bash
journalctl -u ollama --since -5min | grep "inference compute"   # pci_id=0000:04:00.0
```

`gpu.conf.pinned-backup` in the text unit's directory is the older pin, from
when GPU1 belonged to another engine. It ends in `.pinned-backup`, so systemd
does not read it.

## The rule that is easy to break: one window per model

Ollama **reloads a model whenever a request asks for a different window than
the one it is holding.** A caller that sends `num_ctx` of its own against a
server at a different default reloads the model on every call and evicts
whatever the assistants had resident -- and nothing reports it; it reads as
slowness. So every caller here that names a window names the server's:

| Caller | Sends | Must equal |
|---|---|---|
| Paperless's AI | `PAPERLESS_AI_LLM_CONTEXT_SIZE`, from `cloud.ollama.local.context` (or `.vision.context`) | the instance its model is on |
| Camera clip review | `CLIP_REVIEW_NUM_CTX`, default 65536 | the vision instance |
| `describe_image` | 8192, **only** when the vision role is on `:11434` | -- so do not point the vision role at the text instance without changing that |
| the assistants, the titler | nothing | the server's default applies |

**`cloud.ollama.local.context` in the live config must match
`OLLAMA_CONTEXT_LENGTH` in `tuning.conf`.** The stack cannot read systemd, so
the config states it. Change both together, and redeploy `home-paperless`.

Embeddings are the one exception that is safe: Paperless sends its LLM window
to `embeddinggemma` too, and Ollama clamps it to the model's 2,048 without a
reload (observed: both models stayed resident through an index build).

## What the text model costs

`gemma4:e4b` is 8B parameters, but 18 of its 42 layers reuse another layer's
KV cache and 20 of the remaining 24 are sliding-window layers holding 512
tokens. Only 4 layers keep a cache that grows with the window, so at `q4_0` a
token costs **4,608 bytes per slot**. Its 5.4 GB per-layer embedding table
stays in host RAM, which is why a 9.6 GB download is 2.8 GiB of weights on the
card.

| Slots x window (per slot) | On one card | Split over two |
|---|---|---|
| 1 x 48k | **4.5 GiB** | |
| 3 x 64k | **5.8 GiB** | 7.3 GiB |
| 3 x 128k | | 10.1 GiB |
| 2 x 64k (current) | not measured, ~5.2 GiB | |

Split, the rule of thumb was 4.45 GiB fixed plus ~15 KB per token of
slots x window; on one card the per-token part is roughly a third of that,
because the compute buffers are not duplicated.

## What the vision model costs, and why it is not Gemma

`qwen3-vl:4b` at 64k, `q4_0`, one slot: **6.6 GiB**, of which the KV cache is
2.5 GiB, weights 2.4 GiB and the vision encoder up to 1.3 GiB.

It charges almost exactly **one prompt token per 32x32-pixel block** --
1920x1080 ~2,040, 1296x2304 ~2,950, 1440x1920 ~2,700. That is what sizes the
clip reviewer: at 720p a frame is ~900 tokens, so 30 frames (30 s at 1 fps) are
~27k, inside 64k with room to think. At 1080p the same 30 frames would be ~61k.

`gemma4:e4b` can see too, and replacing the vision model with the resident text
model was tried (2026-09-11, the assistant's own prompt, five images):

| | qwen3-vl:4b | gemma4:e4b |
|---|---|---|
| patio at night, empty | nobody there | **a person** |
| front at night, car, empty | car, nobody | car, **a person**, "headlights on" (it was the rear) |
| living room, one person | correct | correct |
| living room, IR, one person | correct | person, wrong clothing |
| an invitation's date, time, place | correct | correct |
| per image | 2-5 s | 6-11 s |

Gemma invented a person on both empty night frames -- the worst answer to
"is anybody in the patio" -- and looked at each image through ~500 prompt tokens
to Qwen's 2,000-3,000. Qwen stays.

## Settings that live only on this machine

Besides the systemd files above:

- **The live config** (`/var/lib/home-stack/config/home-stack.yml`), edited
  by hand or on the admin page, never tracked: the model roles
  (`assistant.models.*`), `cloud.ollama.local.context`, and
  `services.home-paperless.embeddings`. Back it up; `docs/backups.md` says how.
- **Paperless's database**: the AI workflow, and who can see which label. See
  the AI section of `services/home-paperless/README.md`.

Write to the live config in place (an editor that saves over the file, or
Python `open(p, "r+")`) rather than with `sed -i`. The admin page no longer
cares -- it reads the directory since 2026-09-12 -- but anything else that
bind-mounts the file as a single file would be left holding the deleted copy.
