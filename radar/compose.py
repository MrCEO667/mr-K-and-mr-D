"""The composer. Local qwen3:8b turns a scored term into a playbook.

PROMPT.md M7 draws one line and this module exists to enforce it:

* **Measured numbers** -- trend deltas, post counts, view sums, supply counts --
  are read from SQLite and template-injected. The LLM never produces them and
  never sees a slot where it could.
* **Estimated numbers** -- price, cost per sale, days to first dollar -- are LLM
  output, stored with a confidence, and always rendered with `(est.)`.

The enforcement is not the prompt. A prompt is a request, and an 8B model will
occasionally answer a request with "YouTube views are up 340%" regardless of
what it was asked. So the JSON schema has no field a measured number could land
in, and `find_measured_claims()` re-reads the prose afterwards: a measured
figure that contradicts the database discards the generation and retries.

Failure is bounded. Two retries on a parse or validation failure, then the row
is written with `composed=0` and the run moves on. A malformed generation must
never block a sweep.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import db, feasibility, log
from .score import Score

MAX_TITLE_CHARS = 60

# First attempt is near-deterministic for parseable JSON. Retrying at the same
# temperature would mostly reproduce the same rejected answer -- observed with
# the placeholder echo, which is stable across identical calls -- so each retry
# gets warmer to break out of it.
BASE_TEMPERATURE = 0.3
RETRY_TEMPERATURE_STEP = 0.25
VALID_MODES = feasibility.VALID_MODES

# Numbers the LLM is never allowed to assert. Each maps prose keywords to the
# measured value in the bundle, so a contradiction can be caught rather than
# merely discouraged.
MEASURED_KEYWORDS: dict[str, tuple[str, ...]] = {
    "view_sum": ("view", "views"),
    "video_count": ("video", "videos"),
    "post_count": ("post", "posts", "thread", "threads"),
    "stars": ("star", "stars"),
    "launch_count": ("launch", "launches"),
    "supply_total": ("competitor", "competitors", "listing", "listings", "seller", "sellers"),
    "demand_growth_pct": ("% ", "percent", "%"),
}

# A number next to one of those words is a claim about a measured quantity.
_CLAIM = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent)?\s*([a-z%]+)", re.IGNORECASE
)

PROMPT_TEMPLATE = """You are helping two people find a small online business \
they can start this week.

The niche is: {term}

Write ONE concrete way to make money from this niche.

Hard rules:
- Return ONLY a JSON object. No prose before or after, no markdown fence.
- Do NOT state any statistics, counts, percentages, view numbers, post numbers
  or growth figures anywhere in your text. Those are measured separately and
  your numbers would be wrong. Describe the offer, not the market size.
- The team can build: {can_build}.
- "channel" is where a buyer finds you and pays: a marketplace or a storefront,
  for example Gumroad, Fiverr, Etsy, Upwork, or your own landing page. It is
  NOT a technology and NOT an item from the build list above.
- The team CANNOT do: {cannot_build}. Do not propose anything needing these.
- Two people are building this as a side project with no outside funding, so
  it has to be something small they can start alone. Estimate the real numbers
  for the idea you are proposing -- do not tune them to look acceptable.

