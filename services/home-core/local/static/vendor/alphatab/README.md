# alphaTab 1.8.4

The practice page's score renderer and player (`/studio/practice`), from the
npm package `@coderline/alphatab@1.8.4`, MPL-2.0 (`LICENSE`).

Staged here rather than loaded from a CDN, like every other asset of this
app: the portal's Content-Security-Policy is `'self'` and it is meant to be.
The module build is the one used -- the classic one starts its workers from
`blob:` URLs, which that policy (rightly) refuses; this one starts
`./alphaTab.worker.mjs` beside itself.

| File | From the package | Why the name changed |
|---|---|---|
| `alphaTab.mjs` | `dist/alphaTab.min.mjs` | the entry imports `./alphaTab.core.mjs` whatever its own name |
| `alphaTab.core.mjs` | `dist/alphaTab.core.min.mjs` | 1.1 MB minified instead of 2.3 MB, same exports |
| `alphaTab.worker.mjs`, `alphaTab.worklet.mjs` | the `.min.mjs` of each | loaded by those names, beside the entry |
| `font/Bravura.woff2` | `dist/font/` | SIL OFL 1.1 (`font/Bravura-OFL.txt`) |
| `soundfont/sonivox.sf3` | `dist/soundfont/` | the synthesizer's instruments (`soundfont/LICENSE`) |

To update: `npm pack @coderline/alphatab@<version>`, copy the same files the
same way, and run the practice page's check in `test_studio_portal.py`.
