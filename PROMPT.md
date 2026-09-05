# Build Prompt — NicheRadar

> Paste this into Claude Code at the repo root. Read `docs/SPEC.md`,
> `docs/ARCHITECTURE.md` and `schema/contracts.md` before writing any code.
> Build in the milestone order given. Do not skip ahead.

---

## What we are building

A local-first opportunity scanner. It watches public trend data, scores rising
niches on **durability**, **saturation** and **feasibility**, and pushes the
survivors to a Telegram bot as concrete, costed playbooks.

The user is the operator, not a customer. Two developers, a $0–100 budget per
opportunity, and a hard requirement that any suggestion be testable for under
$20 and under 3 hours.

**The system does not predict success. It compresses search.** It replaces two
weeks of manual scanning with twenty minutes of reviewing evidenced candidates.
Write every docstring, log line and card with that framing. Never emit language
implying a suggestion is guaranteed, safe, or certain to make money.

## Hard constraints

- **Python 3.11+, SQLite, stdlib-first.** No cloud DB, no paid API, no key that
  costs money. If a feature needs paid data, stub it and log a warning.
- **All LLM work is local** via Ollama (`qwen3:8b`, `http://localhost:11434`).
  8 GB VRAM ceiling. No Anthropic/OpenAI API calls in the runtime path.
- **Runs on Windows** (RTX 2080, 16 GB RAM) under Task Scheduler. Paths must be
  `pathlib`, never hardcoded POSIX.
- **Every module is independently runnable**: `python -m radar.<module> --once`.
- **No module imports another module's internals.** Modules talk through the
  JSON contracts in `schema/contracts.md` and through SQLite. Those contracts
  are frozen — if one must change, change it in `schema/contracts.md` first, in
  its own commit, and say so in the commit message.

## The rule that matters most

**Write a snapshot row on the very first run, before anything else works.**

The predecessor project (Retrend) collected 395 candidates and then could not
compute a single velocity metric, because it stored one performance snapshot
instead of a series. Velocity needs two points minimum. Durability needs many.

So: `signal_snapshots` is append-only, is written by Milestone 1, and is never
overwritten or garbage-collected. If a run produces no scores, no cards and no
model, but does write snapshots, that run succeeded. Treat any code path that
mutates or deletes a snapshot as a bug.

## Build order

Each milestone must be runnable, tested and committed before the next starts.

### M0 — Skeleton
Config loader (`config/config.yaml`, YAML + env override), SQLite init from
`schema/schema.sql`, structured logging with a `run_id` (UUID) threaded through
every write, `--once` and `--dry-run` flags on every entry point. Ships with
`pytest` passing on an empty suite.

### M1 — One collector, end to end
Google Trends only, via `pytrends`. Prove the loop: seed terms in →
`signal_snapshots` rows out. Add rate limiting with exponential backoff, and a
`SourceUnavailable` exception that degrades the run instead of killing it.
**Done when:** two runs an hour apart produce two distinct snapshot rows per
term and `radar.report --term X` prints a time series.

### M2 — The rest of the collectors
YouTube Data API v3, Reddit (public JSON + OAuth fallback), Product Hunt,
GitHub Trending, tikwm. All behind the `Collector` ABC in
`schema/contracts.md`. See `docs/DATA_SOURCES.md` for per-source quotas,
auth reality and known failure modes — several of these returned zero rows in
the predecessor project and the reasons are documented there.

Every collector must: declare its quota, respect it, return partial results on
failure, and record its own health in a `source_health` row. One dead source
must never fail a run.

### M3 — Discovery
Two paths, both required.
- **Seeded:** `config/seeds.yaml` categories expanded into query terms.
- **Harvested:** n-gram extraction from Reddit titles, PH launches, GitHub repo
  descriptions, YouTube titles and tikwm captions/hashtags. Normalize, dedupe
  against `terms`, filter stopwords and brand names.

Harvest is noisy by design. Cap new terms per run (default 50) and mark
`origin` so the relevance model can learn which harvest sources are worth
anything.

### M4 — Saturation
Supply-side counters: result counts for the term on Etsy, Amazon, Fiverr,
Gumroad, the Shopify app store, GitHub and Product Hunt. This is **counted, not
predicted.** Store raw counts in `saturation_snapshots`; derive the LOW/MED/HIGH
label at scoring time so the thresholds stay tunable.

The signal that matters is not the absolute count but **supply growth versus
demand growth**. Demand rising while supply is flat is the window. Both rising
together is a race you have already lost.

### M5 — Durability model
The unlock: Google Trends returns **five years of history per term on the first
call**. You do not wait months to get a training set. Pull history for every
seed and harvested term, cut it into rolling windows, and auto-label.

- **Features** from a 14-day window: slope, acceleration, volatility,
  weekday seasonality, peak-relative position, cross-source breadth (how many
  independent sources see it), cross-source correlation, days since first seen.
- **Labels:** is the mean of days 30–37 / 60–67 / 90–97 still above X% of the
  window peak. Auto-derived, no human labelling.
