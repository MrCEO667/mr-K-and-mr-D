"""The feasibility gate: five rules, pass/fail plus reasons.

The rejection tests are the easy half. The half that matters is the false
positives -- "stock photos" is not inventory and an MIT license is not a
professional licence -- because a gate that rejects good opportunities for
made-up reasons is worse than no gate, and nothing in a live run would tell
you it was happening.
"""
import math
import pathlib

import pytest
import yaml

from radar import feasibility

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeConfig:
    """Only the budget block matters to the gate."""

    def __init__(self, **budget):
        self.budget = {
            "max_setup_usd": 100,
            "min_margin_multiple": 3.0,
            "max_ttfd_days": 7,
            **budget,
        }

    def get(self, dotted, default=None):
        return self.budget if dotted == "budget" else default


CAPS = feasibility.Capabilities(
    can_build=["python_backend", "digital_products"],
    cannot_build=[
        "physical_manufacturing",
        "inventory_holding",
        "anything_needing_licensing",
        "anything_needing_employees",
        "mobile_native",
        "hardware",
    ],
)


def opportunity(**overrides):
    """A clean, passing opportunity. Override one field per test."""
    base = {
        "title": "AI voice-clone for podcast intros",
        "mode": "online",
        "playbook": {
            "offer": "A five-second branded intro, delivered as an mp3",
            "channel": "Fiverr",
            "steps": ["Set up the gig", "Clone a demo voice", "Deliver the first order"],
        },
        "requirements": ["domain", "Fiverr account", "3h setup"],
        "setup_cost_usd": 12.0,
        "price_usd": 29.0,
        "cost_per_sale_usd": 3.4,
        "ttfd_days": 2,
        "confidence": 0.6,
    }
    base.update(overrides)
    return base


def evaluate(**overrides):
    return feasibility.evaluate(opportunity(**overrides), FakeConfig(), CAPS)


def test_a_clean_opportunity_passes_with_no_reasons():
    verdict = evaluate()
    assert verdict.passed
    assert verdict.reasons == []
    assert verdict.margin_multiple == pytest.approx(29.0 / 3.4)


# --- the five rules ----------------------------------------------------------


def test_setup_cost_over_the_cap_is_rejected():
    verdict = evaluate(setup_cost_usd=250.0)
    assert not verdict.passed
    assert verdict.codes == ["over_budget"]
    assert "$250" in verdict.reasons[0] and "$100" in verdict.reasons[0]


def test_setup_cost_exactly_at_the_cap_passes():
    # The rule is "<= max", so the boundary is inside the gate, not outside it.
    assert evaluate(setup_cost_usd=100.0).passed


def test_margin_below_the_minimum_is_rejected():
    verdict = evaluate(price_usd=10.0, cost_per_sale_usd=5.0)  # 2.0x
    assert verdict.codes == ["low_margin"]
    assert "2.0x" in verdict.reasons[0]


def test_margin_exactly_at_the_minimum_passes():
    assert evaluate(price_usd=30.0, cost_per_sale_usd=10.0).passed  # 3.0x


def test_time_to_first_dollar_over_the_cap_is_rejected():
    verdict = evaluate(ttfd_days=30)
    assert verdict.codes == ["too_slow"]
    assert "30 days" in verdict.reasons[0]


def test_physical_manufacturing_is_rejected():
    verdict = evaluate(
        playbook={
            "offer": "Custom enamel pins",
            "channel": "Etsy",
            "steps": ["Find a factory", "Order a manufacturing run"],
        }
    )
    assert "cannot_build:physical_manufacturing" in verdict.codes


def test_holding_inventory_is_rejected():
    verdict = evaluate(requirements=["warehouse space", "initial inventory of 200 units"])
    assert "cannot_build:inventory_holding" in verdict.codes


def test_each_blocker_slug_is_detectable():
    """Every slug in cannot_build that has patterns must actually fire on
    something, or it is a rule that silently never runs."""
    samples = {
        "physical_manufacturing": "we need a factory",
        "inventory_holding": "hold inventory",
        "anything_needing_licensing": "requires a medical license",
        "anything_needing_employees": "hire staff to answer tickets",
        "mobile_native": "ship an ios app",
        "hardware": "an arduino board",
    }
    for slug, text in samples.items():
        assert feasibility._matches(text, feasibility.BLOCKER_PATTERNS[slug]), slug


