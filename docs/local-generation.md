# Local generation, and what this house does not run

A record rather than a service. `services/z-image` was here, drew pictures on
the household's own GPU, was measured, promoted from bench to service, and then
removed on 2026-09-08 because the household stopped using it. The measurements
are worth keeping even though the container is not, because the next candidate
will be argued against them.

## What z-image was, and what it cost

[Z-Image Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) — 6B
parameters, Apache-2.0, ungated, 8 diffusion steps — behind a 319-line
diffusers server speaking Together's `/v1/images/generations` dialect, so the
assistant's drawing skill could be pointed at it by changing one model string.

Measured on this box, RTX 3060 12 GB, 2026-09-07:

| | |
|---|---|
| speed | 32–33 s per 1024×1024, 9 steps |
| **peak VRAM** | **4,760 MiB**, dropping to ~170 MiB five minutes after the last picture |
| cold load | ~9–16 s from cache, ~195 s the first time |
| Spanish text | `MAÑANA`, `CUMPLEAÑOS`, `¿QUÉ HAY?` rendered correctly — and it misspelled on a bare prompt about one time in three |
| on disk | **10.5 GB of image and ~31 GB of weights** |

Three findings from it that outlive it:

- **Offload cannot rescue a module that does not fit.** With bf16 weights the
  pipeline sat at 10,730 MiB *with* `enable_model_cpu_offload()` on and OOM'd
  anyway, because model-level offload moves whole modules and neither of the
  two big ones fits alongside its own activations. `ZIMAGE_QUANT=4bit` took
  10.7 GB to 4.7. Quantisation is what fits; offload is not.
- **4/4 was not the hit rate.** Four prompts that each *quoted* the target
  word, on one fixed seed. Sampled properly afterwards, the bare form was 1/3.
  A score from a fixed seed is a demonstration, not a rate.
- **An idle reaper can eat its own pipeline.** `_last_used` started at `0.0`,
  so the first tick unloaded the model mid-generation and every request came
  back 507 — which looks exactly like "too big for this card".

## comfy-cli, and what it would and would not do here — evaluated 2026-09-08

