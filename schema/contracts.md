# FROZEN CONTRACTS

These are the interfaces between modules. Three people work in this repo without
a formal ownership split, so these are what keep you from colliding.

**Changing a contract requires its own commit, made before any code depends on
the change, with `contract:` in the commit message.**

## 1. Collector ABC

```python
class Collector(ABC):
    source: str          # "google_trends" | "youtube" | ...
    quota_per_day: int   # declared, enforced, never discovered by hitting it

    @abstractmethod
    def collect(self, terms: list[Term], run_id: str) -> CollectResult: ...

    @abstractmethod
    def health(self) -> SourceHealth: ...
```

`Term` and `SourceHealth` are the two types that ABC signature refers to:

```python
@dataclass(frozen=True)
class Term:
    id: int
    term: str            # as queried
    normalized: str
    starred: bool        # on the hourly watchlist

@dataclass
class SourceHealth:
    source: str
    status: str          # ok | degraded | down
    latency_ms: int | None
    error_count: int
    message: str | None
```

```python
@dataclass
class Reading:
    term_id: int
    source: str
    metric: str          # interest | video_count | view_sum | post_count | stars
    value: float
    ts: int              # unix seconds

@dataclass
class EvidenceItem:
    term_id: int
    source: str
    url: str
    title: str | None
    snippet: str | None
    metric_json: dict | None

@dataclass
class CollectResult:
    readings: list[Reading]
    evidence: list[EvidenceItem]
    partial: bool        # True if some terms failed
    errors: list[str]
```

**Rules:** never raise on a single-term failure — set `partial=True`. Raise
`SourceUnavailable` only when the whole source is down. Never write to the DB
directly; return data and let the runner persist it.

## 2. Feature vector

Produced by `radar/features.py`. **Used identically by training and scoring.**
Never fork this function — a feature computed differently at train and predict
time is a silent bug you will not find.

```python
@dataclass
class FeatureVector:
    term_id: int
    window_end_ts: int
    slope: float
    acceleration: float
    volatility: float
    seasonality_amp: float
    peak_relative: float        # current / window max
    days_observed: int
    source_breadth: int         # independent sources seeing it
    source_correlation: float
    magnitude_bucket: int
```

## 3. Score

```python
@dataclass
class Score:
    term_id: int
    durability: dict[int, float]     # {30: 0.81, 60: 0.74, 90: 0.62}
    saturation_label: str            # LOW | MED | HIGH
    saturation_raw: int
    demand_growth: float
    supply_growth: float
    relevance: float
    composite: float
    scorer: str                      # "model:v3" | "momentum_fallback"
```

`scorer` is mandatory. When the model file is missing and momentum is used
instead, `/why` must show it. Silent substitution is forbidden.

## 4. Opportunity (LLM output)

Strict JSON. Validate against this before persisting. Two retries on parse
failure, then `composed=false`.

```json
{
  "title": "string, <=60 chars",
  "mode": "online|offline|hybrid",
  "playbook": {
    "offer": "what is sold, one sentence",
    "channel": "where it is sold",
    "steps": ["step 1", "step 2", "step 3"]
  },
  "requirements": ["domain", "Fiverr account", "3h setup"],
  "setup_cost_usd": 12.0,
  "price_usd": 29.0,
  "cost_per_sale_usd": 3.4,
  "ttfd_days": 2,
  "confidence": 0.6
}
```

**Forbidden in LLM output:** any measured metric (trend deltas, post counts,
view sums, saturation counts). Those are read from SQLite and template-injected
into the card. If a measured figure appears in generated prose and disagrees
with the DB, discard the generation and retry. There is a test for this.

`margin_multiple` is **computed**, never generated: `price_usd /
cost_per_sale_usd`. Rendered as `8.5x`.

## 5. Card payload (bot)

```python
@dataclass
class Card:
    opportunity_id: int
    title: str
    mode: str
    durability_line: str    # measured, from DB
    saturation_line: str    # measured, from DB
    economics_line: str     # estimates, marked as such
    evidence_line: str      # measured, from DB
    requirements_line: str
    playbook_lines: list[str]
```

Estimates and measurements must be visually distinguishable on the card. Every
estimated figure carries `(est.)`.
