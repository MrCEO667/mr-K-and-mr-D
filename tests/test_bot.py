"""The bot: access control, suppression, first_wins, and the card.

The allowlist is the whole access control -- a supergroup is joinable by
anyone -- so the tests that matter most are the ones proving a stranger gets
nothing, *including through a button*. Forgetting the callback path is the
classic way an allowlist turns out to be decorative.
"""
import json
import time

import pytest

from radar import alerts as alerts_mod
from radar import card as card_mod
from radar import db

MR_D, MR_K, STRANGER = 528037846, 466866179, 999999


class FakeConfig:
    """Enough Config surface for the bot: operators plus dotted lookups."""

    def __init__(self, **over):
        self.data = {
            "scoring.durability_horizon": 60,
            "alerts.max_per_day": 12,
            "alerts.rescore_delta": 0.15,
            "alerts.min_composite": 0.0,
            "alerts.quiet_hours": [23, 8],
            "telegram.decision_mode": "first_wins",
            "telegram.reject_unknown_users": True,
        }
        self.data.update(over)
        self.environ = {"TELEGRAM_CHAT_ID": "-100123", "TELEGRAM_BOT_TOKEN": "t"}
        self._ops = {MR_D: "Mr D", MR_K: "Mr K"}

    def get(self, dotted, default=None):
        return self.data.get(dotted, default)

    def is_operator(self, tg_id):
        return tg_id in self._ops

    def operator_name(self, tg_id):
        return self._ops.get(tg_id)


@pytest.fixture
def conn(tmp_path):
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


def add_opportunity(conn, opportunity_id=1, *, feasible=1, composed=1, title="Voice Clone Kit"):
    conn.execute(
        "INSERT INTO opportunities (id, term_id, run_id, title, mode, playbook_json, "
        "requirements_json, setup_cost_usd, price_usd, cost_per_sale_usd, margin_multiple, "
        "ttfd_days, confidence, feasible, feasible_reasons, composed, llm_model, ts) "
        "VALUES (?, 1, 'r', ?, 'online', ?, ?, 50.0, 45.0, 10.0, 4.5, 3, 0.8, ?, '', ?, "
        "'qwen3:8b', 1)",
        (
            opportunity_id,
            title,
            json.dumps({"offer": "A ready-made kit", "channel": "Gumroad", "steps": ["a", "b"]}),
            json.dumps(["Gumroad account"]),
            feasible,
            composed,
        ),
    )
    return opportunity_id


def add_score(conn, *, composite=0.7, saturation="LOW", scorer="model:60", ts=100):
    conn.execute(
        "INSERT INTO scores (term_id, run_id, durability_30, durability_60, durability_90, "
        "saturation_label, saturation_raw, demand_growth, supply_growth, relevance, "
        "composite, scorer, ts) VALUES (1, 'r', 0.8, 0.75, 0.6, ?, 195, 0.23, 0.0, 0.5, ?, ?, ?)",
        (saturation, composite, scorer, ts),
    )


def candidate(opportunity_id=1, composite=0.7, saturation="LOW"):
    return alerts_mod.Candidate(
        opportunity_id=opportunity_id, term_id=1, composite=composite,
        saturation_label=saturation, title="Voice Clone Kit",
    )


# --- access control -----------------------------------------------------------


class FakeUser:
    def __init__(self, id):
        self.id, self.first_name, self.username = id, "Someone", "someone"


class FakeUpdate:
    def __init__(self, user_id, *, callback=False):
        self.effective_user = FakeUser(user_id) if user_id else None
        self.callback_query = object() if callback else None
        self.effective_message = None


@pytest.mark.parametrize("tg_id", [MR_D, MR_K])
def test_both_operators_are_recognised(conn, tg_id):
    from radar.bot import Bot

    bot = Bot(FakeConfig(), conn)
    assert bot.actor(FakeUpdate(tg_id)) is not None


def test_a_stranger_is_not_an_actor_on_a_message(conn):
    from radar.bot import Bot

    assert Bot(FakeConfig(), conn).actor(FakeUpdate(STRANGER)) is None


def test_a_stranger_is_not_an_actor_through_a_button_either(conn):
    """A group is joinable, so a button is as much an entry point as a command."""
    from radar.bot import Bot

    assert Bot(FakeConfig(), conn).actor(FakeUpdate(STRANGER, callback=True)) is None


def test_an_update_with_no_user_is_refused(conn):
    from radar.bot import Bot

    assert Bot(FakeConfig(), conn).actor(FakeUpdate(None)) is None


