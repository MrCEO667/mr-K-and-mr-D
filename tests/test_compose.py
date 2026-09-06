"""The composer: strict JSON, bounded failure, and the measured/estimated line.

PROMPT.md M7 asks for one assertion by name -- "if any measured figure appears
in LLM prose and disagrees with the DB, discard the generation and retry.
Assert this in tests." That is `test_prose_contradicting_the_database_is_rejected`.

The rest is about a small model behaving like a small model: fencing its JSON,
thinking out loud, and copying any number it is shown.
"""
import json

import pytest

from radar import compose, feasibility
from radar.score import Score

GOOD = {
    "title": "Voice Clone Toolkit",
    "mode": "online",
    "playbook": {
        "offer": "A ready-made voice cloning kit",
        "channel": "Gumroad",
        "steps": ["Build the kit", "List it", "Post about it"],
    },
    "requirements": ["Gumroad account"],
    "setup_cost_usd": 50,
    "price_usd": 45,
    "cost_per_sale_usd": 10,
    "ttfd_days": 5,
    "confidence": 0.85,
}


class FakeOllama:
    """Returns canned generations in order. Never touches the network."""

    def __init__(self, *responses, model="fake"):
        self.responses = list(responses)
        self.model = model
        self.calls = []

    def generate(self, prompt, *, temperature=compose.BASE_TEMPERATURE):
        self.calls.append((prompt, temperature))
        if not self.responses:
            raise compose.ComposeError("no more canned responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeConfig:
    def __init__(self, **over):
        self.data = {
            "budget": {"max_setup_usd": 100, "min_margin_multiple": 3.0, "max_ttfd_days": 7},
            "llm.max_retries": 2,
        }
        self.data.update(over)

    def get(self, dotted, default=None):
        return self.data.get(dotted, default)


CAPS = feasibility.Capabilities(
    can_build=["python_backend"], cannot_build=["hardware", "inventory_holding"]
)


@pytest.fixture
def conn(tmp_path):
    from radar import db

    connection = db.connect(tmp_path / "t.db")
    connection.execute(
        "INSERT INTO runs (run_id, kind, started_ts, status) VALUES ('r', 'test', 1, 'ok')"
    )
    connection.execute(
        "INSERT INTO terms (id, term, normalized, origin, first_seen_ts, last_seen_ts) "
        "VALUES (1, 'ai voice clone', 'ai voice clone', 'seed', 1, 1)"
    )
    yield connection
    connection.close()


def score(**over):
    base = {
        "term_id": 1, "durability": {60: 0.7}, "saturation_label": "LOW",
        "saturation_raw": 0, "demand_growth": 0.0, "supply_growth": 0.0,
        "relevance": 0.5, "composite": 0.6, "scorer": "t",
    }
    base.update(over)
    return Score(**base)


# --- extracting JSON from what a small model actually returns -----------------


def test_plain_json_parses():
    assert compose.extract_json(json.dumps(GOOD))["title"] == GOOD["title"]


def test_a_fenced_block_parses():
    raw = "Here is the JSON:\n```json\n" + json.dumps(GOOD) + "\n```\nHope that helps!"
    assert compose.extract_json(raw)["mode"] == "online"


def test_a_think_block_is_stripped():
    raw = "<think>The user wants {a plan}. I should use Gumroad.</think>" + json.dumps(GOOD)
    assert compose.extract_json(raw)["title"] == GOOD["title"]


def test_trailing_prose_after_the_object_is_ignored():
    raw = json.dumps(GOOD) + "\n\nLet me know if you want another idea."
    assert compose.extract_json(raw)["title"] == GOOD["title"]


def test_braces_inside_strings_do_not_end_the_object():
    payload = dict(GOOD, title="A {curly} kit")
    assert compose.extract_json(json.dumps(payload))["title"] == "A {curly} kit"


@pytest.mark.parametrize(
    "raw",
    [
        "I cannot help with that.",          # no object at all
        '{"title": "x", ',                   # unterminated
        '{"title": "x" "mode": "online"}',   # malformed
    ],
)
def test_unusable_generations_raise_compose_error(raw):
    with pytest.raises(compose.ComposeError):
        compose.extract_json(raw)


# --- contract 4 ---------------------------------------------------------------


def test_a_well_formed_payload_validates():
    assert compose.validate_shape(dict(GOOD)) == GOOD


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ({"title": ""}, "title"),
        ({"title": "x" * 61}, "over 60"),
        ({"mode": "teleport"}, "mode"),
        ({"playbook": None}, "playbook"),
        ({"playbook": {"offer": "", "channel": "c", "steps": ["a"]}}, "offer"),
        ({"playbook": {"offer": "o", "channel": "c", "steps": []}}, "steps"),
        ({"requirements": "a domain"}, "requirements"),
        ({"setup_cost_usd": "twelve"}, "setup_cost_usd"),
        ({"ttfd_days": 2.5}, "ttfd_days"),
    ],
)
def test_contract_violations_are_rejected(mutation, fragment):
    with pytest.raises(compose.ComposeError, match=fragment):
        compose.validate_shape(dict(GOOD, **mutation))