JSON shape, exactly these keys. Every <...> is a description of what to put
there, not a value to copy:
{{
  "title": "<under 60 characters, names the product not the niche>",
  "mode": "<online, offline or hybrid>",
  "playbook": {{
    "offer": "<what is sold, one sentence>",
    "channel": "<the marketplace or storefront where a buyer pays>",
    "steps": ["<first thing to do>", "<second>", "<third>"]
  }},
  "requirements": ["<account or tool needed>", "<another>"],
  "setup_cost_usd": <number, one-time dollars spent before the first sale>,
  "price_usd": <number, what one buyer pays>,
  "cost_per_sale_usd": <number, your marginal cost to deliver one>,
  "ttfd_days": <whole number, days until the first dollar>,
  "confidence": <number between 0 and 1, how sure you are of these estimates>
}}"""


ESTIMATE_FIELDS = (
    "setup_cost_usd",
    "price_usd",
    "cost_per_sale_usd",
    "ttfd_days",
    "confidence",
)


def anchored_estimates(template: str = "") -> list[str]:
    """Estimate fields whose value in the prompt is a literal number.

    An 8B model treats an example number as the answer. Shown
    `"setup_cost_usd": 12.0`, qwen3:8b returned 12.0 / 29.0 / 3.4 / 2 / 0.6 for
    "ai voice clone" on every attempt at three different temperatures -- five
    fabricated figures that would then be stored as estimates and printed on a
    card with "(est.)" beside them. The template describes the fields instead,
    and this function exists so a test can keep it that way.
    """
    template = template or PROMPT_TEMPLATE
    anchored = []
    for field_name in ESTIMATE_FIELDS:
        match = re.search(rf'"{field_name}"\s*:\s*([^,\n]+)', template)
        if match and re.match(r"^-?\d", match.group(1).strip()):
            anchored.append(field_name)
    return anchored


class ComposeError(RuntimeError):
    """A generation that cannot be used. Always caught; never ends a run."""


@dataclass
class MeasuredBundle:
    """Everything measured about a term. Injected into the card, never into
    the model's mouth."""

    term: str
    term_id: int
    demand_growth_pct: float = 0.0
    supply_total: int = 0
    saturation_label: str = "LOW"
    per_source: dict[str, float] = field(default_factory=dict)

    def value_for(self, metric: str) -> float | None:
        if metric == "supply_total":
            return float(self.supply_total)
        if metric == "demand_growth_pct":
            return self.demand_growth_pct
        return self.per_source.get(metric)


@dataclass
class Composition:
    term_id: int
    payload: dict[str, Any] | None
    verdict: feasibility.Verdict | None
    composed: bool
    attempts: int
    errors: list[str] = field(default_factory=list)
    model: str = ""


class Ollama:
    """Minimal client. POST /api/generate, no streaming, no dependency.

    `opener` is injectable so tests never touch the network -- conftest blocks
    the real one anyway.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3:8b",
        *,
        timeout_s: float = 120.0,
        opener=urllib.request.urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._opener = opener

    def generate(self, prompt: str, *, temperature: float = BASE_TEMPERATURE) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                # Low by default: this is structured extraction, not a creative
                # task, and every degree of randomness is another chance of
                # unparseable JSON. compose_one raises it on retry.
                "options": {"temperature": temperature},
                "think": False,
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                return json.load(response).get("response", "")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ComposeError(f"ollama unreachable: {exc}") from exc


def extract_json(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a generation.

    Small models fence their output, prepend "Here is the JSON:", or leave a
    <think> block in front of it even when asked not to. Retrying on that would
    burn an attempt on a fixable formatting habit, so the object is located
    rather than demanded.
    """
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    if start == -1:
        raise ComposeError("no JSON object in the generation")
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise ComposeError(f"malformed JSON: {exc}") from exc
    raise ComposeError("unterminated JSON object")


def validate_shape(payload: Any) -> dict[str, Any]:
    """Contract 4, checked before anything downstream trusts a key exists."""
    if not isinstance(payload, dict):
        raise ComposeError(f"expected an object, got {type(payload).__name__}")

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ComposeError("title is missing or empty")
    if len(title) > MAX_TITLE_CHARS:
        raise ComposeError(f"title is {len(title)} chars, over {MAX_TITLE_CHARS}")

    if payload.get("mode") not in VALID_MODES:
        raise ComposeError(f"mode {payload.get('mode')!r} is not one of {VALID_MODES}")

    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        raise ComposeError("playbook is missing")
    for key in ("offer", "channel"):
        if not isinstance(playbook.get(key), str) or not playbook[key].strip():
            raise ComposeError(f"playbook.{key} is missing or empty")
    steps = playbook.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, str) for s in steps):
        raise ComposeError("playbook.steps must be a non-empty list of strings")

    requirements = payload.get("requirements")
    if requirements is not None and (
        not isinstance(requirements, list) or not all(isinstance(r, str) for r in requirements)
    ):
        raise ComposeError("requirements must be a list of strings")

    for key in ("setup_cost_usd", "price_usd", "cost_per_sale_usd", "confidence"):
        value = payload.get(key)
        if value is not None and not isinstance(value, (int, float)):
            raise ComposeError(f"{key} must be a number, got {value!r}")
    ttfd = payload.get("ttfd_days")
    if ttfd is not None and not isinstance(ttfd, int):
        raise ComposeError(f"ttfd_days must be an integer, got {ttfd!r}")

    # A measured field has no home in the contract; one appearing means the
    # model invented a slot for the numbers it was told not to produce.
    for forbidden in ("view_sum", "post_count", "stars", "demand_growth", "saturation"):
        if forbidden in payload:
            raise ComposeError(f"payload carries the measured field {forbidden!r}")
    return payload


