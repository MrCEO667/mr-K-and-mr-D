# DECISIONS

Locked decisions and their reasons. If you change one, edit it here in the same
commit as the code.

## Locked

| # | Decision | Reason |
|---|---|---|
| 1 | Score durability + saturation over time-series. Do **not** train on news. | News has no failure labels and covers only winners. A news model learns editorial selection. |
| 2 | News enters as LLM-citable evidence, never as training data. | Useful context, useless supervision. |
| 3 | Margin as a **multiple** (`8.5x`), not a percent. | Operator preference, confirmed. |
| 4 | Local-first execution on the PC, not GitHub Actions. | Composer needs Ollama + GPU; Actions has neither. CI only on Actions. |
| 5 | Local `qwen3:8b` via Ollama. No paid LLM API. | $0 constraint. Already proven in the predecessor project. |
| 6 | tikwm for TikTok. No official TikTok/Instagram API. | Research API needs academic affiliation; Creative Center is 404; IG Graph reads own accounts only. tikwm worked in Retrend. |
| 7 | Snapshots are append-only, written from M1. | Retrend stored one perf snapshot and could compute no velocity. Root cause of its dead feedback loop. |
| 8 | Backtest reports model **vs naive momentum vs random**, always. | Without a baseline, an ML number is decoration. |
| 9 | Discovery is seeded + harvested, not pure sweep. | Retrend: 250/395 candidates died on relevance, only 78 on virality. |
| 10 | Human Watch/Dismiss is the relevance training set. | Retrend's LLM scoring was abandoned; human review won. Make that the design, not the fallback. |
| 11 | Saturation is counted, never predicted. | It is directly measurable. Modeling it would add error for nothing. |
| 12 | No trade/token/ticker recommendations. | Out of scope by design. The tool surfaces demand signals; it does not tell anyone where to put money. |
| 13 | Telegram only. No web UI in v1. | Fastest path to a usable tool; long polling needs no public IP. |
| 14 | Public repo. | Unlimited Actions minutes for CI. No secrets in the repo — `.env` is gitignored. |
| 15 | English docs, Python + SQLite. | Confirmed. |

## Open — defaults chosen, implement the default, revisit later

| # | Question | Default | Revisit when |
|---|---|---|---|
| A | Payment rails | Not enforced. Rails appear on the Requirements line only. `config/payment_rails.yaml: enforce: false` | You know your real list — then flip `enforce: true` |
| B | Minimum acceptable first sale | No floor; `min_margin_multiple: 3.0` does the filtering | After ~20 cards, if low-ticket noise dominates |
| C | Label threshold (0.6 of window peak) | 0.6 | Sweep during M5, record the winner in `docs/MODEL.md` |
| D | Composite weights | 0.45 / 0.35 / 0.20 | After the relevance model activates at 100 decisions |
| E | Workload split | None. Contracts + issue-claiming instead. | If merge conflicts start costing real time |
| F | Handling of failed attempts as negative data | Deferred. Saturation is the honest partial mitigation. | If `outcomes` shows the estimates are systematically optimistic |

## Corrections made during planning

Recorded so nobody re-proposes them.

- **GitHub Actions as the scheduler** — proposed, then rejected. The composer
  requires a local GPU and model. See decision 4.
- **TikTok/Instagram called unavailable** — corrected. The predecessor proved
  tikwm.com works without auth across three endpoints. See decision 6.
- **"Model reads business news to find fast-growing companies"** — the original
  framing. Replaced by decisions 1 and 2, because it has no labels and no
  failure data.
- **"100% successful, money on day one"** — replaced by the achievable version:
  five candidates a week testable for under $20 and 3 hours. The tool compresses
  search; it does not guarantee revenue.
