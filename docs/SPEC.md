# SPEC — NicheRadar v1

## Problem

Finding a niche worth testing takes days of manual scanning across Trends,
Reddit, Product Hunt, TikTok and app stores. Most of what surfaces is either
already saturated, unbuildable by a two-person team, or a spike that died before
you noticed it.

## What this is

A local opportunity scanner for one operator. It watches public signals, scores
candidates on three independent axes, and delivers evidenced playbooks to
Telegram.

## What this is not

**It does not predict success.** There is no ground truth for "this business
worked." News covers winners and is silent on the thousands of identical
attempts that made nothing. A model trained on business news learns what
journalists write about, not what causes revenue.

So the system never claims a niche will work. It claims three things it can
actually defend with data:

1. This demand signal is **rising** (measured)
2. It is **likely to still be there in 30–90 days** (modeled, backtested)
3. **Few people are currently selling into it** (counted)

The operator decides. The tool's value is search compression: two weeks of
scanning down to twenty minutes of review.

## Success criterion

Five candidates per week where the cheapest validation test costs **under $20
and under 3 hours**. Not "a guaranteed business" — a cheap, fast, evidenced
test.

## The atom

An **opportunity** = demand signal × monetization playbook × feasibility gate.

The same term can produce multiple opportunities (e.g. "AI voice clone" → a
Fiverr service, a Gumroad preset pack, a Shopify app). Each is scored separately
because their costs and margins differ.

## Scoring axes

### 1. Durability — modeled, 0.0–1.0

`P(signal still elevated at +30 / +60 / +90 days)`, given a 14-day window.

Trained on historical Google Trends and YouTube series with auto-derived labels.
Temporal splits only. Backtested against naive momentum. Details in
`docs/MODEL.md`.

This exists because the thing that kills a fast-money play is jumping onto a
spike that is already dead. A term at day 40 of a 45-day fad looks identical to
a term at day 40 of a two-year trend if you only look at the slope.

### 2. Saturation — counted, LOW / MED / HIGH

Supply-side result counts across Etsy, Amazon, Fiverr, Gumroad, Shopify apps,
GitHub, Product Hunt. Never predicted, always counted, always with the raw
number visible on `/why`.

The derived metric that matters is **supply growth vs demand growth**:

| Demand | Supply | Verdict |
|---|---|---|
| ↑ | flat | The window. This is what you are hunting. |
| ↑ | ↑ | Race already started. Only worth it with a real edge. |
| flat | ↑ | Saturating. Skip. |
| ↓ | any | Dead. Skip. |

### 3. Feasibility — rules, pass/fail + reasons

Hard gate, no ML. Reads `config/capabilities.yaml` and `config/config.yaml`.

- Setup cost ≤ `budget.max_setup_usd` (default 100)
- Buildable from the declared capability list
- No inventory, no physical manufacturing
- Margin multiple ≥ `budget.min_margin_multiple` (default 3.0)
- Estimated time to first dollar ≤ `budget.max_ttfd_days` (default 7)

Failures are stored with reasons, not deleted. The relevance model learns from
them.

### 4. Relevance — personal, learned

Trained on the operator's own Watch/Dismiss decisions with reason codes. Cold
start is rule-based; the model activates at 100 labelled decisions.

### Composite

Weighted blend, weights in `config/config.yaml` so they are tunable without a
code change. Feasibility is a **gate**, not a weight — it multiplies to zero.

```
composite = w_d·durability + w_s·(1 − saturation) + w_r·relevance
            × feasibility_pass
```

## Metric definitions

Written out because ambiguity here produces two incompatible implementations.

| Metric | Definition | Notes |
|---|---|---|
| `margin_multiple` | `price / cost_per_sale` | Rendered as `8.5x`. Multiple, not percent — confirmed. |
| `contribution_margin_pct` | `(price − cost_per_sale) / price` | Stored for sanity checks, not shown on the card. |
| `setup_cost_usd` | One-time cost before first sale | Domain, tools, ad test. Gated against budget. |
| `cost_per_sale_usd` | Marginal cost of one unit | Platform fee, delivery, compute. |
| `ttfd_days` | Estimated days from start to first dollar | LLM estimate. Always shown as an estimate. |
| `durability_N` | P(elevated at +N days) | Model output, 0–1. |
| `saturation` | LOW / MED / HIGH | Derived at score time from raw counts. |
| `mode` | `online` / `offline` / `hybrid` | Filterable in the bot. |

**Everything in the "estimated" column is a guess until `outcomes` says
otherwise.** Price, cost and TTFD are LLM estimates from comparable offers.
M10 exists to measure how wrong they are.

## Delivery

Telegram bot, long polling, local. Card format and commands in `PROMPT.md` M8.
One shared supergroup, both operators, allowlisted by user ID. Decisions carry
the actor who made them — see decisions 16 and 17.

Cadence: hourly on the starred watchlist, daily broad discovery sweep.
Suppression: alert once, re-alert only on a score move of `rescore_delta`.

## Out of scope for v1

- Trade, token or ticker recommendations — excluded by design, see PROMPT.md
- Web UI, accounts, user management beyond the two-operator allowlist
- Paid data sources
- Physical/inventory business models
- Auto-execution of anything. The tool suggests; the human acts.

## Known limitations to state plainly

1. **Survivorship bias is unsolved.** Failures are invisible in public data.
   Saturation counting is the partial mitigation — it is measurable and honest,
   where "will this succeed" is not.
2. **tikwm is an unofficial mirror.** It can vanish without notice. It degrades
   the score; it must never break a run.
3. **The durability model may not beat momentum.** If it does not, we say so
   and ship momentum.
4. **Estimated economics are unvalidated** until the outcomes table has data.
