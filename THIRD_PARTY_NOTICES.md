# Third-party notices

home-stack is MIT-licensed (see [LICENSE](LICENSE)). The components below are
included in this repository under their own licenses, which continue to apply
to them. Their license texts sit next to them.

| Component | Where | License | Notes |
|---|---|---|---|
| [nanobot](https://github.com/HKUDS/nanobot) | `services/nanobot/` | MIT | Forked and modified; see `services/nanobot/LICENSE`, its own `THIRD_PARTY_NOTICES.md`, and `services/nanobot/AGENTS.md` for what changed. Its media (`case/`, `images/`, `webui/public/brand/`) comes with it. |
| Skill format and upstream skills adapted from [OpenClaw](https://github.com/openclaw/openclaw), via nanobot | `services/nanobot/nanobot/skills/` | MIT | Credited in `services/nanobot/nanobot/skills/README.md`. |
| canvas-design skill, from [anthropics/skills](https://github.com/anthropics/skills) | `services/nanobot/nanobot/skills/canvas-design/` | Apache-2.0 | Modified for this stack; `LICENSE.txt` beside it. |
| [KaTeX](https://github.com/KaTeX/KaTeX) 0.17 | `services/home-core/local/static/vendor/katex/` | MIT; fonts SIL OFL 1.1 | Unmodified; `LICENSE` beside it. |
| [IBM Plex](https://github.com/IBM/plex) Sans and Mono, [Fraunces](https://github.com/undercasetype/Fraunces) | `services/home-core/local/static/vendor/fonts/`, `services/home-cameras/web_server/static/fonts/`, `services/mqtt-dashboard/dashboard/public/fonts/` | SIL OFL 1.1 | Unmodified Google Fonts subsets; `OFL.txt` beside each copy. |
| [Socket.IO](https://github.com/socketio/socket.io) client 4.0.1 | `services/home-cameras/web_server/static/js/socket.io.js` | MIT | Unmodified; the copyright banner is kept in the file. |
| Espressif arduino-esp32 CameraWebServer example | `services/home-cameras/esp32/camera-server/` | Apache-2.0 (`app_httpd.cpp`), LGPL-2.1 (the rest of the example) | Modified; see `NOTICE` there. |
| [Gradle wrapper](https://gradle.org) | `services/proxy/android/gradlew*`, `gradle/wrapper/`, `services/home-cameras/android/gradlew.bat` | Apache-2.0 | Unmodified; headers kept. |

## Not included, but worth knowing

Everything else is pulled when the stack is built or run — container images,
Python and npm packages, Arduino libraries, and model weights — and is not
redistributed by this repository. Running them unmodified in their own
containers creates no obligation for this repository, but some are not
permissively licensed, and that matters if you **distribute built images** or
**offer the stack as a service to others**:

- **Ultralytics YOLO** (the camera detector in `home-cameras`, and its weights)
  is AGPL-3.0. A built `home-cameras` image you distribute, or run as a service
  for other people, carries AGPL obligations.
- **Paperless-ngx** is GPL-3.0 and **SearXNG** is AGPL-3.0; both run as their
  upstream images, unmodified.
- **n8n** is under the Sustainable Use License, which is not an open-source
  license; it is off by default.
- **python-telegram-bot** is LGPL-3.0; **paho-mqtt** is EPL-2.0/EDL-1.0; the
  **arduinoWebSockets** library used by the panel firmware is LGPL-2.1, which
  matters if you distribute firmware binaries.
- **Models** (Ollama, Hugging Face, Piper voices) each carry their own license
  or terms of use. Check the model card of anything you download.
