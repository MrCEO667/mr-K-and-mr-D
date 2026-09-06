"""Which opportunities get sent, and which stay quiet.

PROMPT.md M8: an opportunity alerts **once**. It re-alerts only if its composite
moves more than `alert.rescore_delta` or its saturation label changes. Dismissed
items never re-alert -- unless the reason was `too_slow`, because "too slow"
is a statement about timing rather than about the idea, and the same idea can
become worth doing later.

The suppression rules live here rather than in the bot so they can be tested
without a network, a token, or an event loop.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# The one dismissal reason that is about *when* rather than *what*, so it is the
# one that does not bury an opportunity permanently.
REVISITABLE_REASONS = frozenset({"too_slow"})

DISMISS = "dismiss"
WATCH = "watch"
PURSUE = "pursue"
VALID_ACTIONS = (WATCH, DISMISS, PURSUE)

DISMISS_REASONS = (
    "saturated",
    "cant_build",
    "cant_collect",
    "low_margin",
    "too_slow",
    "not_interested",
)


@dataclass
class Candidate:
    opportunity_id: int
    term_id: int
    composite: float
    saturation_label: str
    title: str


@dataclass
class Suppression:
    send: bool
    reason: str


def last_alert(conn: sqlite3.Connection, opportunity_id: int):
    return conn.execute(
        "SELECT * FROM alerts WHERE opportunity_id = ? ORDER BY sent_ts DESC, id DESC LIMIT 1",
        (opportunity_id,),
    ).fetchone()


def settled_decision(conn: sqlite3.Connection, opportunity_id: int):
    """The decision that settled this card, under first_wins the earliest one."""
    return conn.execute(
        "SELECT * FROM decisions WHERE opportunity_id = ? ORDER BY ts ASC, id ASC LIMIT 1",
        (opportunity_id,),
    ).fetchone()


def should_send(
    conn: sqlite3.Connection,
    candidate: Candidate,
    *,
    rescore_delta: float = 0.15,
    min_composite: float = 0.0,
) -> Suppression:
    """Whether this opportunity is worth the operators' attention right now."""
    if candidate.composite < min_composite:
        return Suppression(False, f"composite {candidate.composite:.2f} below the floor")

    decision = settled_decision(conn, candidate.opportunity_id)
    if decision is not None:
        action = decision["action"]
        reason = decision["reason"] or ""
        if action == DISMISS and reason not in REVISITABLE_REASONS:
            return Suppression(False, f"dismissed as {reason or 'no reason given'}")
        if action in (WATCH, PURSUE):
            return Suppression(False, f"already {action} by {decision['actor_name']}")

    previous = last_alert(conn, candidate.opportunity_id)
    if previous is None:
        return Suppression(True, "never alerted")

    moved = abs(candidate.composite - previous["score_at_send"])
    if moved > rescore_delta:
        return Suppression(True, f"composite moved {moved:.2f}")

    was = previous["saturation_at_send"]
    if was and was != candidate.saturation_label:
        return Suppression(True, f"saturation {was} -> {candidate.saturation_label}")

    return Suppression(False, f"already alerted, composite moved only {moved:.2f}")


def record_alert(
    conn: sqlite3.Connection, candidate: Candidate, *, ts: int | None = None
) -> int:
    from . import db

    cursor = conn.execute(
        "INSERT INTO alerts (opportunity_id, sent_ts, score_at_send, saturation_at_send) "
        "VALUES (?, ?, ?, ?)",
        (
            candidate.opportunity_id,
            ts if ts is not None else db.now(),
            candidate.composite,
            candidate.saturation_label,
        ),
    )
    return cursor.lastrowid


def sent_today(conn: sqlite3.Connection, *, since_ts: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE sent_ts >= ?", (since_ts,)
    ).fetchone()
    return int(row["n"])


def in_quiet_hours(hour: int, quiet: tuple[int, int] | list[int]) -> bool:
    """Quiet hours wrap midnight: [23, 8] means 23:00 through 07:59."""
    start, end = int(quiet[0]), int(quiet[1])
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def candidates(conn: sqlite3.Connection, *, limit: int = 50) -> list[Candidate]:
    """Feasible, composed opportunities with their latest score, best first."""
    rows = conn.execute(
        """
        SELECT o.id AS opportunity_id, o.term_id, o.title,
               s.composite, s.saturation_label
        FROM opportunities o
        JOIN scores s ON s.id = (
            SELECT id FROM scores WHERE term_id = o.term_id
            ORDER BY ts DESC, id DESC LIMIT 1
        )
        WHERE o.composed = 1 AND o.feasible = 1
        ORDER BY s.composite DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        Candidate(
            opportunity_id=r["opportunity_id"],
            term_id=r["term_id"],
            composite=r["composite"],
            saturation_label=r["saturation_label"] or "LOW",
            title=r["title"],
        )
        for r in rows
    ]


def record_decision(
    conn: sqlite3.Connection,
    opportunity_id: int,
    action: str,
    actor_tg_id: int,
    actor_name: str,
    *,
    reason: str | None = None,
    decision_mode: str = "first_wins",
) -> tuple[bool, str]:
    """Write a decision, or report who already settled this card.

    Under `first_wins` a second tap writes nothing and answers with who decided
    and when. Overwriting would destroy exactly the disagreement M9 needs to be
    able to see -- two operators labelling one training set.
    """
    from . import db

    if action not in VALID_ACTIONS:
        return False, f"unknown action {action!r}"

    existing = settled_decision(conn, opportunity_id)
    if existing is not None and decision_mode == "first_wins":
        when = existing["ts"]
        return False, (
            f"already {existing['action']} by {existing['actor_name']} "
            f"<t:{when}>; first tap wins, so nothing was changed"
        )

    conn.execute(
        "INSERT INTO decisions (opportunity_id, action, reason, actor_tg_id, actor_name, ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (opportunity_id, action, reason, actor_tg_id, actor_name, db.now()),
    )
    return True, f"recorded {action}"