# --- false positives, the half that matters ----------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "a pack of stock photos",          # "stock" is not inventory
        "stock footage for editors",
        "released under an MIT license",   # not a professional licence
        "an open-source license header",
        "we license the template to buyers",
        "a wise choice of niche",          # not the Wise payment rail as a blocker
        "print-on-demand handled by the platform",
    ],
)
def test_innocent_phrases_are_not_treated_as_blockers(phrase):
    verdict = evaluate(
        playbook={"offer": phrase, "channel": "Gumroad", "steps": [phrase]}
    )
    blockers = [c for c in verdict.codes if c.startswith("cannot_build:")]
    assert blockers == [], f"{phrase!r} wrongly matched {blockers}"


def test_a_blocker_only_fires_for_a_slug_the_team_actually_declared():
    # Same text, but this team can do mobile work, so it is not a rejection.
    caps = feasibility.Capabilities(can_build=["mobile_native"], cannot_build=["hardware"])
    verdict = feasibility.evaluate(
        opportunity(requirements=["ship an ios app"]), FakeConfig(), caps
    )
    assert verdict.passed


# --- unknowns are rejections, not passes -------------------------------------


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("setup_cost_usd", "unknown_setup_cost"),
        ("ttfd_days", "unknown_ttfd"),
    ],
)
def test_a_missing_estimate_is_rejected_rather_than_waved_through(field, code):
    verdict = evaluate(**{field: None})
    assert not verdict.passed
    assert code in verdict.codes


def test_a_missing_price_or_cost_makes_the_margin_unknown_and_rejects():
    assert "unknown_margin" in evaluate(price_usd=None).codes
    assert "unknown_margin" in evaluate(cost_per_sale_usd=None).codes


# --- margin arithmetic -------------------------------------------------------


def test_margin_is_computed_here_and_never_read_from_the_payload():
    # A payload claiming a wonderful margin does not get one; price/cost rules.
    verdict = evaluate(price_usd=10.0, cost_per_sale_usd=5.0, margin_multiple=99.0)
    assert verdict.margin_multiple == pytest.approx(2.0)
    assert "low_margin" in verdict.codes


def test_a_negative_cost_per_sale_is_rejected_as_unknown():
    verdict = evaluate(cost_per_sale_usd=-5.0)
    assert not verdict.passed
    assert "unknown_margin" in verdict.codes
    assert not any("zero" in note for note in verdict.notes)


def test_a_zero_cost_per_sale_is_noted_not_rejected():
    verdict = evaluate(cost_per_sale_usd=0.0)
    assert verdict.passed
    assert math.isinf(verdict.margin_multiple)
    assert any("unbounded" in note for note in verdict.notes)


def test_margin_multiple_helper_handles_the_edges():
    assert feasibility.margin_multiple(29.0, 3.4) == pytest.approx(8.5, abs=0.05)
    assert feasibility.margin_multiple(None, 3.4) is None
    assert feasibility.margin_multiple(29.0, None) is None
    assert feasibility.margin_multiple(29.0, 0) == math.inf
    # A negative cost is a broken estimate, not a free product. Returning inf
    # would clear the margin gate and explain it as "estimated at zero".
    assert feasibility.margin_multiple(29.0, -1) is None


# --- modes, accumulation, persistence ----------------------------------------


def test_an_unknown_mode_is_rejected():
    assert "invalid_mode" in evaluate(mode="teleportation").codes
    assert "invalid_mode" in evaluate(mode=None).codes


@pytest.mark.parametrize("mode", feasibility.VALID_MODES)
def test_every_declared_mode_is_accepted(mode):
    assert evaluate(mode=mode).passed


def test_every_failing_rule_is_reported_not_just_the_first():
    """The reasons go on the card. Stopping at the first one would hide the
    rest and make a rejected card look like a near miss."""
    verdict = evaluate(setup_cost_usd=500.0, ttfd_days=60, price_usd=10.0, cost_per_sale_usd=9.0)
    assert set(verdict.codes) == {"over_budget", "low_margin", "too_slow"}
    assert len(verdict.reasons) == 3


