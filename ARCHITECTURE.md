# ARCHITECTURE

## Deployment: local-first

This is a correction to an earlier plan that put the scheduler on GitHub
Actions. It does not work, for a concrete reason:

**The composer requires Ollama with `qwen3:8b` on the RTX 2080.** GitHub Actions
runners have no GPU and no model. Splitting collection to CI and composition to
the PC would mean shipping the SQLite file between them on every run. Not worth
the complexity for a single-operator tool.

So: **everything runs on DESKTOP-9SJAUG4.** Windows Task Scheduler drives it.
GitHub Actions is used only for CI (lint + tests on push). The Telegram bot uses
long polling, so no public IP, no tunnel, no webhook.

Cost: $0. Tradeoff: nothing runs when the PC is off. Accepted for v1.

## Pipeline

```
                 ┌──────────────┐
  seeds.yaml ───►│  DISCOVER    │◄─── harvest (reddit/PH/GH/YT/tikwm n-grams)
                 └──────┬───────┘
                        │  terms
                        ▼
   ┌───────────────────────────────────────────┐
   │  COLLECT   (Collector ABC, one per source) │
   │  trends · youtube · reddit · ph · gh · tikwm│
   └──────┬─────────────────────────┬───────────┘
          │ demand                  │ supply
          ▼                         ▼
  signal_snapshots          saturation_snapshots     ◄── APPEND ONLY
          │                         │
          └──────────┬──────────────┘
                     ▼
              ┌─────────────┐
              │   SCORE     │  durability(ML) · saturation(count)
              │             │  feasibility(rules) · relevance(ML)
              └──────┬──────┘
                     ▼  survivors only
              ┌─────────────┐
              │  COMPOSE    │  local qwen3:8b → Opportunity JSON
              └──────┬──────┘
                     ▼
              ┌─────────────┐
              │  DELIVER    │  Telegram cards + buttons
              └──────┬──────┘
                     ▼
              decisions ──► relevance training set
              outcomes  ──► estimate calibration
```

## Layout

```
radar/
  config.py         load + validate YAML, env override
  db.py             connection, migrations, run_id
  discover.py       seeds + n-gram harvest
  collectors/
    base.py         Collector ABC  (contract: schema/contracts.md)
    trends.py  youtube.py  reddit.py  producthunt.py  github.py  tikwm.py
  saturation.py     supply counters
  features.py       window -> feature vector  (shared by train + score)
  model/
    dataset.py      history -> labelled windows
    train.py        fit + temporal split
    backtest.py     precision@10 vs momentum vs random
    predict.py      load + score
  feasibility.py    rule gate
  relevance.py      personal model, cold-start rules
  compose.py        Ollama client, strict JSON, validation
  bot/
    app.py  cards.py  handlers.py
  report.py         CLI inspection
  runner.py         orchestration, --once / --dry-run
```

## Rules that prevent collisions

Three people, no formal ownership split. What keeps you out of each other's way:

1. **One responsibility per file.** Never edit a file you did not open the
   branch for.
2. **Contracts in `schema/contracts.md` are frozen.** Changing one is its own
   commit, made before any code depends on the change.
3. **SQLite is the only shared state.** No module calls another module's
   functions across layer boundaries — collect, score, compose and deliver
   communicate through tables.
4. **Claim work as a GitHub issue before starting.** Cheaper than a merge
   conflict.
5. **`features.py` is shared by training and scoring.** Never fork it. A feature
   computed differently at train and predict time is a silent, unfindable bug.

## Failure policy

| Failure | Behaviour |
|---|---|
| One source down | `SourceUnavailable`, log, `source_health` row, continue with reduced breadth |
| All sources down | Run aborts, alert to Telegram, no partial scores written |
| Ollama unreachable | Score and store, skip composition, mark `composed=false`, retry next run |
| LLM JSON unparseable | Two retries, then skip that opportunity |
| Telegram down | Cards queue in DB, flush on next successful connect |
| Model file missing | Fall back to naive momentum, log loudly, still deliver cards |

The pattern: **degrade, never crash, never silently substitute.** Every
degradation is visible in the logs and on `/why`.

## Scheduling

| Job | Cadence | Scope |
|---|---|---|
| watchlist refresh | hourly | starred terms only, cheap sources |
| broad sweep | daily 03:00 | full discovery + all sources |
| saturation | daily | all active terms |
| retrain | weekly | durability + relevance |
| backtest | weekly | after retrain, writes report |

Hourly across every source would exhaust quotas within a day. The split is what
makes hourly affordable.
