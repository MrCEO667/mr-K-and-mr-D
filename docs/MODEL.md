# MODEL

## What is modeled, and what is not

| Question | Approach | Why |
|---|---|---|
| Will this signal last? | **ML, supervised** | Labels are free and objective |
| How many are already selling? | **Counting** | Measurable, needs no model |
| Can we build it for <$100? | **Rules** | Deterministic, no training data |
| Do I personally want it? | **ML, from your clicks** | You are the only label source |
| Will this business succeed? | **NOT MODELED** | No ground truth exists |

That last row is the whole design. Public data contains winners and is silent on
failures. A model trained on business news learns editorial selection, not
causation. We refuse the question rather than answer it badly.

## Durability model

### Target
`P(signal still elevated at +30 / +60 / +90 days)` from a 14-day window.
Three binary heads, or one multi-output model.

### Why this target
It is the only forward-looking question here with **free, objective, abundant
labels**. Google Trends hands you five years of history per term, so the future
of any past window is already known. Tens of thousands of training rows, $0,
no annotation.

It also targets the actual failure mode. Jumping onto a spike that is already
dying is what kills a fast-money attempt, and slope alone cannot tell day 40 of
a 45-day fad from day 40 of a two-year trend.

### Label rule
For window ending at day `t`:

```
label_N = 1 if mean(interest[t+N : t+N+7]) >= 0.6 * max(interest[t-14 : t])
```

`0.6` is a config constant. Swept during M5; **0.6 stands**, and the reason
it was not moved is under "The label threshold sweep" below.

### Features (14-day window)
Slope (OLS), acceleration (2nd difference), volatility (σ/μ), weekday
seasonality amplitude, position relative to window peak, days since first
observation, cross-source breadth (count of independent sources seeing the
term), cross-source correlation, absolute magnitude bucket.

Cross-source breadth is the feature most likely to earn its keep: something
rising on Trends *and* Reddit *and* YouTube simultaneously behaves differently
from something rising on one.

### Algorithm
Gradient boosting — `lightgbm`, or sklearn `HistGradientBoostingClassifier` to
avoid a dependency. Trains in under a minute on CPU. Interpretable via feature
importance, which matters because you need to know *why* a card scored high.

Deep learning is the wrong tool here: a few thousand tabular rows, and the 2080
is better spent on the composer.

### Splitting — the rule that must not be broken
**Temporal splits only.** Train on windows ending before date `D`, test on
windows after. A random split puts a term's future in the training set and its
past in the test set. You will get a great AUC and a worthless model.

Also: **group by term.** The same term appearing in both splits leaks.
*Not done at M5, deliberately — 25 terms is too few to group and still
measure anything. Decision 24, and the caveat at the end of the backtest.*

## Backtest — mandatory

Freeze the data at a past date. Rank. Compare against what actually happened.

**Three baselines, always reported together:**

| Baseline | Why it is there |
|---|---|
| Random ordering | Floor |
| **Naive momentum** (7-day slope only) | The real bar |
| Durability model | Must beat momentum to justify existing |

Report **precision@10**: of the top 10 ranked terms, how many were still
elevated at +90 days.

### The honesty clause

> If the model does not beat naive momentum, this document must say so in plain
> language, and the pipeline must fall back to momentum.
>
> A model that loses to a one-line heuristic while being presented as ML is the
> worst possible outcome. It is worse than having no model, because it produces
> false confidence in every card it touches. Write the real number here.

**Result, 2026-09-06.** Run it yourself with `python scripts/train_model.py`;
the same block is written into `models/metadata.json` under `backtest`.

```
label threshold: 0.6      window: 14d      stride: 1 day
n_terms: 25   n_windows: 15,374   train: 9,999   test: 3,084 (+30d)
temporal split with embargo: train windows end <= 2026-03-29 minus 97d,
                             test windows end 2026-03-29 .. 2026-07-30

                precision@10                 precision@100        full test set
horizon   random  momentum  model      momentum      model        AUC     use_model
  +30d     0.59     1.00     1.00        0.85         0.97        0.75    yes
  +60d     0.47     0.90     0.90        0.81         0.81        0.65    NO
  +90d     0.36     0.30     0.90        0.64         0.68        0.66    yes
```

*(These numbers are the second run. The first, committed on the same day, was
produced with two broken features -- see "Two features that measure nothing"
below. The conclusion did not change; the +60d figures did.)*

### The verdict, in plain language

**At +60d — the horizon the composite actually uses — the model does not beat
naive momentum, so the pipeline falls back to momentum and the cards say
`momentum_fallback`.** It *ties* it: 0.90 against 0.90 at precision@10, and
0.81 against 0.81 at precision@100, with an AUC of 0.65. A tie is not a win,
and the gate requires a win, so the model is refused here. That is the honesty
clause firing, and it is the number that matters most, because
`scoring.durability_horizon` is 60.

The other two heads earn their place. +30d is the strongest result: it ties
momentum at precision@10 only because both saturate at 1.00, and pulls clearly
ahead once you look past the top ten (0.97 vs 0.85, AUC 0.75). +90d is the
opposite shape — a large win at the top of the ranking (0.90 vs 0.30) that
narrows to 0.68 vs 0.64 by rank 100. It is good at picking the few best and
close to a coin at ordering the rest, which is acceptable for a tool that only
ever shows the top handful.

