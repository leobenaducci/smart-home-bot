# The coding benchmark

Admin page → Models → *Coding benchmark* (`/models/coding`). It answers one
question: which local model should the Programmer run on this house's card?

A model is served on the Studio's card by the engine picked for it -- Ollama,
llama.cpp, or PrismML's fork -- and opencode, the Programmer's own harness,
drives it through five fixed problems: read, edit, run the tests, repeat.
The same problems for every model, so rows compare.

| Problem | Language | Kind | What it takes |
|---|---|---|---|
| `cpp-lru` | C++ | fix | an LRU cache whose `get` does not refresh and whose `put` duplicates a key |
| `cpp-expr` | C++ | implement | a recursive-descent evaluator to a grammar in the header, errors included |
| `cpp-ring` | C++ | fix | a ring buffer that reads past its storage and double-frees on copy, under ASan/UBSan |
| `py-invoices` | Python | fix | a discount with the wrong sign, and a CLI flag the tests expect |
| `py-slots` | Python | implement | time parsing, interval merging and free slots, from docstrings |

## How an attempt is graded

Each problem has tests the model can run and hidden tests it never sees. After
the model stops, the protected paths (the tests, the Makefile) are put back as
they were, the build directory is removed, the visible check runs, then the
hidden tests are added and run. **✓** is both green with the tests untouched;
**½** is the visible tests green and the hidden ones not -- an answer that
fits the tests rather than the specification; **✗** is neither. Editing the
tests is reported and graded against the real ones.

`deploy/codebench/test_codebench.py` keeps the problems honest: each fails as
given, passes with its reference solution (`solution/`, never shipped), and
the hidden tests catch a half answer.

What is kept per attempt: every tool call (what it ran, its output, how long),
the model's text, its turns, its tool errors, its tokens, the diff it made,
what each check printed, and opencode's log when it failed. Per run: the load
time, the card's memory with the model loaded, and llama-server's median
reading and writing speeds.

## Running it

Tick problems and *Run the ticked problems*, or *Run only this* beside one, or
*Run this one again* from a result. One model at a time; a second run waits
in the queue. The model is loaded once for all the problems of a run.

## The card

It borrows the Studio's card the way the Programmer's local model does: the
Studio is paused -- after its running job, or, with *Stop the Studio's running
job now*, at once, that job going back to the queue to start over -- and
resumed when the queue is empty. Anything else on the card (the Programmer's
local model) is waited for, never evicted: a run starts when no more than
1.5 GB of the card is in use.

## The containers

All started by the admin container through the docker socket:

- `codebench-server` -- the model, on the card. llama.cpp is the house's own
  build (`./home-stack llamacpp build`) in the CUDA image it was built in;
  Ollama is the host's binary and runners over the host's store, mounted
  read-only. Published on `127.0.0.1:11483` for the page to watch it load.
- `codebench-run-<run>-<problem>` -- one attempt: opencode (pinned to the
  Programmer's release), g++, make, pytest, and the problem's starting files.
  No host directory is mounted, it runs unprivileged, and it is removed after.
  The hidden tests reach it on stdin, after opencode has exited; the image
  never holds them, because the model can read the whole filesystem.
  (bubblewrap would have been lighter; this host restricts unprivileged user
  namespaces.)
- the network `codebench` between them. It is a plain bridge, so opencode can
  fetch its provider package; opencode's own `webfetch` is denied.

The sandbox image is built on the first run after any change to it, tagged
by a hash of its contents. Results are files in the admin state directory
(`codebench/results/`), one per run. A run is cut short if the admin
container restarts; the next start removes what it left and resumes the
Studio if the benchmark had paused it.
