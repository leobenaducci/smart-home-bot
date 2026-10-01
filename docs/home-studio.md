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

The **Storyboard tab** is where the frames are worked on: each shot's frame,
its time on the song and the words sung in it, its description (the same text
the Video tab edits -- there is one description per shot, not two), redraw,
and 📌 to make any picture already in the project its frame (a reference, a
generated image, another shot's frame; copied into the shot's folder,
`board_from`). A frame keeps the description it was drawn from
(`boards[].prompt`, the job's `shot_prompt`), so a shot described differently
since is marked, and "redraw the changed ones" redraws exactly those. The tab
makes no video. The **Video tab** reads the storyboard instead, by a switch at
its top that is on unless turned off (`settings.use_storyboard`): on, a shot
that starts fresh shows its frame as the picture it starts from, in place of
its own start-picture choice, and `generate` sends it as `start_board`; off,
the frames are not used. A video remembers the frame it started from
(`takes[].board`), so a video made before its frame was drawn -- or from
another frame -- says so, and Regenerate makes it from the frame.

### One correction for every shot

A note that is true of the whole storyboard -- "it is night in every shot",
"Mora always wears the red coat", "closer shots" -- is written once, in the box
under the tab's toolbar. The portal (`/studio/api/board-correct`) hands the
Designer (`assistant.models.designer`, the storyboard's writer) every shot's
description in order, the project's look, the characters and the note, in one
call, so the correction is applied the same way everywhere, and asks for exactly
as many descriptions back, a shot the note does not concern unchanged. The ones
that changed are saved under the assistant's name in the project's history --
one step to undo -- and redrawn, unless the box's "redraw" is unticked; a frame
left undrawn is marked changed like any edited shot. Recorded shots are not
descriptions and are left out.

### Drawn from pictures: the cast's own, and the film's style

Z-Image reads words only, so a character it draws is whoever the description
makes them -- a different face every frame, and a family's real people come
out as strangers. A frame or a portrait is drawn instead on **FLUX.2 klein 4B**
(`recipes.REF_IMAGE_MODEL`) whenever it has pictures to go on:

- **each cast member's chosen picture** -- their portrait, or the photo they
  were given -- up to three, so each looks like themselves;
- **the film's style pictures**: reference pictures flagged 🎨 in the Files
  tab, uploaded as such, or made one from any frame or image (🎨 beside 📌).
  Two at most; a third is refused rather than quietly left out.

The pictures are found when the job runs (`manager._refs`), and the prompt
says which is which: "image 1 is Bruma ... draw each of them as that picture
shows them, even when it is a photograph, in the style below; images 2 and 3
show the film's style: match their medium, rendering, line, palette and light,
not their content". One deleted since is left out and the numbering follows.
With no pictures at all, the frame is Z-Image's from words, as before. klein
4B shares Z-Image's text encoder and is Apache-licensed; the 9B is not.

A character's look can be **written from its picture** (✍️ on its card): the
house's own vision model (`assistant.models.vision`) describes what an
illustrator would need -- age, build, hair, face, clothes -- and nothing about
who they are; it is saved under the assistant's name. The picture never
leaves the house.

### Style lives in the look, and only there

The project's look is put ahead of every frame's description and into every
shot's video prompt. So the descriptions carry no style of their own: two
places naming the style is two places that can disagree. Measured on a
household's music video (2026-10-01): its look said "realista", every shot
said "Disney style cartoon", and a correction had written "matching his
reference photo" into 29 of 34 shots. The image model never sees a photo;
it reads the word. The frames came out 2D, 3D and photographic by turns, and
redraws told to restate the look wrote "clean, realistic" into them.

- The Designer's redraws and corrections are told to name no style, repeat no
  look and mention no photo or reference picture (`STUDIO_STYLE_RULE`). A
  correction also takes such words out of descriptions that already have them.
- A correction about the style of the whole piece ("make it a 3D cartoon")
  changes the **look**, under the assistant's name, and every frame is redrawn.
  The Designer is asked to say 2D or 3D: "cartoon" alone is both.
- A frame is reviewed for style against the look and against up to two other
  frames, and only frames that passed **in the style** are used for that.
  Unreviewed frames used to stand in, and in a storyboard drawn in mixed
  styles the verdicts contradicted each other.
- A frame off-style scores at most 4 (6 if partly), whatever else it gets
  right.

### Reviewing the frames

Every frame drawn is looked at when it lands -- a first drawing, a redraw,
the planner's, the assistant's (the Studio carries a review budget on each
frame's job and hands the filed frame to the portal's `/studio/api/frame-review`;
`review: false` on the request skips it). The looking is the house's own
vision model (`assistant.models.vision`, called directly on the endpoint the
deployer resolves for it; the pictures never leave the house), in two steps:
the shot is first made into a checklist of what a single still must show
(camera movement, sound and anything over time left out), then each item is
marked on the frame shown, partly or not, with rendering defects apart. The
score is counted from the marks -- the model does not choose it: asked to,
it listed a frame's real problems and still gave it 9/10.

Under 7, the Designer (`assistant.models.designer`, text only) writes the
prompt to draw it with instead. The card shows it in an editable box with one
action, "Use and redraw": it becomes the shot's description and the frame is
drawn from it, and the new frame is reviewed in turn -- description, frame and
video agree. 🔁 Refine runs that loop on every frame by itself, up to twice
each; the descriptions it rewrites are in the project's history under the
assistant, to undo.

### Cutting on the beat

A generated song keeps one tempo, and the beat tracker's beats wander around
it (0.395-0.557 s apart on a steady 0.492, a tenth of them more than 150 ms
off), so a song that keeps one tempo gets one: the tempo and phase whose
beats sit on the most onset strength, within 3 % of the tracker's
(`analysis.steady_grid`), less the onset envelope's one-hop latency. Section
changes snap to the bar line within half a beat. "Fit to the song" refits the
shots a project already has onto the song's bars and section changes -- each
keeps its place and its words -- and names the shots whose video is now
shorter than its new length.

## Characters

Who appears (`studio/characters.py`): a name, how they look (said the same
way in every storyboard frame and shot they are cast in -- the words are what
keep them recognisable), a personality (for the lines the assistant writes
them), pictures (uploaded, or a 🎨 portrait drawn from their look) and a
voice sample, recorded in the page or uploaded and kept as WAV, that their
lines are cloned from (▶ try it). A shot lists its cast; the music video's
planner is given the characters and names them on each shot.

A character starts in its project and its scope only widens, when the
person says so: this project -> all of mine -> the family's
(`<data>/projects/<login>/<project>/characters/`, `<login>/.characters/`,
`@family/characters/` -- `@` is never in a login). Widening moves the folder
and keeps the id, so projects that cast it keep finding it. Nothing narrows:
somebody else's film may be using a family character. Its creator and a parent
may edit or delete one; anyone may cast it. All of it is under the Studio's
data directory, in the same backups as the projects.

Not measured yet, and the reason a face can still drift between frames: an
image model that takes the character's picture as a reference (WanGP has
editing models that do), and replacing H3's own voice in a shot's dialogue
with the character's.

## Recording (the Recording kind)

🔴 in a Recording (or free) project: the screen, the camera or both -- the
camera in a corner over the screen, drawn on a worker's clock because a
page's own timers crawl while the person is in the window being recorded --
with the microphone. The browser's recorder hands over a piece every five
seconds, and each goes up as it is made (`/recordings/<id>/chunk?n=`, retried),
so a closed tab or a dropped network loses seconds, not the take. Finishing
joins the pieces in order -- they are one stream cut up -- and encodes the clip
on the CPU (H.265, 30 fps), beside the card's queue; a clip that fails to
encode keeps its pieces. Each recording is a clip of the project, in the same
timeline as generated shots, so the preview, the film and the downloads work
on it unchanged. Screen recording is a computer's browser only; in the
Android app the camera also needs the app to grant it (it grants only the
microphone today).

What a recording gets afterwards, each on the CPU pool beside the card:
**subtitles** -- its speech sent to the house's speech recogniser (faster-whisper,
on its own card; tuned for speech, which is why lyrics go to the aligner
instead), kept on the version as a transcript and SubRip `.srt`, downloadable
and burnt into a film when asked (libass, DejaVu Sans) -- and **taking out its
long silences**: stretches below -35 dB for over 1.2 s are cut, leaving 0.3 s
either side so no word is clipped, as a new version with the original kept.
A trimmed version needs its own subtitles; the transcript belongs to the
version it was made from.

