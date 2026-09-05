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

`0.6` is a config constant. Sweep it during M5 and record the chosen value here.

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

**Result: _to be filled in at M5._**

```
precision@10   random: ___   momentum: ___   model: ___
temporal split: train < ______  test >= ______
n_terms: ___   n_windows: ___   label threshold: ___
verdict: ___
```

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
