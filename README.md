# NicheRadar

A local opportunity scanner. It watches public trend data, scores rising niches
on **durability**, **saturation** and **feasibility**, and pushes the survivors
to Telegram as costed, evidenced playbooks.

Built by Mr K and Mr D. Two operators in one Telegram group, $0 running cost,
everything local.

## What it does and doesn't do

It compresses search. Two weeks of manual scanning across Trends, Reddit,
Product Hunt, TikTok and app stores becomes twenty minutes of reviewing
candidates that already carry their evidence.

It does **not** predict that a business will succeed. There is no ground truth
for that — public data records winners and is silent on the thousands of
identical attempts that made nothing. So the system only claims things it can
defend:

1. This demand signal is **rising** — measured
2. It is **likely to still be there in 30–90 days** — modeled, backtested
3. **Few people are currently selling into it** — counted

You decide. The tool finds and evidences; it doesn't promise.

**Target:** five candidates a week that can be validated for under $20 and
under three hours.

## Read in this order

| File | What it is |
|---|---|
| **`PROMPT.md`** | The build prompt. Paste into Claude Code. |
| `docs/SPEC.md` | What we're building, metric definitions, limitations |
| `docs/ARCHITECTURE.md` | Pipeline, module layout, failure policy |
| `docs/DATA_SOURCES.md` | Every source, its real quota, its known failure mode |
| `docs/MODEL.md` | Durability model, labelling, backtest, honesty clause |
| `docs/DECISIONS.md` | What's locked, what's open, what was rejected and why |
| `docs/WORKFLOW.md` | How three people share one repo without collisions |
| `schema/schema.sql` | Database |
| `schema/contracts.md` | **Frozen** interfaces between modules |

## Setup

```bash
git clone <your-repo-url> && cd mr-k-and-mr-d
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt

cp .env.example .env                        # fill in the keys
cp config/config.example.yaml config/config.yaml

# Telegram: @BotFather /newbot, then create a supergroup with both operators
# and the bot in it, send /hello from each, and run:
python scripts/telegram_setup.py            # prints the chat ID + operator IDs

ollama pull qwen3:8b                        # ~5GB, fits the 2080's 8GB
python -c "import sqlite3;sqlite3.connect('data/radar.db').executescript(open('schema/schema.sql').read())"
```

Then open Claude Code in the repo root and give it `PROMPT.md`.

Keys you need, all free: YouTube Data API v3, a Reddit script app, a Product
Hunt developer token, a Telegram bot from @BotFather. Google Trends and tikwm
need nothing.

## Runs on

Windows, RTX 2080 (8GB), 16GB RAM, scheduled via Task Scheduler. GitHub Actions
runs CI only — the composer needs a local GPU and model, which hosted runners
don't have.

## Lessons carried over from Retrend

The predecessor project is why several decisions here look the way they do.

- **Store time-series from run one.** Retrend collected 395 candidates and could
  compute no velocity metric, because it stored a single performance snapshot.
  `signal_snapshots` here is append-only and written by Milestone 1.
- **Relevance is the bottleneck, not virality.** Retrend's math filter rejected
  78 candidates; its relevance screen rejected 250. Broad trending feeds surface
  real virality that is useless to you. Seeded keyword discovery is the fix.
- **Human review beat automated scoring.** Only 2 of 395 candidates ever carried
  an LLM score; everything shipped went through human approval. So here, human
  Watch/Dismiss decisions *are* the training set, by design.
- **Every official API was a dead end and one scraper carried the project.**
  Provision keys before writing collectors, and make every source degradable.

## Status

M0 landed: config loader, SQLite init, structured logging with a run_id, and
`--once` / `--dry-run` on the entry point.

```bash
python -m radar --once --dry-run     # proves the wiring, writes nothing
```

M1 landed: Google Trends collector, anchored and rate limited, writing
append-only `signal_snapshots`.

```bash
python -m radar --once                       # collect
python -m radar.report --term "faceless youtube"
```

First live sweep: 1932 snapshots across 21 terms, 92 days of history each.

M2 and M4 are in as far as the credentials allow:

| Source | Signal | Status |
|---|---|---|
| Google Trends | `interest` | live, anchored |
| YouTube | `view_sum`, `video_count` | live |
| Hacker News | `post_count` | live |
| GitHub | `stars` + repo count | live |
| Gumroad | supply count | live |
| Reddit | `post_count`, `score_sum` | written, needs a script app |
| Product Hunt | `launch_count`, `vote_sum` | written, needs a developer token |
| tikwm | — | dead, see decision 19 |

```bash
python -m radar --once              # collect + count supply
python -m radar --once --harvest    # also mine new terms from subreddit RSS
python -m radar.report --term "faceless youtube"
```

M5 (durability model) is next.