def test_a_rejected_opportunity_is_still_a_persistable_row():
    """Rejections stay in the DB for M9 to learn from, so as_row() must
    produce something writable rather than nothing."""
    verdict = evaluate(setup_cost_usd=500.0)
    feasible, reasons = verdict.as_row()
    assert feasible == 0
    assert "over the $100 cap" in reasons

    passed, no_reasons = evaluate().as_row()
    assert passed == 1
    assert no_reasons == ""


def test_reasons_are_joined_into_one_column_in_order():
    verdict = evaluate(setup_cost_usd=500.0, ttfd_days=60)
    _, reasons = verdict.as_row()
    assert reasons.count("|") == 1


# --- payment rails, off by default -------------------------------------------


def test_rails_are_noted_but_not_gated_by_default():
    caps = feasibility.Capabilities(cannot_build=[], rails_enforced=False, rails_available=[])
    verdict = feasibility.evaluate(opportunity(), FakeConfig(), caps)
    assert verdict.passed
    assert verdict.rail == "fiverr_payout"
    assert any("not enforced" in note for note in verdict.notes)


def test_enforcement_does_not_reward_a_playbook_that_names_no_rail():
    """A gate that passes silence and rejects honesty is worse than no gate."""
    caps = feasibility.Capabilities(
        cannot_build=[], rails_enforced=True, rails_available=["gumroad"]
    )
    vague = feasibility.evaluate(
        opportunity(
            playbook={"offer": "A template pack", "channel": "a website", "steps": ["Build"]},
            requirements=["a domain"],
        ),
        FakeConfig(),
        caps,
    )
    assert not vague.passed
    assert "unknown_payment_rail" in vague.codes


def test_an_unavailable_rail_is_gated_once_enforcement_is_on():
    caps = feasibility.Capabilities(
        cannot_build=[], rails_enforced=True, rails_available=["gumroad"]
    )
    verdict = feasibility.evaluate(opportunity(), FakeConfig(), caps)
    assert not verdict.passed
    assert "no_payment_rail" in verdict.codes


def test_an_available_rail_passes_under_enforcement():
    caps = feasibility.Capabilities(
        cannot_build=[], rails_enforced=True, rails_available=["fiverr_payout"]
    )
    assert feasibility.evaluate(opportunity(), FakeConfig(), caps).passed


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("deliver through gumroad", "gumroad"),
        ("a fiverr gig", "fiverr_payout"),
        ("paid out via wise.com", "wise"),
        ("a wise choice of niche", None),      # ordinary English, not a rail
        ("otherwise a good fit", None),
        ("nothing in particular", None),
    ],
)
def test_rail_detection_does_not_fire_on_ordinary_words(text, expected):
    assert feasibility.detect_rail(text) == expected


def test_shipped_config_leaves_rails_unenforced():
    """Open decision A: the default is off, and flipping it is a deliberate act."""
    rails = yaml.safe_load((ROOT / "config" / "payment_rails.yaml").read_text(encoding="utf-8"))
    assert rails["enforce"] is False


# --- wiring to the real config files -----------------------------------------


def test_capabilities_load_from_the_shipped_files():
    caps = feasibility.Capabilities.load()
    assert "physical_manufacturing" in caps.cannot_build
    assert "inventory_holding" in caps.cannot_build
    assert caps.can_build
    assert caps.rails_enforced is False


def test_the_gate_reads_budget_numbers_from_the_example_config():
    """The caps must come from config, not from constants in the module."""
    example = yaml.safe_load(
        (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    )

    class ExampleConfig:
        def get(self, dotted, default=None):
            return example.get(dotted, default)

    assert feasibility.evaluate(opportunity(), ExampleConfig(), CAPS).passed
    over = feasibility.evaluate(
        opportunity(setup_cost_usd=example["budget"]["max_setup_usd"] + 1),
        ExampleConfig(),
        CAPS,
    )
    assert "over_budget" in over.codes


def test_missing_capability_files_do_not_crash_the_gate(tmp_path):
    caps = feasibility.Capabilities.load(tmp_path / "nope.yaml", tmp_path / "also-nope.yaml")
    assert caps.cannot_build == []
    assert feasibility.evaluate(opportunity(), FakeConfig(), caps).passed
