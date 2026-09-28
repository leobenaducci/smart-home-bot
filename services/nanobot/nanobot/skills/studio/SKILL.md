---
name: studio
description: "Invoke with JSON: {\"skill\":\"studio\",\"action\":\"...\"}. The house Studio: generate on the house's own card -- make_image(prompt, [size]) | make_song(lyrics, style, [seconds], [language]) | make_instrumental(style, [seconds]) | make_video(description, [seconds], [dialogue], [sound]) | studio_queue() | my_projects(). Use it when a message asks to make, draw, generate or compose a picture, a photo, a song, music, a jingle or a video. Everything goes into the person's default Studio project, where they can open, change and redo it."
# On demand: the description carries the invocation and the API.
metadata: {"nanobot":{"translatable":true}}
---

# Studio

The house has its own generator on a card of its own. Every request is queued
behind everybody else's -- it is one card for the whole family -- and the
person gets a notification when theirs is ready. It lands in their **default
Studio project** ("Alfred") -- one place for everything asked of you -- in the
Studio page (the Apps menu → Studio), where they can watch it, redo it,
change part of it, move it into a project of its own or put a film together.

Write the JSON block as plain text in your reply; the system intercepts and
runs it. Never exec, curl, or Python you write.

```json
{"skill": "studio", "action": "make_image", "prompt": "A cosy kitchen at golden hour, a grey cat asleep on a chair, a sign that reads \"BUENOS DÍAS\"", "size": "1024x1024"}
{"skill": "studio", "action": "make_song", "lyrics": "[Verse]\n...\n\n[Chorus]\n...", "style": "latin pop, acoustic guitar, warm female voice", "seconds": 90}
{"skill": "studio", "action": "make_instrumental", "style": "calm piano with soft rain", "seconds": 60}
{"skill": "studio", "action": "make_video", "description": "A grey cat walks along the garden path toward the camera and looks up", "seconds": 10, "sound": "birds, wind in the leaves"}
{"skill": "studio", "action": "studio_queue"}
```

- **Prompts in English work best** for pictures and video; the text *inside* a
  picture (a sign, a title) stays in the language it should be read in.
- **Songs need lyrics.** When asked for a song about something, write the
  lyrics yourself first, in the person's language: section tags on their own
  lines (`[Verse]`, `[Chorus]`, `[Bridge]`), a blank line between sections,
  6-10 syllables per line. Then call `make_song` with them. `style` is genre,
  instruments, mood and voice, not the words.
- **Video** is long work: minutes per 5 seconds on this card. A video longer
  than 15 s is split into shots that continue one another. `dialogue` is what
  a character says aloud, in the video's language.
- `size` for a picture: `1024x1024` (square), `1344x768` (wide), `768x1344` (tall).
- The answer says where the job is in the queue and roughly when it starts.
  Tell the person that plainly, and that they will be notified; never claim
  it is done. `studio_queue` says what the card is doing and who is waiting,
  by name and kind only.
