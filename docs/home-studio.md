# Home studio: video, images, music and voices on the household's own card

Status: **first version** (2026-09-28): the manager, the Studio page and Alfred's
`studio` skill. The measurements below are from the prototype on this card.

## What it is

One service, `home-studio`, owns one GPU and nothing else touches that card.
Everything generative goes through it: a video from a storyboard, a picture,
a song with lyrics, a voice cloned from a sample. People reach it from the
portal (a Studio page, signed in as themselves) and from Alfred (a skill), and
both land in the same queue.

The engines are [WanGP](https://github.com/deepbeepmeep/Wan2GP) (Wan2GP), used
headless through its Python API -- **home-studio uses WanGP**, and says so in
its interface as WanGP's terms ask. One WanGP session runs inside the manager's
own process, so one process decides what is on the card at any moment.

| Job | Model (WanGP id) | Why this one |
|---|---|---|
| Video with sound | MiniMax H3 FL2VA, pruned 20B, int8 (`minimax_h3_fl2va_pruned`) | open weights, synchronized stereo audio, first/last frame, continuation |
| Video from references | MiniMax H3 Ref2VA (`minimax_h3_ref2va_pruned`) | characters and voices kept from reference clips |
| Image | Z-Image Turbo (`z_image`) | measured here before: ~33 s at 1024², Spanish text |
| Song with vocals | ACE-Step 1.5 (`ace_step_v1_5_turbo_lm_1_7b`) | MIT, Spanish, up to 10 min, fits 12 GB |
| Instrumental / effects | Stable Audio 3 | licensed training data, up to 6 min |
| Voice clone (speech) | Qwen3 TTS base / IndexTTS2 | measured in Spanish in audio-cpp |
| Voice replacement (singing) | SeedVC | a sung track in a cloned voice |
| Lyrics, shot prompts | the household's assistant model | writing is not the card's job |

## The manager

- **One queue for the card**, persistent across restarts. Every job from every
  person is in it; each person sees their position, an estimate, and the
  other jobs ahead by owner and kind (never their prompts or content).
- **Fair order**: a long video is many shot-jobs, interleaved with other
  people's short ones, so one film does not hold a picture for an hour.
- **Model residency**: the model a job needs is loaded once and kept while
  jobs for it are next; a swap is minutes, so the queue groups by model
  where that does not starve anybody. Idle, it unloads everything.
- **Projects**, per person, on the big disk: a timeline of shots (prompt,
  duration, references, the take chosen and the takes kept), songs, images.
  Save, load, duplicate. A retake never deletes the previous take.
- **Long videos** are shots generated in order, each starting from the last
  frame of the one before, then stitched (ffmpeg, on the CPU).
- **Changing one part** re-generates that shot only, anchored to the last
  frame before it and the first frame after it, so both joins still match.
- **Assembly**: music or narration under a video, crossfades, final render.
- **Alfred** writes and polishes lyrics and shot prompts, and can submit and
  report on jobs through the same API.

## How the pieces fit

    phone / browser ── portal /studio (page) ── /studio/api/* ──┐
    Alfred ── skill `studio` ── portal /studio/api/* ───────────┤  login, name, parent?
                                                               ▼  + derived secret
                                    home-studio :21040  (FastAPI, one process)
                                      ├─ store.py     the queue (SQLite), fair order, estimates
                                      ├─ projects.py  per-login projects on disk
                                      ├─ manager.py   one job at a time; continuity; filing
                                      ├─ media.py     ffmpeg: frames, stitch, mix (CPU)
                                      └─ worker.py    child process holding WanGP → GPU
                                               │
                                    done → portal /studio/api/notify → ntfy → phone

- The portal forwards `/studio/api/*` with `X-Studio-User` (the login),
  `X-Studio-Name` and `X-Studio-Admin`, and the secret `derive_studio_secret()`
  makes from `PROXY_SHARED_SECRET` -- nothing to type, nothing to rotate.
- The studio's API is published on the LAN port but refuses anything without
  the secret; nothing outside the house reaches it.
- **WanGP runs in a child process.** Its API keeps the last model loaded and
  has no call to give the card back, so the manager stops the worker after
  `idle_s` (default 10 minutes) with nothing to do. A crash in a model takes
  the worker, not the queue or the page.
- The worker reports over its own pipe: WanGP's API captures stdout while a
  job runs, so nothing printed there can be a report.

## Things learnt building it

- **One cuBLAS.** A `nvidia/cuda` base image plus PyTorch's cu130 wheels put two
  `libcublasLt` versions in one process, and the first matrix multiply failed
  with `CUBLAS_STATUS_INVALID_VALUE`. The image is plain Ubuntu; the wheels
  bring CUDA, the NVIDIA runtime brings the driver.
- **Triton INT8, not Comfy Kitchen.** WanGP's default INT8 kernels refuse
  ACE-Step's layer shapes on an RTX 3060 ("dimensions multiple of 4");
  `int8_kernels: triton` (in `wgp_config.json`) runs it.
- **SDPA attention and profile 5** for H3 on 12 GB, as the WanGP research found.
- **The GGUF Q2_K text encoder** (`config: gguf_q2_k`): 8.5 GB of RAM instead
  of ~65 GB for BF16.

## A music video from a song

The 🎬 button on a song, voice or instrumental that has a take. The page cuts
the take's length into shots of the chosen length (5-15 s, default 8) -- that
is arithmetic, so the video always covers the song -- and the portal asks the
person's own assistant only what each shot shows, following the words in order
(`/studio/api/music-video`: exactly N descriptions as JSON, asked once more if
the count is off). The shots land in the Video tab as ordinary shots, queued if
asked, with the song remembered as the project's `settings.soundtrack`: the
film dialog then preselects it and mutes the shots' own sound.

It is slow: H3 measured ~5 card-minutes per second
of video here, so a three-minute song is hours. The dialog says how many from
`video_rate` in `/api/queue` (the median of this card's own shots), and the
queue's estimates scale with each shot's seconds (`Store.seconds_for_job`).

While it works: the shot on the card shows WanGP's own in-progress picture
(decoded from the latents on the CPU, written by the worker every few seconds,
`GET /api/jobs/<id>/preview`, the owner's only); ▶ Preview plays the chosen
takes in order in the browser with the soundtrack moved to each shot's start,
and a card with the description where a shot is not made yet. Every queued or
running item has its own cancel, and the Video tab a cancel for all of them.

## Where things live

Everything on the big disk (`/mnt/data`), nothing on the system SSD except
the container image:

    <paths.state>/home-studio/models          WanGP checkpoints (~52 GB), backup: skip
    <paths.state>/home-studio/data/projects   <login>/<project>/ -- project.json, takes/, uploads/, renders/
    <paths.state>/home-studio/data/queue      the queue, so a restart resumes it
    <paths.state>/home-studio/data/logs       worker.log: WanGP's own console

`paths.state` is on the big disk on this house (`/mnt/data`); only the image
(~20 GB) is on the system SSD.

What is kept is compressed, on the CPU, as each take is filed
(`studio/media.py`): songs and voices as MP3 (VBR ~190 kbps -- the generators
write WAV at ~10 MB a minute, and WAV takes from before this are converted
once at start-up), and every shot and film as H.265 (`X265`, CRF 23, tagged
`hvc1`), about half the H.264 the generator writes. The price of H.265 is
playback: phones, the Android app and Safari play it; Firefox and most Linux
desktop browsers do not, and show a video that will not start. A retouch hands
the generator an H.264 copy of the shot, never the kept file.

## Settings

`services.home-studio`: `enabled`, `port` (21040), `gpu`, `gpu_device` (the
card it owns), `idle_s`, `mem_limit` (the container's RAM cap, 36g: H3 streams
weights through RAM, and an out-of-memory must stay its own). `dns.studio`
names its host for the portal. WanGP is pinned (`STUDIO_WANGP_REF`, the
Dockerfile's `WANGP_REF`): bump it on purpose and measure again.

## Measurements

Filled in by the prototype (RTX 3060 12 GB, 62 GB RAM, `--profile 5`,
`--attention sdpa`, CUDA 13):

| Job | Settings | Time | Peak VRAM | Peak RAM |
|---|---|---|---|---|
| image | z_image 1024×1024, 8 steps | 62 s (model already on disk; 6.8 s of it loading) | 5.1 GiB | 13.1 GiB |
| song | ACE-Step 1.5 turbo + 1.7B LM, 60 s, Spanish, int8 on Triton | 324 s (63 s loading) | 2.9 GiB | 8.6 GiB |
| video | H3 FL2VA pruned int8 + Q2_K text encoder, 832×480, 124 frames (5.2 s), 20 steps | 1506 s (457 s of it loading, cold) | 3.9 GiB | 28.4 GiB |
