# WORKFLOW

Three people, no formal ownership split, one repo. This is what prevents merge
pain.

## Rules

1. **Claim before you build.** Open a GitHub issue titled `M<n>: <module>` and
   assign yourself. Thirty seconds; saves an hour.
2. **One responsibility per file.** Don't edit a file you didn't claim.
3. **Contracts are frozen.** Changing `schema/contracts.md` is its own commit,
   made *before* dependent code, with `contract:` in the message.
4. **Branch per milestone:** `m3-discovery`, `m5-durability-model`.
5. **No milestone merges without tests.**
6. **SQLite is the only shared state.** Layers talk through tables, never
   through each other's functions.

## Milestone order

Sequential — each depends on the last. Full detail in `PROMPT.md`.

```
M0 skeleton ─► M1 trends collector ─► M2 all collectors ─► M3 discovery
                     │                                          │
                     └──────────► M4 saturation ◄───────────────┘
                                       │
                     M5 durability model + backtest
                                       │
                     M6 feasibility ─► M7 composer ─► M8 telegram bot
                                                            │
                                       M9 relevance ◄───────┘
                                       M10 outcomes
```

**M1 is the one that must not be rushed.** If snapshots aren't landing correctly
from day one, M5 has nothing to train on and the whole thing is a scraper with
a chat interface.

## Parallelizable

Once M0 lands: M2 collectors (one person per source), M4 saturation, and the M5
historical dataset builder can all proceed at once — Trends history is available
immediately and doesn't wait for live collection.

M7 and M8 can be built against fixture data before M5 finishes.

## Commits

```
M1: add google trends collector with backoff
contract: add source_breadth to FeatureVector
fix: tikwm cursor paging drops last page
docs: record M5 backtest result
```

## Definition of done, per milestone

- Runs via `python -m radar.<module> --once`
- Tests pass
- Degrades on failure instead of crashing
- Any doc it contradicts is updated in the same commit
