-- NicheRadar v1 schema. SQLite.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- runs
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,              -- watchlist | sweep | saturation | train
  started_ts  INTEGER NOT NULL,
  finished_ts INTEGER,
  status      TEXT DEFAULT 'running',     -- running | ok | degraded | failed
  notes       TEXT
);

-- ---------------------------------------------------------------- terms
CREATE TABLE IF NOT EXISTS terms (
  id            INTEGER PRIMARY KEY,
  term          TEXT NOT NULL UNIQUE,
  normalized    TEXT NOT NULL,
  category      TEXT,
  origin        TEXT NOT NULL,            -- seed | harvest:reddit | harvest:ph | ...
  starred       INTEGER DEFAULT 0,        -- 1 = hourly watchlist
  status        TEXT DEFAULT 'active',    -- active | muted | dead
  first_seen_ts INTEGER NOT NULL,
  last_seen_ts  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_terms_status ON terms(status, starred);

-- ------------------------------------------------- demand (APPEND ONLY)
-- Never UPDATE. Never DELETE. Two points minimum for velocity; many for
-- durability. The predecessor project stored one snapshot and could compute
-- nothing. This table is written from Milestone 1.
CREATE TABLE IF NOT EXISTS signal_snapshots (
  id      INTEGER PRIMARY KEY,
  term_id INTEGER NOT NULL REFERENCES terms(id),
  source  TEXT NOT NULL,                  -- google_trends | youtube | reddit | tikwm | ...
  metric  TEXT NOT NULL,                  -- interest | video_count | view_sum | post_count | ...
  value   REAL NOT NULL,
  ts      INTEGER NOT NULL,
  run_id  TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_sig_term_ts ON signal_snapshots(term_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sig_dedup
  ON signal_snapshots(term_id, source, metric, ts);

-- ---------------------------------------------- supply (APPEND ONLY)
CREATE TABLE IF NOT EXISTS saturation_snapshots (
  id      INTEGER PRIMARY KEY,
  term_id INTEGER NOT NULL REFERENCES terms(id),
  source  TEXT NOT NULL,                  -- etsy | fiverr | gumroad | shopify | github | ph
  count   INTEGER NOT NULL,
  ts      INTEGER NOT NULL,
  run_id  TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_sat_term_ts ON saturation_snapshots(term_id, ts);

-- ---------------------------------------------------------------- evidence
CREATE TABLE IF NOT EXISTS evidence (
  id       INTEGER PRIMARY KEY,
  term_id  INTEGER NOT NULL REFERENCES terms(id),
  source   TEXT NOT NULL,
  url      TEXT NOT NULL,
  title    TEXT,
  snippet  TEXT,
  metric_json TEXT,
  ts       INTEGER NOT NULL,
  run_id   TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_ev_term ON evidence(term_id, ts);

-- ---------------------------------------------------------------- scores
CREATE TABLE IF NOT EXISTS scores (
  id            INTEGER PRIMARY KEY,
  term_id       INTEGER NOT NULL REFERENCES terms(id),
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  durability_30 REAL, durability_60 REAL, durability_90 REAL,
  saturation_label TEXT,                  -- LOW | MED | HIGH
  saturation_raw   INTEGER,
  demand_growth    REAL,
  supply_growth    REAL,
  relevance     REAL,
  composite     REAL,
  scorer        TEXT NOT NULL,            -- 'model:v3' | 'momentum_fallback'
  ts            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_term ON scores(term_id, ts);

-- --------------------------------------------------------- opportunities
CREATE TABLE IF NOT EXISTS opportunities (
  id                INTEGER PRIMARY KEY,
  term_id           INTEGER NOT NULL REFERENCES terms(id),
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  title             TEXT NOT NULL,
  mode              TEXT NOT NULL,        -- online | offline | hybrid
  playbook_json     TEXT NOT NULL,        -- steps, channel, offer
  requirements_json TEXT,                 -- accounts/tools/skills needed
  setup_cost_usd    REAL,                 -- ESTIMATE
  price_usd         REAL,                 -- ESTIMATE
  cost_per_sale_usd REAL,                 -- ESTIMATE
  margin_multiple   REAL,                 -- price / cost_per_sale, rendered "8.5x"
  ttfd_days         INTEGER,              -- ESTIMATE
  confidence        REAL,
  feasible          INTEGER NOT NULL,
  feasible_reasons  TEXT,
  composed          INTEGER DEFAULT 1,
  llm_model         TEXT,
  ts                INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_feasible ON opportunities(feasible, ts);

-- ------------------------------------------- decisions = training labels
CREATE TABLE IF NOT EXISTS decisions (
  id             INTEGER PRIMARY KEY,
  opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
  action         TEXT NOT NULL,           -- watch | dismiss | pursue
  reason         TEXT,                    -- saturated | cant_build | cant_collect
                                          -- | low_margin | too_slow | not_interested
  actor_tg_id    INTEGER NOT NULL,        -- which operator tapped; two humans
  actor_name     TEXT NOT NULL,           -- label this set, and M9 must be able
                                          -- to tell their taste apart
  ts             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_actor ON decisions(actor_tg_id);

-- ---------------------------------------------- outcomes = closing the loop
CREATE TABLE IF NOT EXISTS outcomes (
  id             INTEGER PRIMARY KEY,
  opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
  tested         INTEGER NOT NULL,
  spent_usd      REAL,
  revenue_usd    REAL,
  days_to_first_sale INTEGER,
  notes          TEXT,
  actor_tg_id    INTEGER NOT NULL,        -- who actually ran the test
  actor_name     TEXT NOT NULL,
  ts             INTEGER NOT NULL
);

-- ---------------------------------------------------------------- alerts
CREATE TABLE IF NOT EXISTS alerts (
  id             INTEGER PRIMARY KEY,
  opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
  sent_ts        INTEGER NOT NULL,
  score_at_send  REAL NOT NULL
);

-- ---------------------------------------------------------- source health
CREATE TABLE IF NOT EXISTS source_health (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  status      TEXT NOT NULL,              -- ok | degraded | down
  latency_ms  INTEGER,
  error_count INTEGER DEFAULT 0,
  message     TEXT,
  ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health ON source_health(source, ts);
