# DATA SOURCES

Ground truth for what actually works. Several entries below are corrections
learned the hard way in the predecessor project (Retrend), where every official
API turned out to be a dead end and the whole thing ran on one unofficial
scraper. Do not repeat that.

## Demand side

### Google Trends — `pytrends` — PRIMARY
No key, no cost. Unofficial client over the public endpoint.

**Why it is the backbone:** it returns **up to 5 years of history on the first
call**. That is what makes the durability model trainable on day one instead of
after six months of collection. Every other source gives you only "now".

- Rate limited aggressively. Exponential backoff, 1 req / 3–5 s, cache hard.
  4 s was not enough in practice; 8 s got a full sweep through with one batch
  lost to throttling.
- Values are *relative* (0–100, normalized within the request) — never compare
  across separate requests without a shared anchor term. Use a fixed anchor.
- 429s are routine. Treat as backoff, not as failure.

**Anchor sizing, learned the hard way.** The anchor must sit in the same volume
band as the terms. Measured against `weather`, every seed term in this project
returned **0 for a full quarter** — Trends reports integers 0–100 normalized
within the request, so a popular anchor rounds a niche term to nothing. That is
indistinguishable from "no demand" once stored. The collector now refuses a
flat-zero series instead of writing it, and the anchor is
`wordpress plugin` (~43 mean against seeds of 2–32).

Two consequences worth remembering:

1. **The anchor must not be a seed term.** Trends rejects a request containing
   the same keyword twice, and a term can only score 1.0 against itself. The
   first live run lost a batch of four terms to exactly this.
2. **Resolution is set by the largest term in the batch**, not by the anchor.
   Batches mixing a 32-mean term with a 2-mean term will always quantise the
   small one coarsely. Revisit if M5 finds the low-volume terms too noisy.

### YouTube Data API v3 — official, free
10,000 quota units/day. `search.list` costs 100 units, so ~90 searches/day.
Budget it: `videos.list` is 1 unit, so search once and batch-hydrate.

> Retrend implemented this fully and got **zero rows** because no API key was
> ever provisioned. The code was fine; the key was missing. Provision the key
> during M2 and assert on startup that it is present and valid.

### Reddit — public JSON / OAuth
`/r/<sub>/new.json` returns 403 without credentials — this bit Retrend hard.
Create a script-type app (free, 2 minutes) and use OAuth from the start. 100
req/min with credentials, effectively nothing without them.

Subreddits are seeds, not a fixed list — put them in `config/seeds.yaml`.

### tikwm.com — unofficial TikTok mirror
Proven working in Retrend across three endpoints: `/api/feed/list` (trending,
region-hinted), `/api/feed/search` (keyword, cursor paging), `/api/user/posts`.
No auth, no key.

**Reuse the Retrend client code.** It handles cursor paging and region hints
already.

Two known truths from that project:
1. It is a **single point of failure** with no fallback. It must degrade the
   score, never break the run.
2. **Generic trending feeds are the wrong source for a niche.** Retrend's
   worldwide feed was full of genuinely viral content that was worthless for its
   audience — the relevance screen killed 250 of 395 candidates while the math
   filter killed only 78. Keyword search with seeded terms is what worked. Use
   `/api/feed/search` as the primary path and `/api/feed/list` only for harvest.

TikTok's official Research API needs academic affiliation. Creative Center went
404. Instagram's Graph API only reads accounts you own. Neither is available.

### Product Hunt — GraphQL, free tier
Developer token, generous limits. Good for both demand (what launched, how it
did) and supply (is someone already doing this).

### GitHub Trending — scrape
No official API for trending. Scrape the HTML, or use one of the community
mirrors. Best leading indicator for developer-tool niches.

### Hacker News — Algolia API, free, no key
Unlimited practical use. Good for harvest and for "Show HN" launch signals.

## Supply side (saturation)

Result counts, not content. Cheap, and the honest half of the system.

| Source | Method | Signal |
|---|---|---|
| Etsy | search result count | POD / digital / craft supply |
| Amazon | result count | physical supply (mostly a red flag for us) |
| Fiverr | gig count for the term | service supply |
| Gumroad | search count | digital product supply |
| Shopify app store | result count | app supply |
| GitHub | repo search count | OSS supply, is it commoditized |
| Product Hunt | launches matching | how many already tried |

Store **raw counts with timestamps**. The derived LOW/MED/HIGH label is computed
at score time so thresholds stay tunable without re-collecting.

## Excluded

| Source | Why |
|---|---|
| TikTok Research API | Academic affiliation required |
| TikTok Creative Center | 404, dead |
| Instagram Graph API | Own accounts only |
| Crunchbase | Paid |
| SEMrush / Ahrefs | Paid |
| Anything behind a login | Policy — no credential scraping |
| Token / ticker price feeds | Out of scope by design, see PROMPT.md |

## Rules for every collector

1. Declare quota in the class. Enforce it. Never discover a limit by hitting it.
2. Honest `User-Agent` identifying the project. Respect `robots.txt`.
3. Exponential backoff on 429/503. Never hammer.
4. Cache aggressively — same term, same day, same source = one call.
5. Return partial results on partial failure.
6. Write a `source_health` row every run: ok / degraded / down, with latency and
   error count. When a card looks wrong, this is the first place to look.