def test_a_measured_field_in_the_payload_is_rejected():
    """The schema gives measured numbers nowhere to go. One appearing means the
    model invented a slot for the figures it was told not to produce."""
    with pytest.raises(compose.ComposeError, match="view_sum"):
        compose.validate_shape(dict(GOOD, view_sum=48000))


# --- the measured / estimated line, which M7 asks be asserted -----------------


def test_prose_contradicting_the_database_is_rejected():
    bundle = compose.MeasuredBundle(
        term="ai voice clone", term_id=1, per_source={"post_count": 47.0}
    )
    lying = dict(GOOD, playbook=dict(GOOD["playbook"], offer="Riding 300 posts a week"))
    problems = compose.check_against_measurements(lying, bundle)
    assert problems and "post_count" in problems[0]

    honest = dict(GOOD, playbook=dict(GOOD["playbook"], offer="Riding 47 posts a week"))
    assert compose.check_against_measurements(honest, bundle) == []


def test_a_contradiction_costs_the_generation_and_triggers_a_retry(conn):
    bundle_metric = '{"title": "T", "mode": "online", "playbook": {"offer": "up 900 percent",\
 "channel": "Gumroad", "steps": ["a"]}, "requirements": [], "setup_cost_usd": 10,\
 "price_usd": 40, "cost_per_sale_usd": 5, "ttfd_days": 2, "confidence": 0.5}'
    client = FakeOllama(bundle_metric, json.dumps(GOOD))

    conn.execute(
        "INSERT INTO signal_snapshots (term_id, source, metric, value, ts, run_id) "
        "VALUES (1, 'google_trends', 'interest', 10.0, 86400, 'r')"
    )
    result = compose.compose_one(
        conn, FakeConfig(), score(demand_growth=0.1), "ai voice clone", client, caps=CAPS
    )
    assert result.composed
    assert result.attempts == 2
    assert "demand_growth_pct" in result.errors[0]


def test_claims_the_database_cannot_speak_to_are_left_alone():
    bundle = compose.MeasuredBundle(term="x", term_id=1)  # nothing measured
    noisy = dict(GOOD, playbook=dict(GOOD["playbook"], offer="Sell 3 templates a day"))
    assert compose.check_against_measurements(noisy, bundle) == []


def test_find_measured_claims_reads_numbers_next_to_metric_words():
    found = dict(compose.find_measured_claims("47 posts and 12,000 views, up 340%"))
    assert found["post_count"] == 47
    assert found["view_sum"] == 12000
    assert found["demand_growth_pct"] == 340


# --- the prompt must not hand the model its answers ---------------------------


def test_the_prompt_carries_no_numeric_estimate_anchors():
    """Regression. Shown `"setup_cost_usd": 12.0`, qwen3:8b returned that exact
    figure -- and the other four -- for a live term on three attempts at three
    temperatures. Five fabricated numbers that would have been stored as
    estimates and printed with "(est.)" beside them."""
    assert compose.anchored_estimates() == []


def test_the_anchor_detector_actually_detects_an_anchor():
    anchored = compose.anchored_estimates('{"setup_cost_usd": 12.0, "price_usd": 29.0}')
    assert anchored == ["setup_cost_usd", "price_usd"]


def test_the_prompt_names_what_the_team_cannot_build(conn):
    prompt = compose.build_prompt(
        compose.MeasuredBundle(term="drone parts", term_id=1), FakeConfig(), CAPS
    )
    assert "drone parts" in prompt
    assert "hardware" in prompt


# --- failure is bounded -------------------------------------------------------