- **Model:** gradient boosting (`lightgbm` or sklearn's `HistGradientBoosting`).
  Small, fast, interpretable, trains on CPU in under a minute.
- **Splits are temporal, never random.** Random splits leak the future into the
  training set and will hand you a beautiful, worthless AUC.

**Backtest is mandatory and it must include baselines.** Freeze at a past date,
rank, compare against what actually happened. Report precision@10 for the model
against: random ordering, and naive momentum (rank by 7-day slope alone).

> If the model does not beat naive momentum, `docs/MODEL.md` must say so in
> plain language and the pipeline must fall back to momentum. Shipping a model
> that loses to a one-line heuristic and not saying so is the single worst
> outcome of this project. Report the honest number.

### M6 — Feasibility gate
Rule-based, no ML. Reads `config/capabilities.yaml` and `config/config.yaml`.
Rejects anything above the budget cap, outside the team's build capability,
requiring inventory or physical manufacturing, or below the minimum margin
multiple. Emits pass/fail **plus reasons** — the reasons go on the card, and
rejected items stay in the DB for the relevance model to learn from.

### M7 — Composer (LLM)
Local `qwen3:8b`. Input: term + metric bundle + evidence links. Output: strict
JSON matching the `Opportunity` contract. Retry twice on parse failure, then
mark `composed=false` and move on — never let a malformed generation block a run.

**Separation of concerns, enforced in code:**
- **Measured numbers** (trend deltas, post counts, view sums, supply counts)
  are read from SQLite and template-injected into the card. The LLM never
  produces them and never sees a slot where it could.
- **Estimated numbers** (price point, cost per sale, days to first dollar) are
  LLM output, always stored with a confidence value, always rendered on the
  card with an explicit estimate marker.

Post-generation validation: if any measured figure appears in LLM prose and
disagrees with the DB, discard the generation and retry. Assert this in tests.

### M8 — Telegram bot
Long polling (`python-telegram-bot`), no webhook, no public IP.

Card format:

```
🔥 AI voice-clone for podcast intros                    [ONLINE]
Durability 0.81 (60d sustained, rising) · Saturation LOW
Margin ~8.5x (est.) · Setup $12 · First sale est. 1–3 days

Evidence: YT +340% 30d · Reddit 47 posts/wk (was 6) · PH 2 launches, both weak
Needs: domain, Fiverr account, 3h setup
Play: Fiverr gig + one-page site.
  1. …  2. …  3. …

[👁 Watch]  [✕ Dismiss]  [📄 Full breakdown]
```

Commands: `/scan`, `/watchlist`, `/filter online|offline|margin>Nx`,
`/why <id>` (full evidence trail with source links), `/outcome <id>` (log what
actually happened after you tested it).

**Suppression:** an opportunity alerts once. It re-alerts only if its composite
score moves more than `alert.rescore_delta` (default 0.15) or its saturation
label changes. Dismissed items never re-alert unless the reason was `too_slow`.

### M9 — Relevance model
`Dismiss` opens a reason keyboard: `saturated`, `cant_build`, `cant_collect`,
`low_margin`, `too_slow`, `not_interested`. Every decision is a labeled row.

Below 100 labels: rule-based scoring only, and say so in `/why`. At 100+: train
a small classifier on term embeddings plus score features, and blend it into
the composite behind a config weight.

This is the part the predecessor got right by accident — its LLM scoring was
abandoned and human review won. Here the human review *is* the training data.

### M10 — Outcomes
`/outcome <id>` records tested yes/no, spent, revenue, notes. This closes the
loop the predecessor never closed. Surface `hit rate` and `median actual margin
vs estimated margin` in a `/stats` command. Expect the estimates to be wrong at
first; the point is to measure how wrong.

## What not to build

- **No trade or purchase recommendations.** The tool surfaces demand signals and
  supply counts. It does not tell anyone to buy an asset, and it does not score
  tokens or tickers. If a harvested term is a financial instrument, drop it at
  the discovery stage — there is a stopword list for this, keep it current.
- No web frontend. Telegram is the whole UI.
- No accounts, auth or multi-tenancy. Single operator.
- No paid data source, ever, without an explicit decision recorded in
  `docs/DECISIONS.md`.
- No scraping behind a login, and no circumventing a paywall or rate limit.
  Respect `robots.txt`, identify the user agent honestly, back off on 429.
- No writing to `signal_snapshots` outside the collector layer.

## Definition of done for v1

1. Two scheduled runs per day minimum, producing snapshots without manual help.
2. `radar.backtest` prints precision@10 for model, momentum and random, on a
   temporal split, with the honest verdict written into `docs/MODEL.md`.
3. Telegram delivers at least 5 cards a week that pass the feasibility gate.
4. Every card's numbers are traceable: `/why <id>` shows the source rows.
5. At least one opportunity has an `outcomes` row, win or lose.

## Working style

- Commit per milestone, tests included. No milestone is done without tests.
- If a decision was left open, it is listed in `docs/DECISIONS.md` under **Open**
  with a default already chosen. Implement the default, do not stall, do not
  ask — record any deviation in that file.
- When something in this prompt turns out to be wrong on contact with reality
  (a source is dead, a quota is lower than documented), fix the code and update
  the doc in the same commit. A stale doc here is worse than no doc.