With a transcript, ✨ asks the person's assistant for a title, a short
description and chapters -- where the talk changes subject, at the times the
transcript gives, the first at 0:00 (`/studio/api/describe`). They are edited
before they are kept on the clip, and copied as one text in the "0:00 Title"
form video sites read chapters from.

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

## Sheet music and practice

🎼 on a song's (or instrumental's) version writes out its parts -- a `score`
job, about a minute on the card for a 90-second song -- and then opens
`/studio/practice` for them:

- **Stems** first: HTDemucs on the audio unit (`htdemucs_q8_0`, 62 MB) splits
  the version into vocals, drums, bass and "other", where guitars and keys
  land. They become the practice page's play-along tracks: the song without
  its guitars and keys (`minus.mp3`) and those alone (`part.mp3`).
- **Notes** from the whole mix, not the stem: MuScriptor (`muscriptor_small_f32`,
  412 MB, audio.cpp's `midi` task) asked for acoustic, clean and distorted
  guitar and acoustic and electric piano. On the first song tried the mix told
  a strummed acoustic from a picked electric, and the separated stem heard it
  all as one acoustic and lost the riff.
- **The score** (`studio/score.py`), as MusicXML: guitar as notes over
  tablature with chord names, piano on two staves, one part per instrument
  heard more than a handful of times. The grid is fitted to the notes
  themselves -- the beat tracker said 123 bpm where the strums fell every
  0.2475 s (121.2), a second and a half of drift over the song -- and the bar
  starts where chords change. Tab is a string and fret per note, chosen a
  chord shape at a time along the cheapest path for the hand; a note the
  guitar cannot play stays in the notation and out of the tab.

The files sit beside the version (`takes/<song>/<version>-score/`) and leave
with it. The practice page draws the score in the browser with alphaTab
(staged in the portal as its module build: the classic build starts its
workers from `blob:`, which the portal's `'self'` policy refuses). It plays
with the song, the minus-one, the part alone, or the notes on a synthesizer
(count-in and metronome there), at 25-125 % speed with pitch kept, and a
stretch dragged across the score repeats. The cursor follows the real song
through alphaTab's external-media handler: the score is written at a
whole-number tempo, and the page maps it onto the song's by the ratio of the
two and the score's start. MusicXML (MuseScore opens it) and MIDI download.

It is written by ear from a generated recording: chords and rhythm are
usually right, a fast run may not be, and the page says so.

## History

Every project's words are under version control
(`studio/history.py`): a git repository in `<project>/.history/` with one
JSON file per shot, song and picture holding what a person writes in it --
descriptions, dialogue, sound, lyrics, style, prompts, titles, lengths, cast
-- plus the project's name and settings, the order of each section and its
own characters' text. Never a picture, clip or song, and never the list of
versions the card made. `git log -p` in the folder reads as the edits were
made.

Each save is a revision by whoever made it, with "(Alfred)" when their
assistant did (the portal sends `X-Studio-Via` for a member's own token). A
person's saves minutes apart fold into one revision (the page saves while
you type), and something typed and undone inside one leaves nothing. The
snapshot is taken when the save is made; only git runs behind the request.

🕘 History on the project lists the revisions and their tags. Each can be
looked at word by word, **undone alone** -- only what it changed, and only
where nothing changed it since; the rest is reported, not overwritten --
**gone back to**, making the project read as it did then, or **tagged**.
Undoing and going back are revisions too, so both can be undone. Nothing is
lost to them: an item that leaves the timeline -- by the page's ✕ as well --
keeps its full record, versions and all, in `<project>/.history-removed/`,
and the revision that brings it back brings them back (those whose files
still exist). A copy of a project keeps the history it was copied from.

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
    <paths.state>/home-studio/data/projects   <login>/<project>/ -- project.json, takes/, uploads/, renders/,
                                              .history/ (the words' git), .history-removed/
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
