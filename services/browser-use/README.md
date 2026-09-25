# browser-use — an agent that drives a real browser

[browser-use](https://github.com/browser-use/browser-use) (MIT) opens pages,
clicks, types and fills forms in a real Chromium. It is here for the jobs
`web_search` and Bright Data's `scrape_as_markdown` cannot do: the thing is
behind a login, or behind a form, rather than sitting in a page's text.

**Off by default.** Turn it on in `config/home-stack.yml`:

```yaml
services:
  browser-use:
    enabled: true
```

## Read this before switching it on

This is the only thing in the stack that *acts on other people's sites as you*.
Everything else here reads. That difference is worth a minute:

- **It clicks.** A model decides what to click, and it will get it wrong
  sometimes. On a page that only reads back, wrong is a wasted turn. On a page
  with a "Confirm order" button, wrong is an order.
- **Sessions are real sessions.** Anything you log it into, it is logged into —
  and so is anybody who can ask the assistant to use it.
- **Sites notice.** Automated browsing gets accounts rate-limited or closed;
  some terms of service forbid it outright. That is the household's call to
  make, per site, not this service's.

## What it cannot reach

It opens the **public internet and nothing else**. A URL that resolves to a
private address is refused before a browser starts, and non-http(s) schemes
(`file://`, `chrome://`) are refused outright.

That check runs on the URL the caller hands over. The page it lands on is what
tells the model where to go *next*, so the browser profile carries a second,
weaker guard for every navigation after the first: IP-literal URLs are refused,
and so are the house's own names -- `localhost`, `*.home`, `*.lan`,
`host.docker.internal` and this stack's own containers. Weaker, and worth
knowing which half is which: that one matches names and never resolves them, so
a public name pointed at a private address, or a container name it does not
spell, still gets through on the second hop.

That guard is not paranoia about the household. It is that this is the one
service here whose URL is chosen by a model **after reading somebody else's
page**, so a page can ask for one. Without it, "now open
http://192.168.1.10:21002" is an instruction it would carry out.

It is deliberately not wired to `site.lan_cidr`, which is what the assistants'
own `ssrfWhitelist` reads. That exemption is granted to nanobot, which takes
its instructions from a person. `BROWSER_USE_ALLOW_CIDRS` is the knob here, it
is empty by default, and empty is the answer for almost every household.

Setting it costs more than the range it names. browser-use's IP-literal block
is all-or-nothing and is checked before any allow list, so naming one CIDR
turns that block off for *every* address on every navigation after the first --
loopback and `169.254.169.254` included. The entry URL is still resolved and
still refused; what widens is the link the model may follow on a page it has
just read. The service logs a warning at startup saying so.

The narrow, sane use is the household's own accounts on sites that offer no API
— a utility bill, a school portal, a booking that has to be made in a form. The
default configuration below is built for that: the browser starts fresh every
time and keeps nothing, so a session exists only for as long as the task.

## What it costs

| | |
|---|---|
| image | ~1.5 GB — it ships Chromium |
| memory | Chrome is per-tab hungry; budget ~1 GB while a task runs |
| GPU | none |
| network | it browses the internet, unlike almost everything else here |

## The model it thinks with

browser-use needs a model of its own to decide what to click, separate from
whichever model is answering in the chat. It is pointed at the house's Ollama by
default — slower than a frontier model at this, and the pages it reads stay on
this machine, which is the trade this stack makes everywhere else.

`BROWSER_USE_MODEL` and `BROWSER_USE_BASE_URL` override it. Pointing it at a
paid API means every page it reads is sent there; that is a decision, not a
tuning knob.

## How the assistant reaches it

As an MCP server over HTTP, the same way the assistants reach `alfred-mcp`.
Turning the service off removes the entry from their configuration entirely
rather than leaving it pointed at nothing — see `apply_service_wiring` in
`deploy/deploy.py` for why an unreachable MCP entry is not merely inert.

## Verifying it

```bash
curl -s http://127.0.0.1:21034/healthz          # is the browser up
python services/browser-use/test/smoke.py       # fetch a known page and read it back
```

The smoke test also asserts that the browser still refuses loopback,
link-local and `file://` -- the same check `crawl4ai` carries next door, and
the one worth re-running after any version bump, because a guard that has
quietly stopped guarding looks exactly like one that works.

The smoke test drives a real page and asserts on its *content*, not on a status
code — a browser that starts, navigates nowhere and returns an empty body
answers 200 all day.