**We did not retune `durability_horizon` from 60 to 30 to make the model look
useful.** That would be choosing the horizon on the strength of the test set,
which is the same sin as choosing the threshold on it. 60 was picked before any
of this was measured and it stays.

### Why the gate is not precision@10 alone

precision@10 is the metric PROMPT.md mandates and it is reported above
unchanged. It is also, at this data size, badly underpowered: ten rows drawn
from 25 terms, where the top ten at +30d came from a *single* term. Taken alone
it approved the wrong heads in both directions — it rejected +30d on a
saturated tie and, on the first run, approved a head on a lucky top ten.

So `BacktestResult.use_model` requires three things, all fixed before the
numbers were looked at:

1. Beat random ordering.
2. Beat momentum at precision@10. A *tie* is not a win, but it is not evidence
   either, and only then is precision@100 consulted as a tiebreak.
3. Clear an AUC floor of 0.55 on the full test set (`backtest.AUC_FLOOR`), so a
   lucky top ten cannot promote a head that cannot rank anything else.

A head that fails any of these is not loaded at all — see
`DurabilityModel.load()`. The fallback is enforced by the loader rather than by
remembering to pass the right argument, and a model directory with no recorded
backtest loads nothing, because unmeasured is not the same as good.

### The label threshold sweep

Decision C said to sweep 0.6 and record the winner. Swept over 0.4–0.8, the
+60d model precision@10 goes 1.00 / 0.50 / 0.80 / 1.00 / 0.70 — it is not
monotonic, and 0.7 is "best" only in the sense that it happens to win on this
test set. **Picking it for that reason would be selecting the label definition
on the test metric, so 0.6 stands** as pre-registered. Re-run the sweep when
the term count is large enough for the differences to mean something.

### Two features that measure nothing

`source_breadth` and `source_correlation` contribute **exactly zero** to every
head, and it took a code review to notice, because a dead feature looks the
same as a useless one in an importance table.

There were two separate causes.

The first was a bug. `features.build()` received each other source's *full*
history and `_correlation` truncated to the shorter series, so a 14-day window
was compared against the **first 14 days of a two-year series**. A source
rising in perfect lockstep with the window scored -1.0. Breadth had the same
root cause: it counted sources with any data ever for the term, so it was
constant across all ~600 windows of that term. Both are fixed -- `build()` now
slices the other sources to the window itself, so no caller can get it wrong.

The second cause is data, and it is not fixed. **Only Google Trends has
history:**

| source | days of history | rows |
|---|---|---|
| google_trends | 720 | 18,000 |
| youtube, hackernews, github, product_hunt | **1** | 25-150 each |

Trends backfills two years on demand; the others only ever report today, and
there is no historical endpoint to ask. So across all 15,374 training windows
`source_breadth` is **constant 1** and `source_correlation` is **constant
0.0**. They are not weak features, they are absent ones, and a constant column
cannot inform a split.

This matters more than an importance of zero suggests, because the section
above claims cross-source breadth is "the feature most likely to earn its
keep". **That claim is currently untestable.** The durability model is, today,
a Google-Trends-shape model with seven live features. The two cross-source
features will start carrying information once the daily sweeps have
accumulated a few months of their own history, and the backtest should be
re-run then -- not because the code changed, but because the data will finally
exist. Decision 31.

### What this backtest cannot tell you

**25 terms.** All 25 appear on both sides of the temporal split, so the split is
temporal but *not* grouped by term, which this document asks for two sections
above. With 25 terms, grouping would leave a handful on either side and measure
nothing at all; the deviation is deliberate and recorded as decision 24. It
means a term's own past informs its future here, and the true out-of-sample
numbers are somewhere below the ones printed above.

Treat this as a floor-clearing exercise, not a measurement of skill: it
establishes that the pipeline is honest and that one horizon does not deserve
the model. Re-run it once discovery has pushed the term count into the
hundreds, and expect the numbers to move.

## Relevance model (personal)

Trained on your own Watch/Dismiss decisions. Reason codes are the labels:
`saturated`, `cant_build`, `cant_collect`, `low_margin`, `too_slow`,
`not_interested`.

- **< 100 decisions:** rule-based only. `/why` states that the personal model
  is not active yet.
- **≥ 100 decisions:** small classifier over term embeddings (local, via Ollama
  or `sentence-transformers`) plus score features. Blended into the composite
  behind a config weight.

This is the piece the predecessor stumbled into: its automated LLM scoring was
abandoned in practice and human review won — only 2 of 395 candidates ever
carried an LLM score. Here, that human review is not a fallback. It is the
training set.

Expect the reason distribution to be lopsided and to teach you something about
your own filter that you did not know.

## Composer (not a model, a formatter)

`qwen3:8b` via Ollama. It writes playbooks and prose. It does not predict, rank,
or score.

**Enforced separation:**
- Measured numbers → SQLite → template-injected. The LLM never emits them.
- Estimated numbers (price, cost, TTFD) → LLM → stored with confidence, always
  rendered with an explicit estimate marker.
- Validation: if a measured figure appears in generated prose and contradicts
  the DB, discard and retry. There is a test for this.

## Calibration

Once `outcomes` has rows, compare estimated vs actual on price, cost and TTFD.
Report in `/stats`. Expect the first estimates to be badly off. Measuring the
error is the point — an uncalibrated estimate presented confidently is the
failure mode this whole document is written against.
