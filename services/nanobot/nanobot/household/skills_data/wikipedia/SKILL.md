---
name: wikipedia
description: "Look up facts, definitions, and summaries from Wikipedia. Use when asked to search Wikipedia, look something up, or explain/define a topic, person, place, or event."
metadata: {"nanobot":{"emoji":"📖","requires":{"bins":["curl"]}}}
---

# Wikipedia

Free, no API key needed. Uses the public Wikipedia REST/Action API.

## Search for a page

```bash
curl -s "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=QUERY&format=json"
```

Returns candidate page titles in `query.search[].title`. URL-encode spaces in QUERY.

## Get a summary

Fastest way to answer "what is X" / "who is X":

```bash
curl -s "https://en.wikipedia.org/api/rest_v1/page/summary/TITLE"
```

Replace spaces in TITLE with underscores. Returns JSON with `extract` (plain-text summary), `description`, and `content_urls.desktop.page` (link to share).

## Get a fuller article extract

When the summary isn't enough:

```bash
curl -s "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro&explaintext&titles=TITLE&format=json"
```

Drop `&exintro` to get the full article body instead of just the intro.

## Other languages

Swap the `en.` subdomain, e.g. `es.wikipedia.org` for Spanish results.

## Workflow

1. If the exact page title isn't known, search first, then fetch the summary/extract for the top result.
2. If the title is obvious (e.g. "Raspberry Pi"), skip straight to the summary endpoint.
