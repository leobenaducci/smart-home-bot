---
name: news
description: "Read current news and headlines. Use when asked what's in the news, for headlines on a topic, or what's happening with something recent."
metadata: {"nanobot":{"emoji":"📰","requires":{"bins":["curl"]}}}
---

# News

Free, no API key needed. Uses Google News RSS.

## Search news by topic

```bash
curl -sL "https://news.google.com/rss/search?q=QUERY&hl=en-US&gl=US&ceid=US:en"
```

For Chile/Spanish results, use `hl=es-419&gl=CL&ceid=CL:es` instead.

## Top headlines (no topic)

```bash
curl -sL "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
```

Always pass `-L`: Google News 302-redirects when it normalizes the `ceid`
parameter, so without it curl returns an empty body.

## Parsing

The response is RSS/XML. Each story is an `<item>` with `<title>`, `<link>`, `<pubDate>`, and `<source>`. Read these directly out of the XML — list the most relevant/recent items with title, source, and date.

## Reading a full article

Titles/links from RSS are just headlines. To read the full article, use the `web_fetch` tool on the item's `<link>`.
