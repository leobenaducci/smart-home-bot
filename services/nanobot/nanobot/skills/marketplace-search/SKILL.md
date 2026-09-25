---
name: marketplace-search
description: "Search retail/marketplace sites and compare prices (MercadoLibre, Falabella, Paris, Amazon, AliExpress, Easy, Jumbo, Ripley) via the Bright Data MCP tools. Use when asked to find, search, or compare prices for a product on any of these sites, or to check what a product costs/where to buy it."
metadata: {"nanobot":{"emoji":"🛍️","requires":{"env":["BRIGHTDATA_API_TOKEN"]}}}
---

# Marketplace Search (Bright Data)

All of these sites either serve JS-rendered pages with no data in the raw HTML, or actively block plain HTTP requests (confirmed for MercadoLibre's public API too — it 403s even in production). Use the Bright Data MCP tools instead of `curl`/`web_fetch`.

## Start here: cross-site price comparison

For "what's the best price for X" / "where can I buy X" / any open-ended product search, use `search_engine` (Google SERP via Bright Data) first — it's usually faster and more complete than scraping any single retailer directly:

- General: `search_engine` with query `QUERY precio Chile` or `QUERY comprar` to surface retailer listings with prices in the snippets.
- Google Shopping product data: `web_data_google_shopping` gives structured data (price, seller, rating) but needs an actual Google Shopping product URL — get that URL from a `search_engine` result first, don't guess it.

**Falabella, Paris, and (likely) Ripley: go straight to `search_engine`, don't bother with `scrape_as_markdown` on these three.** Confirmed in production: `scrape_as_markdown` only returns the page's navigation/header shell for these sites — the actual product grid is loaded client-side after page load and never appears in the scraped content. Trying the direct URL first just wastes a call.

## Site-specific search (MercadoLibre, Amazon, AliExpress, and unconfirmed sites)

For sites not listed above, `scrape_as_markdown` does return usable product content:

1. Build the search URL for the target site (see below).
2. Call `scrape_as_markdown` on that URL.
3. Read product name, price, and link out of the returned markdown and summarize the top results.
4. If the result only shows navigation/header content with no products (same symptom as Falabella/Paris), fall back to `search_engine` instead of retrying.

Known search URL patterns:

- **MercadoLibre**: `https://listado.mercadolibre.cl/QUERY`
- **Amazon**: for a specific known product page, use `web_data_amazon_product` on the product URL for clean structured data (price/title/rating). For a general search, `scrape_as_markdown` on `https://www.amazon.com/s?k=QUERY`.
- **AliExpress**: `scrape_as_markdown` on `https://www.aliexpress.com/wholesale?SearchText=QUERY`
- **Easy, Jumbo**: URL pattern not yet confirmed — try `search_engine` instead of guessing a `?query=`-style parameter blindly.

Always URL-encode the QUERY.

## Presenting results: use a card per product

The chat UI renders a special block as a visual product card (image, title, price, and a "Ver producto" link) instead of plain text. For every product you present, emit one of these blocks — one per product, don't combine multiple products into one block:

```
:::card
image: https://example.com/product-image.jpg
title: Producto XYZ 128GB
price: $19.990
site: Falabella
url: https://example.com/product/123
:::
```

- `image` and `url` should be real, absolute `http(s)://` URLs pulled from the scrape/search result — never invent or guess one.
- `price`, `site` are optional but include them when available.
- If no product image is available, omit the `image` line rather than guessing a URL.
- You can still write normal prose before/after the cards (e.g. "Here's what I found on Falabella:"), just don't put prose *inside* the `:::card` block — only the `key: value` lines shown above.