[comfy-cli](https://github.com/Comfy-Org/comfy-cli) is Comfy-Org's official
command-line tool for [ComfyUI](https://github.com/comfyanonymous/ComfyUI):
`pip install comfy-cli`, GPL-3.0, and was the obvious question to ask
about a directory containing a hand-written 319-line diffusers server, and it
outlives that directory: it is how a *next* one would be built. It installs
ComfyUI and its dependencies, downloads models, manages custom nodes, launches
the server in the background, and — the part that matters for a stack with no
browser in it — runs a workflow from a terminal:

```bash
comfy install --skip-manager --workspace=/opt         # ComfyUI, no Manager
comfy model download --url <hf-or-civitai-url>        # weights, declaratively
comfy launch --background                             # headless
comfy run --workflow ./draw.json --wait               # and block for the result
```

**The first thing to say is that it is a packaging tool, and this repository
already has a packaging layer.** `comfy install`, `comfy update` and
`comfy node install` manage a mutable tree in `$HOME/comfy` with its own venv;
`deploy/` builds an image and ships it. Those are the same job, and the
`## Architecture` rules in `CLAUDE.md` are what this house decided about how to
do it. comfy-cli was never a candidate to replace `server.py` — it is not a
runtime. It is a candidate to *build* one, and it is a good one.

### What it would genuinely buy, if ComfyUI is ever benched here

- **`--skip-manager`.** `comfy install` pulls ComfyUI-Manager by default, and
  Manager is the component behind **CVE-2025-67303** — its config readable and
  writable through the web API without authentication, which is enough to make
  it install node packages — and the vector in the 2026 campaign that turned
  1,000+ exposed instances into a cryptomining proxy botnet. One documented
  flag removes it. That is worth more than it looks: the alternative is
  remembering to delete it.
- **`comfy node save-snapshot` / `restore-snapshot` is a lockfile.** The custom
  node set is this ecosystem's supply chain, and a snapshot pins it by name and
  version. It is the same discipline `services/audio-cpp`'s Dockerfile spells
  by hand, supplied by upstream.
- **`comfy model download`** is a declarative fetch with Hugging Face and
  CivitAI tokens read from the environment — the same shape as audio.cpp's
  `model_manager_v2.py install`, which this stack already runs inside a
  container and trusts.
- **`comfy run --workflow x.json --wait`** is a headless generation from a
  shell. That is what a bench needs and what a web UI cannot give: a check that
  asserts a produced payload rather than a status code, which is the rule in
  `CLAUDE.md` that most of this repository's verification hangs off.

### What it does not change

**The runtime's shape, which is the actual reason this house is not running
it.** ComfyUI's own self-hosting docs say it plainly: *"The ComfyUI server has
no authentication by default. Only bind to a non-loopback address on a network
you trust, or put a reverse proxy with authentication in front of it."* That is
sane for a workstation and wrong here, because of what would be calling it.
z-image accepted a **prompt string**; ComfyUI accepts a **program** — a graph
naming Python nodes to execute — and the thing holding the pen is an assistant
that summarises web pages through crawl4ai. Prompt injection into an agent that
can POST an arbitrary workflow graph is a different class of exposure from
prompt injection into one that can ask for a picture of a cat.

2026 gave that shape **CVE-2026-68771** (CVSS 9.8), unauthenticated RCE in a
*core* node — upload a pickle to `/upload/image`, reference it from `/prompt`,
`torch.load` deserialises it, fixed by `weights_only=True`; **CVE-2026-6591**,
path traversal; and the Manager bypass above. All patched. The pattern is the
point: the attack surface is an unauthenticated endpoint whose job is running
code somebody else wrote, and `comfy run` is a client for exactly that
endpoint.

**The card, either.** Ollama sat at 9.2 GB across two warm models; z-image
peaked at 4,760 MiB and dropped to ~170 MiB five minutes after the last
picture, because its idle unload was written for a box where the GPU is shared
with a vision model and the camera detectors. ComfyUI caches models to make the
*next* generation fast, which is the right default in front of a person and the
wrong one in a container. It is tunable and it would have to be re-derived, and the
idle-reaper and offload findings above are what that derivation costs.

### Three things to set explicitly if it is ever used

- **`--skip-manager`**, above.
- **`DO_NOT_TRACK=1`** (or `COMFY_NO_TELEMETRY=1`) **in the build environment.**
  comfy-cli has Mixpanel and PostHog wired in. Analytics are opt-in and off by
  default, and it asks on first run *in an interactive terminal* — a container
  build is not one, so it never asks and the default holds. This stack's claim
  is that `OPENCODE_API_KEY` is the only credential that leaves the network;
  a thing that could phone home gets pinned off in writing, not left to
  somebody else's default.
- **Never `comfy update` inside an image.** It is the command that makes the
  tree not match the Dockerfile that built it. Related, and worth knowing
  before trusting comfy-cli as the pinning mechanism: `comfy install` has no
  commit or tag flag — you get the current release and pin *afterwards* with
  `comfy update comfy --version <X>`. That is a weaker guarantee than
  `git clone --depth 1 --branch <tag>`, which is what
  `services/audio-cpp/Dockerfile` does and why it can say which commit its
  numbers came from.

### What any of this would actually be for

Not text-to-image. This house had that, measured it, and stopped using it —
so a new runtime for it would have to answer a question nobody is asking.
ComfyUI is the answer to a question this household has not asked yet: **video,
inpainting, upscaling, img2img** — things with no path here at all, where a
workflow file is genuinely cheaper than another bespoke server.

If that becomes the ask, the bench is *not* "ComfyUI vs z-image on a prompt".
It is **"can a 3060 with 7 GB free do a new generation kind at all"**, and
comfy-cli is how it would be built: pinned, `--skip-manager`, no custom nodes,
`DO_NOT_TRACK=1`, bound to the container network with nothing published, and
the assistant talking to a narrow adapter that accepts a prompt and picks the
workflow — never to `/prompt` itself. Off by default, started by hand, and a
bench until a measurement says otherwise.

**The audio half of "local generation" already has a candidate in the tree.**
`services/audio-cpp` ships music (`ace_step`, `stable_audio`,
`minimax_music3`), sound effects (`controlfoley`), separation (`htdemucs`,
`bs_roformer`) and audio super-resolution (`audiosr`) as GGUF packages on the
**CPU**, costing no VRAM — and its TTS profile is a warning about what that
costs in wall time on the big ones. Whether any of them is usable here is
unmeasured. It is the cheaper thing to measure first, because nothing new has
to be installed to find out.