def prose_of(payload: dict[str, Any]) -> str:
    playbook = payload.get("playbook") or {}
    parts = [
        str(payload.get("title") or ""),
        str(playbook.get("offer") or ""),
        str(playbook.get("channel") or ""),
        " ".join(str(s) for s in (playbook.get("steps") or [])),
        " ".join(str(r) for r in (payload.get("requirements") or [])),
    ]
    return " ".join(parts)


def find_measured_claims(text: str) -> list[tuple[str, float]]:
    """Numbers in prose that assert a measured quantity.

    Returns (metric, number) pairs. Detection is keyword-adjacency rather than
    anything clever: "340% growth", "47 posts", "2 launches". It will miss a
    creative phrasing, which is why the schema also gives those numbers nowhere
    to go -- two weak defences on different failure modes rather than one.
    """
    claims: list[tuple[str, float]] = []
    lowered = text.lower()
    for match in _CLAIM.finditer(lowered):
        number_text, word = match.group(1), match.group(2)
        try:
            number = float(number_text.replace(",", ""))
        except ValueError:
            continue
        # Read the whole match, not just the trailing word. "900 percent" at
        # the end of a string leaves word="percent", but "900 percent growth"
        # leaves word="growth" and the marker only survives in group(0).
        matched = match.group(0)
        is_percent = "%" in matched or "percent" in matched
        for metric, keywords in MEASURED_KEYWORDS.items():
            if metric == "demand_growth_pct":
                if is_percent:
                    claims.append((metric, number))
                continue
            if word in keywords:
                claims.append((metric, number))
    return claims


def check_against_measurements(
    payload: dict[str, Any], bundle: MeasuredBundle, *, tolerance: float = 0.01
) -> list[str]:
    """Contradictions between generated prose and the database.

    A measured figure the model got *right* is still not its job, but it is not
    a lie either, so only disagreement discards the generation. Anything the
    bundle cannot speak to is ignored rather than guessed at.
    """
    problems = []
    for metric, claimed in find_measured_claims(prose_of(payload)):
        actual = bundle.value_for(metric)
        if actual is None:
            continue
        if abs(actual) < tolerance and abs(claimed) < tolerance:
            continue
        if abs(claimed - actual) > max(tolerance, abs(actual) * tolerance):
            problems.append(
                f"prose claims {claimed:g} for {metric}, the database says {actual:g}"
            )
    return problems


# Budget thresholds the gate enforces. The prompt must not contain them: told
# "setup under $100", qwen3:8b returned $20 for a term it valued at $500 when
# not told, so the estimate became our own config read back to us and the gate
# passed 11 of 11 because the model had been handed the passing criteria.
GATE_THRESHOLD_FIELDS = ("max_setup", "min_margin", "max_ttfd")


def leaks_gate_thresholds(template: str = "") -> list[str]:
    """Gate thresholds that appear in the prompt. Should always be empty."""
    template = template or PROMPT_TEMPLATE
    return [name for name in GATE_THRESHOLD_FIELDS if "{" + name + "}" in template]


def build_prompt(bundle: MeasuredBundle, cfg, caps: feasibility.Capabilities) -> str:
    """The prompt deliberately does not carry the budget caps.

    The gate exists to reject; a model told what the gate wants will produce it,
    and a card would then print our own thresholds back at us with "(est.)"
    beside them. Feasibility is judged after the estimate, not before it.
    """
    return PROMPT_TEMPLATE.format(
        term=bundle.term,
        can_build=", ".join(caps.can_build[:12]) or "software",
        cannot_build=", ".join(caps.cannot_build) or "nothing",
    )