def test_retries_are_bounded_and_then_the_row_says_it_was_not_composed(conn):
    client = FakeOllama("nope", "still nope", "nope again", "a fourth")
    result = compose.compose_one(
        conn, FakeConfig(), score(), "ai voice clone", client, caps=CAPS
    )
    assert not result.composed
    assert result.attempts == 3           # one attempt plus two retries
    assert len(client.calls) == 3         # and it stopped asking
    assert result.payload is None
    assert len(result.errors) == 3


def test_each_retry_is_warmer_than_the_last(conn):
    """Retrying at the same temperature mostly reproduces the same rejected
    answer -- observed with the placeholder echo, stable across identical
    calls."""
    client = FakeOllama("no", "no", json.dumps(GOOD))
    compose.compose_one(conn, FakeConfig(), score(), "x", client, caps=CAPS)
    temperatures = [t for _, t in client.calls]
    assert temperatures == sorted(temperatures)
    assert temperatures[0] < temperatures[-1]


def test_an_unreachable_model_is_a_failed_row_not_an_exception(conn):
    client = FakeOllama(*[compose.ComposeError("ollama unreachable")] * 3)
    result = compose.compose_one(conn, FakeConfig(), score(), "x", client, caps=CAPS)
    assert not result.composed
    assert "unreachable" in result.errors[0]


def test_an_infeasible_generation_is_still_composed_just_gated(conn):
    """Composition and feasibility are separate verdicts. The composer did its
    job; the gate rejected the idea, and both facts are recorded."""
    expensive = dict(GOOD, setup_cost_usd=5000)
    result = compose.compose_one(
        conn, FakeConfig(), score(), "x", FakeOllama(json.dumps(expensive)), caps=CAPS
    )
    assert result.composed
    assert not result.verdict.passed
    assert "over_budget" in result.verdict.codes


# --- persistence --------------------------------------------------------------


def test_a_composed_opportunity_is_written_with_its_computed_margin(conn):
    result = compose.compose_one(
        conn, FakeConfig(), score(), "x", FakeOllama(json.dumps(GOOD)), caps=CAPS
    )
    row_id = compose.write_opportunity(conn, "r", result)
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (row_id,)).fetchone()

    assert row["composed"] == 1
    assert row["feasible"] == 1
    assert row["title"] == GOOD["title"]
    assert row["margin_multiple"] == pytest.approx(4.5)   # 45 / 10, computed
    assert json.loads(row["playbook_json"])["channel"] == "Gumroad"


def test_a_failed_composition_is_still_a_row(conn):
    """Deleting it would hide a composer that has quietly stopped working."""
    result = compose.compose_one(
        conn, FakeConfig(), score(), "x", FakeOllama("no", "no", "no"), caps=CAPS
    )
    row_id = compose.write_opportunity(conn, "r", result)
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (row_id,)).fetchone()

    assert row["composed"] == 0
    assert row["feasible"] == 0
    assert row["feasible_reasons"] == "not composed"


def test_an_unbounded_margin_is_stored_as_null_not_infinity(conn):
    """sqlite has no infinity, and a column that sometimes holds one is a trap
    for every reader downstream."""
    free = dict(GOOD, cost_per_sale_usd=0)
    result = compose.compose_one(
        conn, FakeConfig(), score(), "x", FakeOllama(json.dumps(free)), caps=CAPS
    )
    row_id = compose.write_opportunity(conn, "r", result)
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (row_id,)).fetchone()
    assert row["margin_multiple"] is None


def test_a_percent_marker_is_seen_even_when_a_word_follows_it():
    """Regression: the regex consumes the word after "percent", so checking
    only the trailing token missed "up 900 percent Gumroad" while catching
    "up 900 percent"."""
    assert compose.find_measured_claims("up 900 percent Gumroad") == [
        ("demand_growth_pct", 900.0)
    ]
    assert compose.find_measured_claims("up 340% growth") == [("demand_growth_pct", 340.0)]


def test_the_prompt_tells_the_model_what_a_channel_is():
    """Regression: given a bare "where it is sold", qwen3:8b answered with the
    build-capability slugs it had just been shown -- "telegram_bots", "web
    scraping and Telegram bot". A channel is where a buyer pays."""
    assert "marketplace" in compose.PROMPT_TEMPLATE
    assert "NOT a technology" in compose.PROMPT_TEMPLATE
