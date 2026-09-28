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
                                    home-studio :<port>  (FastAPI, one process)
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

## Kinds of project

A project says what it is for (`kind`: music video, short film, explainer,
podcast, recording, free). The kind decides the page's starting shape and the
flow Alfred plans it with; every tool stays available in every kind. Only the
music video and free have a flow today -- the others are listed as coming.

## Storyboard

A frame per shot before any video: a picture on the image model, about a
minute, where the shot is ~25 minutes. `POST /api/projects/<p>/storyboard`
queues one for every shot without one (or the shots named, to redraw), drawn
with the project's look (`settings.look`, what the person said it should look
like) ahead of the shot's description, at the image size nearest the video's
shape (`recipes.BOARD_SIZE`). Frames are kept per shot (`boards`, the chosen
one `board`), apart from its takes. A shot that starts fresh -- the first, or
one that does not carry on from the one before -- starts from its chosen frame
(`start_board`), so what was approved is where the video begins; a shot that
carries on starts from the last frame of the one before, and its frame is only
for looking at. Until a shot is made, the page and the preview download show
its frame in its place: an animatic, timed to the song. The music video's
planner draws the storyboard first unless asked to go straight to video.

## A music video from a song

The 🎬 button on a song, voice or instrumental that has a take. The Studio
first **listens** to it (`studio/analysis.py`, a job in the card's queue like
any other): its own audio.cpp -- the `audio` unit, on the Studio's card --
separates the vocals (Mel-Band RoFormer) and places each word of the known
lyrics on them (Qwen3 forced aligner); librosa finds the beats; the lyrics' own
tags name the parts. Measured on a 90-second song: separation 23 s with the
model load, alignment 4 s. Forced alignment rather than transcription, because
the words are known -- the house's speech recogniser, tuned for short spoken
commands, heard one wrong line for the whole song. On the separated vocals,
every line boundary the aligner gave fell inside a measured pause in the
singing.

The cuts then fall on the music (`plan_cuts`): a part of the song starting
within reach wins, then the nearest bar, then the nearest beat. Every cut is a
frame, the last is the song's end, and each shot is generated at least as long
as its cut (`h3_frames_at_least`) and read only up to it when the film is made
-- so the shots add up to the song to the frame, where rounding to H3's lengths
alone left a 90-second song half a second short. Alfred is given each shot's
time, its part and the words sung during it, and the music-only stretches as
such (`/studio/api/music-video`), and the shots land as ordinary shots marked
`exact`: the page shows their cut instead of a length slider, and the preview
stops each at its cut. The song becomes `settings.soundtrack`, so the film
dialog preselects it and mutes the shots' own sound.

The audio.cpp instance unloads its model five seconds after using it and holds
one at a time (`--idle-unload-ms 5000 --max-loaded-models 1`), and the manager
waits for `/v1/models` to say nothing is loaded before the next job: the card
is the video generator's. Two measured quirks: the aligner must be given 16 kHz
mono (given 44.1 kHz it reports seconds at the wrong rate), and a separation
request names a file the server opens, so the Studio's data is mounted in it at
the same path. Without the audio unit -- or for a song with no words -- the
cuts still follow the beat; only the words are lost.

The preview can be downloaded ("⬇ with the music"): the same video as a
file, rendered on the CPU beside the queue. Every shot is in place -- one not
made yet is a still card with its description (`media.placeholder`) for the
length it will have -- so the song runs under it unbroken, and every frame
carries a watermark (`media.watermark`, drawn with Pillow rather than ffmpeg's
drawtext, whose text needs escaping for any colon or quote in a description):
PREVIEW in a corner, and the shot, its time in the song and its version along
the bottom. It is a draft, so it is H.264 at a fast preset (`media.FAST`):
seconds where H.265 takes a minute, and playable in every browser. The film
itself stays H.265 and still leaves missing shots out; its song follows
each shot's place in the video (`media.follow`), so a gap no longer puts the
shots after it out of time with their words.

A version can be marked the favourite (⭐): it is the one used, and a new
version no longer takes its place.

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

While it works the bar is honest about what it knows. WanGP's steps place
it where there are steps; a stage with none -- ACE-Step's lyrics-to-music, a
model loading -- moves with how much of this kind of job's usual time on this
card has gone, never reaching the stage's end, because WanGP's own figure there
is not a measure (it says 100% as the stage starts, and a song sat at 95% for
five minutes looking stuck).

## Changing a song

✏️ on a song's version, two ways, each a new version (the one it came from is
kept, and the new one remembers the words it was sung with):

- **A part.** The song's lines, each at the time it is sung (the song is
  listened to first if it has not been). The lines ticked or rewritten set the
  stretch -- with 0.3 s of air either side -- and only that is made again,
  with the new words: ACE-Step's *repaint* on the audio unit (audio.cpp;
  WanGP's ACE-Step has no repaint route). Measured on a 20-second excerpt with
  3.6 s repainted: every second outside the stretch identical to the original
  (correlation 1.00), the stretch itself new (0.26-0.39), blending back at its
  edges. ACE-Step there is ~6 GB, so the manager lets the video generator go
  before a repaint; the next video job loads it again.
- **All of it, close to this one.** ACE-Step's *cover* mode on WanGP: the whole
  song again from the version, held to it by "how close" (Source Audio
  Strength, 50-95%), keeping the singer's timbre too if asked.

## Deleting

A version (a take) is deleted with its 🗑: the clip or picture and the frames
taken from it leave the disk, and the item keeps its other versions. A file
somebody uploaded is deleted the same way from Files, and whatever pointed at
it -- a shot's start picture, a voice's sample -- lets go. Both are explicit
requests by id (`DELETE /api/projects/<p>/items/<i>/takes/<t>`,
`/uploads/<name>`) and ask first. A card's ✕ only takes the item out of the
project; its versions stay on disk until the project is deleted. That is on
purpose: the page saves the whole project, so a page holding an older copy
sends fewer items than there are, and a save must never be able to delete a
file.

## Optional, and what off means

`home-studio` is off in the example and arrives off on an upgrade
(`NEW_SERVICES` in deploy.py -- without that, a config with no block would
read as on and build an 18 GB CUDA image). Off is off everywhere, not only
the container:

- the portal gets no `STUDIO_URL`, answers `/studio` with "not configured"
  and leaves Studio out of the apps menu;
- the assistants lose the `studio` skill (`when_service` on both units), so
  it is not in any prompt;
- a drawing role still set to `studio:…` exports nothing and the drawing
  skill says it has no model, rather than posting to a door that is closed;
- the portal's dashboard has no Studio tile (on, it is a `basic` tile to
  `/studio`, left out for someone away while it is house-only).

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

`services.home-studio`: `enabled`, `port` (21035), `gpu`, `gpu_device` (the
card it owns; "0" by default, the free one on a machine with two), `idle_s`, `mem_limit` (the container's RAM cap, 36g: H3 streams
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
