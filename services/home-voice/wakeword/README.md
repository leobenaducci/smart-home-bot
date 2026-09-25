# Training "Alfred"

The wake word runs on the **server**, not on the pucks. That is not a
compromise, it is what makes a custom word possible at all:

- **No on-device engine ships an "Alfred".** WakeNet — the only wake engine that
  runs on an ESP32-S3 without a tflite-micro port — has 29 built-in words: Hi
  ESP, Alexa, Jarvis, Computer, Sophia, Mycroft, Hey Willow, Hi Joy, Blue Chip
  and so on. Alfred is not among them.
- **Espressif's custom route is not self-service.** You supply 20,000 qualified
  corpus entries from 500+ speakers including 100 children, recorded in a
  professional audio room — or you pay them (`sales@espressif.com`, two to three
  weeks). Neither is a household.

Training our own and running it here costs one thing — the microphone streams to
this server whenever a puck is powered — and buys three: the model is entirely
ours, retuning it never means reflashing anything, and the firmware has no wake
engine and no VAD in it at all.

It costs *nothing* in availability. An on-device wake word would not help when
the gateway is down: a puck that heard you with no Alfred to ask has nothing to
say.

## The word

**"Alfred"** in English pronunciation, which is what the family already says.
Consider training **"Hey Alfred"** as well and preferring it: two syllables
false-trigger on ordinary conversation far more than three, and a device that
wakes up during dinner gets unplugged. Both can be trained; run whichever wins
the soak test below.

## Training it

openWakeWord, which is what the gateway loads. Two routes, same output:

**Hosted** — <https://openwakeword.com/train>. Generates the synthetic dataset,
optionally clones a voice for better accuracy, and runs the GPU job. Simplest.

**Notebook** — `dscripka/openWakeWord`, `notebooks/automatic_model_training.ipynb`.
Linux only, because it drives Piper for sample generation. Roughly:

1. Generate ~13,000 positive samples of "Alfred" with `piper-sample-generator`
   across many English voices, with speed and pitch variation.
2. Generate negatives — clearly different phrases, plus music, noise and speech
   corpora — so the model learns what *not* to fire on.
3. Augment with noise, reverb and room simulation.
4. Train, export **ONNX**.

You already have Piper in the gateway image if you want to generate samples
locally, though the English voices matter here rather than the Spanish one baked
in for TTS.

## Installing it

Drop the `.onnx` beside `devices.json` on compute:

```
/home/homestack/home-lab/configs/home-voice/
├── devices.json
└── wake.onnx        <- here
```

Then `docker restart voice-gateway`. No rebuild, no redeploy — the model is
live config exactly like the device registry, for the same reason: this is a
file you will iterate on.

Until it exists the gateway logs, on every stream:

```
no wake model at /config/wake.onnx — every stream will connect and never wake
```

and tells the device so, which shows up as a fault on the LED ring rather than a
puck that looks fine and ignores you.

## Tuning it

`WAKE_THRESHOLD` (default `0.5`) in the compose environment. Raise it if the
house trips the word by accident, lower it if quiet voices are missed. It is a
restart, not a rebuild.

Two measurements, neither of which is "it worked when I tried it":

1. **False accepts.** Leave a puck in the kitchen for a full day of normal life
   — talking, television, radio, dishes. Count the wakes nobody asked for. The
   gateway logs every one as `wake (0.87)`, so `docker logs voice-gateway | grep
   wake` is the whole measurement. More than a couple a day is a model that will
   get unplugged.
2. **True accepts.** Each person, about three metres away, says the word ten
   times. Count. The quiet voices and the children are the ones that fail, and
   they are the ones who will complain least — ask them specifically.

## If you later want it on-device

Nothing in the protocol depends on where the wake word runs. A puck that grew an
on-device engine would stop streaming and start posting to `/v1/turn`, which
still exists and is still the simpler path for a device that can decide for
itself when to talk. The realistic route there is **microWakeWord** —
<https://microwakeword.com/train> trains ESP32-S3 models — but its inference
lives in ESPHome, so taking it would mean rewriting the puck as ESPHome plus a
custom component to reach this gateway. That trade did not look worth it while
the server has 56 idle cores.