def bundle_for(conn: sqlite3.Connection, score: Score, term: str) -> MeasuredBundle:
    """Measured values for one term, straight from the database."""
    per_source: dict[str, float] = {}
    rows = conn.execute(
        "SELECT metric, value FROM signal_snapshots WHERE term_id = ? "
        "AND ts = (SELECT MAX(ts) FROM signal_snapshots WHERE term_id = ? AND metric = "
        "signal_snapshots.metric)",
        (score.term_id, score.term_id),
    ).fetchall()
    for row in rows:
        per_source[row["metric"]] = float(row["value"])
    return MeasuredBundle(
        term=term,
        term_id=score.term_id,
        demand_growth_pct=score.demand_growth * 100.0,
        supply_total=score.saturation_raw,
        saturation_label=score.saturation_label,
        per_source=per_source,
    )


def compose_one(
    conn: sqlite3.Connection,
    cfg,
    score: Score,
    term: str,
    client: Ollama,
    *,
    caps: feasibility.Capabilities | None = None,
    max_retries: int | None = None,
) -> Composition:
    """One term -> one composed, gated opportunity.

    Never raises. A generation that will not parse, will not validate, or
    contradicts the database is retried; when the retries are gone the row is
    written with `composed=0` so the failure is visible and the run continues.
    """
    caps = caps or feasibility.Capabilities.load()
    retries = max_retries if max_retries is not None else int(cfg.get("llm.max_retries", 2))
    logger = log.get(__name__, term_id=score.term_id)

    bundle = bundle_for(conn, score, term)
    prompt = build_prompt(bundle, cfg, caps)
    errors: list[str] = []

    for attempt in range(1, retries + 2):
        temperature = BASE_TEMPERATURE + RETRY_TEMPERATURE_STEP * (attempt - 1)
        try:
            raw = client.generate(prompt, temperature=temperature)
            payload = validate_shape(extract_json(raw))
            contradictions = check_against_measurements(payload, bundle)
            if contradictions:
                raise ComposeError("; ".join(contradictions))
        except ComposeError as exc:
            errors.append(f"attempt {attempt}: {exc}")
            logger.warning(
                "generation rejected",
                extra={"attempt": attempt, "temperature": temperature, "error": str(exc)},
            )
            continue

        verdict = feasibility.evaluate(payload, cfg, caps)
        logger.info(
            "composed",
            extra={"attempt": attempt, "feasible": verdict.passed, "title": payload["title"]},
        )
        return Composition(
            term_id=score.term_id,
            payload=payload,
            verdict=verdict,
            composed=True,
            attempts=attempt,
            errors=errors,
            model=client.model,
        )

    logger.error("composition failed", extra={"attempts": retries + 1, "errors": errors})
    return Composition(
        term_id=score.term_id,
        payload=None,
        verdict=None,
        composed=False,
        attempts=retries + 1,
        errors=errors,
        model=client.model,
    )


def write_opportunity(
    conn: sqlite3.Connection, run_id: str, composition: Composition
) -> int | None:
    """Persist a composition, composed or not.

    A failed generation is still a row. Deleting it would hide a composer that
    has quietly stopped working, and M9 learns from what was rejected.

    No commit here: `db.run` owns the transaction, and committing early would
    break --dry-run's promise to write nothing.
    """
    payload = composition.payload or {}
    verdict = composition.verdict
    feasible, reasons = verdict.as_row() if verdict else (0, "not composed")
    margin = verdict.margin_multiple if verdict else None
    if margin is not None and margin == float("inf"):
        margin = None

    cursor = conn.execute(
        "INSERT INTO opportunities (term_id, run_id, title, mode, playbook_json, "
        "requirements_json, setup_cost_usd, price_usd, cost_per_sale_usd, "
        "margin_multiple, ttfd_days, confidence, feasible, feasible_reasons, "
        "composed, llm_model, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            composition.term_id,
            run_id,
            payload.get("title") or "(not composed)",
            payload.get("mode") or "online",
            json.dumps(payload.get("playbook") or {}, ensure_ascii=False),
            json.dumps(payload.get("requirements") or [], ensure_ascii=False),
            payload.get("setup_cost_usd"),
            payload.get("price_usd"),
            payload.get("cost_per_sale_usd"),
            margin,
            payload.get("ttfd_days"),
            payload.get("confidence"),
            feasible,
            reasons,
            int(composition.composed),
            composition.model,
            db.now(),
        ),
    )
    return cursor.lastrowid
