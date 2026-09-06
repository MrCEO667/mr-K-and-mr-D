"""Rendering an opportunity as the card the operators read.

contracts.md section 5 sets one rule above the layout: **estimates and
measurements must be visually distinguishable, and every estimated figure
carries `(est.)`.** That is the whole reason this is a module with tests rather
than an f-string in the bot -- the separation the composer enforces in code is
worth nothing if the card blurs it again on the way out.

Measured lines come from SQLite. Estimated lines come from the LLM and are
marked. A number whose provenance is unknown is not printed at all.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

ESTIMATE_MARK = "(est.)"

SATURATION_ORDER = {"LOW": 0, "MED": 1, "HIGH": 2}


@dataclass
class Card:
    opportunity_id: int
    title: str
    mode: str
    durability_line: str
    saturation_line: str
    economics_line: str
    evidence_line: str
    requirements_line: str
    playbook_lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    feasible: bool = True
    feasible_reasons: str = ""

    def render(self) -> str:
        head = f"🔥 {self.title}"
        badge = f"[{self.mode.upper()}]"
        lines = [f"{head}  {badge}", self.durability_line, self.economics_line, ""]
        if self.evidence_line:
            lines += [f"Evidence: {self.evidence_line}"]
        if self.requirements_line:
            lines += [f"Needs: {self.requirements_line}"]
        if self.playbook_lines:
            lines += ["Play: " + self.playbook_lines[0]]
            lines += [f"  {i}. {step}" for i, step in enumerate(self.playbook_lines[1:], 1)]
        if not self.feasible:
            lines += ["", f"❌ Gated: {self.feasible_reasons}"]
        if self.notes:
            lines += [""] + [f"· {note}" for note in self.notes]
        return "\n".join(line for line in lines if line is not None)


def _money(value) -> str:
    if value is None:
        return "?"
    return f"${value:,.0f}" if float(value) >= 10 else f"${float(value):.2f}"


def durability_line(score_row, horizon: int) -> str:
    """Measured. Names the scorer, because the model is refused at some
    horizons and a card must never imply otherwise."""
    value = score_row[f"durability_{horizon}"] if score_row else None
    scorer = (score_row["scorer"] if score_row else "") or ""
    shown = "?" if value is None else f"{value:.2f}"
    label = "momentum" if "momentum_fallback" in scorer else "model"
    saturation = (score_row["saturation_label"] if score_row else "?") or "?"
    return f"Durability {shown} (+{horizon}d, {label}) · Saturation {saturation}"


def economics_line(row) -> str:
    """Every figure here is an LLM estimate and every one is marked."""
    margin = row["margin_multiple"]
    margin_text = "?" if margin is None else f"{margin:.1f}x"
    setup = _money(row["setup_cost_usd"])
    ttfd = row["ttfd_days"]
    ttfd_text = "?" if ttfd is None else f"{ttfd}d"
    return (
        f"Margin ~{margin_text} {ESTIMATE_MARK} · "
        f"Setup {setup} {ESTIMATE_MARK} · "
        f"First sale {ttfd_text} {ESTIMATE_MARK}"
    )


def evidence_line(score_row) -> str:
    """Measured only. Says 'not counted' rather than implying a zero."""
    if score_row is None:
        return ""
    parts = []
    demand = score_row["demand_growth"]
    if demand:
        parts.append(f"Trends {demand * 100:+.0f}% 7d")
    raw = score_row["saturation_raw"]
    parts.append(f"{raw:,} competing listings" if raw else "supply not counted")
    supply = score_row["supply_growth"]
    if supply:
        parts.append(f"supply {supply * 100:+.0f}%")
    return " · ".join(parts)


def build(conn: sqlite3.Connection, opportunity_id: int, horizon: int = 60) -> Card | None:
    row = conn.execute(
        "SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)
    ).fetchone()
    if row is None:
        return None

    score_row = conn.execute(
        "SELECT * FROM scores WHERE term_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (row["term_id"],),
    ).fetchone()

    playbook = json.loads(row["playbook_json"] or "{}")
    requirements = json.loads(row["requirements_json"] or "[]")
    steps = playbook.get("steps") or []
    offer = playbook.get("offer") or ""
    channel = playbook.get("channel") or ""

    play = []
    if offer or channel:
        play.append(" ".join(part for part in (offer, f"via {channel}" if channel else "") if part))
    play += [str(s) for s in steps]

    return Card(
        opportunity_id=row["id"],
        title=row["title"],
        mode=row["mode"],
        durability_line=durability_line(score_row, horizon),
        saturation_line="",
        economics_line=economics_line(row),
        evidence_line=evidence_line(score_row),
        requirements_line=", ".join(str(r) for r in requirements),
        playbook_lines=play,
        feasible=bool(row["feasible"]),
        feasible_reasons=row["feasible_reasons"] or "",
    )
