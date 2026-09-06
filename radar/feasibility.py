"""The feasibility gate. Rules, no ML, per PROMPT.md M6 and docs/SPEC.md.

Five hard rules, all from `config/config.yaml` and `config/capabilities.yaml`:

    setup cost <= budget.max_setup_usd
    margin multiple >= budget.min_margin_multiple
    time to first dollar <= budget.max_ttfd_days
    no inventory and no physical manufacturing
    nothing else on the cannot_build list

A rejection is a *result*, not a deletion. Every failure carries a machine
code and a human sentence: the sentence goes on the card, the code becomes a
label M9 learns from. Nothing here throws away a row.

Two rules about how this gate reasons, both learned the hard way elsewhere in
this project:

* **An unknown estimate is a rejection, not a pass.** A missing setup cost is
  not a cheap setup cost. M5 shipped the same principle for an unmeasured
  model: unmeasured is not the same as good, and if it costs nothing it gets
  skipped.
* **Rejection needs positive evidence of a blocker.** The gate does not reject
  for failing to prove the team *can* build something. `can_build` is
  deliberately broad -- "anything buildable with an AI coding agent" -- so the
  operative list is `cannot_build`, matched against the text the composer
  produced. Requiring proof of capability from free-form LLM prose would
  reject nearly everything, and loudly, for no reason. Decision 27.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES_PATH = ROOT / "config" / "capabilities.yaml"
PAYMENT_RAILS_PATH = ROOT / "config" / "payment_rails.yaml"

# Blocker slug -> patterns that are evidence of it, matched against the
# composed text. Deliberately narrow: a false positive here rejects a good
# opportunity, so a pattern earns its place only if it is hard to say
# innocently. "license" is absent for that reason -- an MIT license and a
# medical license are the same word and only one is a blocker. Likewise bare
# "stock", which is also in "stock photos".
BLOCKER_PATTERNS: dict[str, tuple[str, ...]] = {
    "physical_manufacturing": (
        r"manufactur\w*", r"\bfactory\b", r"\bfactories\b", r"injection mold\w*",
        r"\bcnc\b", r"assembly line", r"\bmachining\b",
    ),
    "inventory_holding": (
        r"\binventory\b", r"\bwarehous\w+", r"\bdropship\w*", r"drop.ship\w*",
        r"\bfulfilment\b", r"\bfulfillment\b", r"\bwholesale\b", r"\brestock\w*",
        r"\bstockpil\w+", r"\bcourier\b", r"\bpostage\b",
        r"\bphysical (?:product|good)s?\b",
    ),
    "anything_needing_licensing": (
        r"\blicensed (?:professional|practitioner|therapist|doctor|attorney|agent)\b",
        r"\b(?:medical|legal|financial|broker|pharmacy|nursing) licen[cs]e\b",
        r"regulatory approval", r"\bfda\b", r"\bcertification required\b",
        r"\bbusiness permit\b", r"\bliquor licen[cs]e\b",
    ),
    "anything_needing_employees": (
        r"\bhire (?:staff|employees?|a team|workers?)\b", r"\bemployees\b",
        r"\bpayroll\b", r"\bfull.time hires?\b", r"\brecruit staff\b",
    ),
    "mobile_native": (
        r"\bios app\b", r"\bandroid app\b", r"\bnative (?:mobile )?app\b",
        r"react native", r"\bswiftui\b", r"\bkotlin\b", r"\bxcode\b",
        r"app store submission", r"\bplay store\b",
    ),
    "hardware": (
        r"\barduino\b", r"\braspberry pi\b", r"\bpcb\b", r"\bsoldering\b",
        r"\b3d print\w*", r"\bhardware (?:device|kit|build)\b", r"\bsensors?\b",
    ),
}

# Which payment rail a playbook implies. A note by default; a gate only when
# payment_rails.yaml sets enforce: true (open decision A).
RAIL_PATTERNS: dict[str, tuple[str, ...]] = {
    "gumroad": (r"\bgumroad\b",),
    "lemon_squeezy": (r"lemon ?squeezy",),
    "fiverr_payout": (r"\bfiverr\b",),
    "upwork_payout": (r"\bupwork\b",),
    "payoneer": (r"\bpayoneer\b",),
    # Not a bare "wise": it is an ordinary English word, and "a wise choice of
    # niche" would otherwise put a payment rail on the card and, once
    # enforcement is on, reject the opportunity for using it.
    "wise": (r"\bwise\.com\b", r"\btransferwise\b", r"\bwise (?:account|transfer|payout)\b"),
    # Not a bare "crypto": a crypto-*topic* product ("a crypto portfolio
    # tracker", "a newsletter about crypto") says nothing about how it gets
    # paid, and under enforcement that misreading becomes a hard rejection.
    "crypto_usdt": (r"\busdt\b", r"\busdc\b", r"\bcrypto (?:payment|wallet|checkout)s?\b",
                    r"\bpaid in crypto\b", r"\bcrypto payout\b"),
    "kaspi": (r"\bkaspi\b",),
    "stripe": (r"\bstripe\b",),
}

VALID_MODES = ("online", "offline", "hybrid")


@dataclass(frozen=True)
class Rejection:
    code: str    # stable, machine-readable; becomes an M9 label
    reason: str  # one sentence, goes on the card and into /why


@dataclass
class Verdict:
    passed: bool
    rejections: list[Rejection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    margin_multiple: float | None = None
    rail: str | None = None

    @property
    def codes(self) -> list[str]:
        return [r.code for r in self.rejections]

    @property
    def reasons(self) -> list[str]:
        return [r.reason for r in self.rejections]

    def as_row(self) -> tuple[int, str]:
        """The `feasible` and `feasible_reasons` columns of `opportunities`.

        A rejected opportunity is still a row. It is kept so the relevance
        model can learn from what was thrown out, which is impossible if the
        gate deletes its own evidence.
        """
        return int(self.passed), " | ".join(self.reasons)


@dataclass
class Capabilities:
    can_build: list[str] = field(default_factory=list)
    cannot_build: list[str] = field(default_factory=list)
    rails_enforced: bool = False
    rails_available: list[str] = field(default_factory=list)

    @classmethod
    def load(
        cls, capabilities_path: Path | None = None, rails_path: Path | None = None
    ) -> Capabilities:
        caps = _read_yaml(capabilities_path or CAPABILITIES_PATH)
        rails = _read_yaml(rails_path or PAYMENT_RAILS_PATH)
        return cls(
            can_build=list(caps.get("can_build") or []),
            cannot_build=list(caps.get("cannot_build") or []),
            rails_enforced=bool(rails.get("enforce")),
            rails_available=list(rails.get("available") or []),
        )


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def margin_multiple(price: float | None, cost_per_sale: float | None) -> float | None:
    """`price / cost_per_sale`, computed here and never taken from the LLM.

    A zero marginal cost returns infinity rather than a rejection. It is
    implausible often enough to be worth a note, but a genuinely free-to-
    deliver digital download does exist and refusing it would be wrong.

    A *negative* cost is different: it is not a cheap product, it is a broken
    estimate. Returning infinity for it would clear the margin gate and print
    "cost per sale was estimated at zero", which is not what happened. It is
    unknown, and unknown is a rejection.
    """
    if price is None or cost_per_sale is None:
        return None
    if cost_per_sale < 0:
        return None
    if cost_per_sale == 0:
        return math.inf
    return price / cost_per_sale


def _text_of(opportunity: dict) -> str:
    playbook = opportunity.get("playbook") or {}
    parts = [
        str(opportunity.get("title") or ""),
        str(playbook.get("offer") or ""),
        str(playbook.get("channel") or ""),
        " ".join(str(s) for s in (playbook.get("steps") or [])),
        " ".join(str(r) for r in (opportunity.get("requirements") or [])),
    ]
    return " ".join(parts).lower()


def _matches(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            return found.group(0)
    return None


def detect_rail(text: str) -> str | None:
    for rail, patterns in RAIL_PATTERNS.items():
        if _matches(text, patterns):
            return rail
    return None


def evaluate(opportunity: dict, cfg, capabilities: Capabilities | None = None) -> Verdict:
    """Pass/fail plus reasons for one composed opportunity.

    `opportunity` is the contract-4 dict the composer emits. Nothing here
    mutates it and nothing here writes to the database.
    """
    caps = capabilities or Capabilities.load()
    budget = cfg.get("budget", {}) or {}
    max_setup = budget.get("max_setup_usd", 100)
    min_margin = budget.get("min_margin_multiple", 3.0)
    max_ttfd = budget.get("max_ttfd_days", 7)

    rejections: list[Rejection] = []
    notes: list[str] = []
    text = _text_of(opportunity)

    # 1. Setup cost.
    setup = opportunity.get("setup_cost_usd")
    if setup is None:
        rejections.append(
            Rejection(
                "unknown_setup_cost",
                "No setup cost was estimated, so it cannot be gated.",
            )
        )
    elif setup > max_setup:
        rejections.append(
            Rejection(
                "over_budget",
                f"Setup costs ${setup:,.0f} (est.), over the ${max_setup:,.0f} cap.",
            )
        )

    # 2. Margin, computed rather than believed.
    margin = margin_multiple(
        opportunity.get("price_usd"), opportunity.get("cost_per_sale_usd")
    )
    if margin is None:
        rejections.append(
            Rejection(
                "unknown_margin",
                "Price or cost per sale is missing, so margin is unknown.",
            )
        )
    elif math.isinf(margin):
        notes.append(
            "Cost per sale was estimated at zero; margin is unbounded, which is unusual."
        )
    elif margin < min_margin:
        rejections.append(
            Rejection(
                "low_margin",
                f"Margin is {margin:.1f}x (est.), under the {min_margin:.1f}x minimum.",
            )
        )

    # 3. Time to first dollar.
    ttfd = opportunity.get("ttfd_days")
    if ttfd is None:
        rejections.append(
            Rejection(
                "unknown_ttfd",
                "No time-to-first-dollar estimate, so it cannot be gated.",
            )
        )
    elif ttfd > max_ttfd:
        rejections.append(
            Rejection(
                "too_slow",
                f"First dollar in {ttfd} days (est.), over the {max_ttfd}-day cap.",
            )
        )

    # 4 and 5. Blockers, inventory and manufacturing among them.
    for slug in caps.cannot_build:
        patterns = BLOCKER_PATTERNS.get(slug)
        if not patterns:
            continue
        hit = _matches(text, patterns)
        if hit:
            rejections.append(
                Rejection(
                    f"cannot_build:{slug}",
                    f"Needs {slug.replace('_', ' ')}, which the team cannot do "
                    f"(matched {hit!r}).",
                )
            )

    mode = opportunity.get("mode")
    if mode not in VALID_MODES:
        rejections.append(
            Rejection("invalid_mode", f"Mode {mode!r} is not one of {', '.join(VALID_MODES)}.")
        )

    # Payment rail. Off by default -- open decision A -- so it informs the
    # Requirements line without blocking, because a playbook you cannot collect
    # on is the most common reason a "first sale today" plan makes no sale.
    rail = detect_rail(text)
    if caps.rails_enforced:
        if rail is None:
            # Under enforcement a silent playbook cannot pass. Noting it and
            # waving it through would reject every playbook honest enough to
            # name Gumroad while passing every one that said nothing -- a gate
            # that rewards vagueness is worse than no gate.
            rejections.append(
                Rejection(
                    "unknown_payment_rail",
                    "No payment rail is named, so there is no way to check we can collect.",
                )
            )
        elif rail not in caps.rails_available:
            rejections.append(
                Rejection(
                    "no_payment_rail",
                    f"Collects via {rail}, which is not available to us.",
                )
            )
    elif rail:
        notes.append(f"Collects via {rail}; rails are not enforced, so verify it yourself.")

    return Verdict(
        passed=not rejections,
        rejections=rejections,
        notes=notes,
        margin_multiple=margin,
        rail=rail,
    )