def test_the_operator_name_comes_from_config_not_from_telegram(conn):
    """Decisions are attributed to a named operator; M9 must be able to tell
    the two apart even if someone changes their Telegram display name."""
    from radar.bot import Bot

    assert Bot(FakeConfig(), conn).actor(FakeUpdate(MR_D)) == (MR_D, "Mr D")


# --- first_wins ---------------------------------------------------------------


def test_the_first_tap_settles_the_card(conn):
    add_opportunity(conn)
    written, message = alerts_mod.record_decision(conn, 1, "watch", MR_D, "Mr D")
    assert written and "watch" in message


def test_a_second_tap_writes_nothing_and_says_who_decided(conn):
    add_opportunity(conn)
    alerts_mod.record_decision(conn, 1, "watch", MR_D, "Mr D")
    written, message = alerts_mod.record_decision(conn, 1, "dismiss", MR_K, "Mr K")

    assert not written
    assert "Mr D" in message and "first tap wins" in message
    rows = conn.execute("SELECT * FROM decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["action"] == "watch"


def test_the_decision_records_which_operator_tapped(conn):
    add_opportunity(conn)
    alerts_mod.record_decision(conn, 1, "dismiss", MR_K, "Mr K", reason="saturated")
    row = conn.execute("SELECT * FROM decisions").fetchone()
    assert row["actor_tg_id"] == MR_K
    assert row["actor_name"] == "Mr K"
    assert row["reason"] == "saturated"


def test_an_unknown_action_is_refused(conn):
    add_opportunity(conn)
    written, message = alerts_mod.record_decision(conn, 1, "explode", MR_D, "Mr D")
    assert not written and "unknown action" in message


# --- suppression --------------------------------------------------------------


def test_a_new_opportunity_is_sent(conn):
    add_opportunity(conn)
    assert alerts_mod.should_send(conn, candidate()).send


def test_the_same_opportunity_is_not_sent_twice(conn):
    add_opportunity(conn)
    alerts_mod.record_alert(conn, candidate())
    verdict = alerts_mod.should_send(conn, candidate())
    assert not verdict.send
    assert "already alerted" in verdict.reason


def test_a_big_move_in_composite_re_alerts(conn):
    add_opportunity(conn)
    alerts_mod.record_alert(conn, candidate(composite=0.50))
    assert alerts_mod.should_send(conn, candidate(composite=0.70)).send      # +0.20
    assert not alerts_mod.should_send(conn, candidate(composite=0.60)).send  # +0.10


def test_a_saturation_change_re_alerts_even_at_the_same_score(conn):
    add_opportunity(conn)
    alerts_mod.record_alert(conn, candidate(composite=0.7, saturation="LOW"))
    verdict = alerts_mod.should_send(conn, candidate(composite=0.7, saturation="HIGH"))
    assert verdict.send
    assert "LOW -> HIGH" in verdict.reason


def test_a_dismissed_opportunity_never_comes_back(conn):
    add_opportunity(conn)
    alerts_mod.record_decision(conn, 1, "dismiss", MR_D, "Mr D", reason="saturated")
    verdict = alerts_mod.should_send(conn, candidate(composite=0.99))
    assert not verdict.send
    assert "dismissed" in verdict.reason


def test_too_slow_is_the_one_dismissal_that_can_come_back(conn):
    """"Too slow" is a statement about timing, not about the idea."""
    add_opportunity(conn)
    alerts_mod.record_decision(conn, 1, "dismiss", MR_D, "Mr D", reason="too_slow")
    assert alerts_mod.should_send(conn, candidate()).send


def test_a_watched_opportunity_is_not_re_alerted(conn):
    add_opportunity(conn)
    alerts_mod.record_decision(conn, 1, "watch", MR_K, "Mr K")
    verdict = alerts_mod.should_send(conn, candidate())
    assert not verdict.send
    assert "Mr K" in verdict.reason


def test_a_composite_below_the_floor_is_held(conn):
    add_opportunity(conn)
    verdict = alerts_mod.should_send(conn, candidate(composite=0.30), min_composite=0.55)
    assert not verdict.send
    assert "below the floor" in verdict.reason


def test_only_feasible_composed_opportunities_are_candidates(conn):
    add_score(conn)
    add_opportunity(conn, 1, feasible=1, composed=1)
    add_opportunity(conn, 2, feasible=0, composed=1, title="Gated")
    add_opportunity(conn, 3, feasible=1, composed=0, title="Not composed")
    assert [c.opportunity_id for c in alerts_mod.candidates(conn)] == [1]


@pytest.mark.parametrize(
    ("hour", "quiet", "expected"),
    [
        (2, [23, 8], True), (23, [23, 8], True), (8, [23, 8], False),
        (12, [23, 8], False), (22, [23, 8], False),
        (10, [9, 17], True), (18, [9, 17], False),
        (5, [0, 0], False),   # a degenerate window silences nothing
    ],
)
def test_quiet_hours_wrap_midnight(hour, quiet, expected):
    assert alerts_mod.in_quiet_hours(hour, quiet) is expected


# --- the card -----------------------------------------------------------------


def test_every_estimated_figure_is_marked(conn):
    add_opportunity(conn)
    add_score(conn)
    card = card_mod.build(conn, 1)
    assert card.economics_line.count(card_mod.ESTIMATE_MARK) == 3
    assert "4.5x" in card.economics_line
    assert "$50" in card.economics_line


def test_measured_lines_carry_no_estimate_mark(conn):
    add_opportunity(conn)
    add_score(conn)
    card = card_mod.build(conn, 1)
    assert card_mod.ESTIMATE_MARK not in card.durability_line
    assert card_mod.ESTIMATE_MARK not in card.evidence_line


def test_the_card_names_momentum_when_the_model_was_refused(conn):
    add_opportunity(conn)
    add_score(conn, scorer="momentum_fallback+relevance:neutral")
    assert "momentum" in card_mod.build(conn, 1).durability_line


def test_the_card_names_the_model_when_it_was_used(conn):
    add_opportunity(conn)
    add_score(conn, scorer="model:60+relevance:neutral")
    assert "model" in card_mod.build(conn, 1).durability_line


def test_uncounted_supply_is_not_rendered_as_zero_competitors(conn):
    add_opportunity(conn)
    conn.execute(
        "INSERT INTO scores (term_id, run_id, durability_60, saturation_label, "
        "saturation_raw, demand_growth, supply_growth, relevance, composite, scorer, ts) "
        "VALUES (1, 'r', 0.7, 'LOW', 0, 0.0, 0.0, 0.5, 0.6, 'model:60', 100)"
    )
    assert "not counted" in card_mod.build(conn, 1).evidence_line


def test_a_gated_card_shows_why(conn):
    add_opportunity(conn, feasible=0)
    conn.execute("UPDATE opportunities SET feasible_reasons = 'Setup costs $800' WHERE id = 1")
    add_score(conn)
    rendered = card_mod.build(conn, 1).render()
    assert "Gated" in rendered and "$800" in rendered


def test_a_missing_opportunity_renders_nothing_rather_than_crashing(conn):
    assert card_mod.build(conn, 4242) is None


def test_the_rendered_card_leads_with_the_title_and_mode(conn):
    add_opportunity(conn)
    add_score(conn)
    first = card_mod.build(conn, 1).render().splitlines()[0]
    assert "Voice Clone Kit" in first and "ONLINE" in first


# --- /why ---------------------------------------------------------------------


def test_why_separates_measured_from_estimated(conn):
    from radar.bot import Bot

    add_opportunity(conn)
    add_score(conn)
    text = Bot(FakeConfig(), conn).why(1)
    assert "Measured" in text and "Estimated" in text
    assert "may be wrong" in text


def test_why_admits_the_personal_model_is_not_active_yet(conn):
    from radar.bot import Bot

    add_opportunity(conn)
    add_score(conn)
    assert "/100 decisions" in Bot(FakeConfig(), conn).why(1)


def test_why_warns_when_durability_came_from_momentum(conn):
    from radar.bot import Bot

    add_opportunity(conn)
    add_score(conn, scorer="momentum_fallback+relevance:neutral")
    assert "naive momentum" in Bot(FakeConfig(), conn).why(1)


def test_why_on_an_unknown_id_says_so(conn):
    from radar.bot import Bot

    assert "No opportunity" in Bot(FakeConfig(), conn).why(9999)


# --- the daily cap ------------------------------------------------------------


def test_the_daily_cap_stops_further_cards(conn):
    from radar.bot import Bot

    add_score(conn)
    for i in range(1, 4):
        add_opportunity(conn, i, title=f"Idea {i}")
    now = int(time.time())
    for i in range(1, 3):
        alerts_mod.record_alert(conn, candidate(i), ts=now)

    bot = Bot(FakeConfig(**{"alerts.max_per_day": 2, "alerts.quiet_hours": [0, 0]}), conn)
    assert bot.due_cards() == []
