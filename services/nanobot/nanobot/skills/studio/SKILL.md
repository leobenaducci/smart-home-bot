---
name: studio
description: "Invoke with JSON: {\"skill\":\"studio\",\"action\":\"...\"}. The house Studio: generate on the house's own card -- make_image(prompt, [size]) | make_song(lyrics, style, [seconds], [language]) | make_instrumental(style, [seconds]) | make_video(description, [seconds], [dialogue], [sound]) | studio_queue() | my_projects() | project_details(project) | clone_project(project, [name]) | create_character(project, name, look, [personality], [portrait]) | edit_character(project, character, [name], [look], [personality], [voice_text]) | song_timing(project, [song], [shot_seconds]) | set_storyboard(project, shots, [song], [shot_seconds], [look], [replace]) | make_shot_videos(project, [without_frames]). Use it when a message asks to make, draw, generate or compose a picture, a photo, a song, music, a jingle or a video, or to work on a Studio project by name: its characters, its storyboard, its music video. Loose requests go into the person's default Studio project."
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

## A project of its own: characters, storyboard, music video

When the person names a project ("el video de Faro Zorro"), work **in
that project** with its own tools -- never a document, never a loose
`make_video` into the default project.

```json
{"skill": "studio", "action": "project_details", "project": "Faro Zorro"}
{"skill": "studio", "action": "create_character", "project": "Faro Zorro", "name": "Bruma", "look": "An anthropomorphic red fox, white chest and tail tip, yellow rain slicker and blue knitted cap, a brass lantern in one paw", "personality": "Calm, curious, hums while she works", "portrait": true}
{"skill": "studio", "action": "clone_project", "project": "Faro Zorro", "name": "Faro Zorro - versión 2"}
{"skill": "studio", "action": "edit_character", "project": "Faro Zorro", "character": "Bruma", "look": "An anthropomorphic red fox wearing a yellow rain slicker and a brass lantern in one paw"}
{"skill": "studio", "action": "song_timing", "project": "Faro Zorro", "shot_seconds": 8}
{"skill": "studio", "action": "set_storyboard", "project": "Faro Zorro", "shot_seconds": 8, "replace": true, "shots": [{"prompt": "Wide shot at dusk: Bruma climbs the spiral stairs of a white lighthouse, lantern swinging, waves below", "cast": ["Bruma"], "continues": false}]}
```

- **Characters** are the project's recurring people and creatures. `look` is
  one fixed English description (body, clothes, colours, props) -- it goes
  into every frame and shot the character is cast in, so write it once and
  well. `portrait: true` draws a reference picture of it.
- **Clone a project** to make a copy of it with its own takes and characters;
  the original is left untouched.
- **Edit a character** by name to change its `look`, `personality`, `voice_text`
  or `name`. The character is matched by exact name, or by partial name when
  only one matches.
- **A storyboard that fits the song**: call `song_timing` first. It answers
  with the cuts -- each shot's time, its section and the words sung in it
  (the first time, the Studio has to listen for a minute: say so and call it
  again). Then write **exactly one shot per cut**, each showing what is sung
  in it (music-only shots carry the mood), and call `set_storyboard` with the
  same `shot_seconds`. Put the characters on screen in `cast`, by name.
- `set_storyboard` draws **still frames only**. The person looks at them in
  the project's Storyboard tab, changes or redraws what they want, and starts
  the videos there -- each video starts from its frame. `replace: true`
  replaces the project's current shots (for "do it again"); without it the
  new shots are added after the existing ones.
- `make_shot_videos` generates the videos of the shots that have none. It is
  long work on the family's one card: only when the person asks for the
  videos themselves, never as a step of "make the storyboard".
- Report plainly what was queued and where to look; never claim a frame or a
  video is finished.

