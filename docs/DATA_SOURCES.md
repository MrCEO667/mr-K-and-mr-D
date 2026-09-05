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
  At the documented 4 s, two live sweeps each lost exactly one batch of four
  terms to throttling and completed the rest. Wider spacing is untested.
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

### YouTube Data API v3 — official, free — **LIVE**
10,000 quota units/day. `search.list` costs 100 units, so ~90 searches/day.
Budget it: `videos.list` is 1 unit, so search once and batch-hydrate.

Measured on the first live sweep: **2,505 units for 25 terms** (25 searches at
100, plus 5 hydrate calls covering 250 video ids at 50 per call). That is three
sweeps a day, and a fourth does not fit. Adding terms costs 100 units each, so
the seed list and the sweep cadence trade against each other directly.

Two metrics, unequal in trustworthiness:

- `view_sum` — summed views of the top ten videos in the window. Counted, real.
- `video_count` — `totalResults` from search, which is an **estimate** and
  routinely inflated ("ai voice clone" reports 23,581). Its movement is
  informative; its absolute value is not a count of anything. Never render it
  on a card as one.

Since tikwm died (decision 19), this is the **only video demand source**.

> Retrend implemented this fully and got **zero rows** because no API key was
> ever provisioned. The code was fine; the key was missing. Provision the key
> during M2 and assert on startup that it is present and valid.

### Reddit — public JSON / OAuth
`/r/<sub>/new.json` returns 403 without credentials — this bit Retrend hard.
Create a script-type app (free, 2 minutes) and use OAuth from the start. 100
req/min with credentials, effectively nothing without them.

Subreddits are seeds, not a fixed list — put them in `config/seeds.yaml`.

### tikwm.com — unofficial TikTok mirror — **SEARCH IS DEAD**

`/api/feed/search`, the keyword endpoint this project was going to depend on,
returns **HTTP 403** as of 2026-09-05. Verified across GET and POST, on both
`tikwm.com` and `www.tikwm.com`, with our honest User-Agent. Only
`/api/feed/list` still answers, and only with a `region` parameter.

That leaves no usable TikTok demand signal:

- `/api/feed/search` — 403. This was the primary path.
- `/api/feed/list` — works, but it is the generic worldwide trending feed, and
  this project already knows that is the wrong source. Retrend's relevance
  screen killed 250 of 395 candidates fed from exactly this kind of feed while
  the math filter killed only 78.

So tikwm is **disabled**, not degraded: a collector built on the trending feed
would produce rows that look like data and mean nothing. Re-enable only if the
search endpoint comes back. The prior note to reuse Retrend's client stands if
it does.

TikTok's official Research API needs academic affiliation. Creative Center went
404. Instagram's Graph API only reads accounts you own. Neither is available,
which now leaves **YouTube as the only video demand source** — one more reason
the YouTube key matters.

### Product Hunt — GraphQL, free tier
Developer token, generous limits. Good for both demand (what launched, how it
did) and supply (is someone already doing this).

### GitHub — repo search API, no key
`/search/repositories` works unauthenticated at 10 req/min, which is enough and
avoids scraping entirely. The demand metric is `stars`: the summed stars of the
ten best-matching repos. The repo *count* is a supply signal and belongs to
saturation, not here.

Trending-page scraping is still the better harvest source for discovery (M3);
the search API covers per-term demand.

### Hacker News — Algolia API, free, no key
Unlimited practical use. Good for harvest and for "Show HN" launch signals.

**Quote the phrase.** Algolia ORs the words otherwise: unquoted
`ai voice clone` returns 219 stories matching almost anything with "ai" in it,
where the quoted phrase returns 22 that are actually about the thing. Counting
the loose number would be counting noise and calling it demand.

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
