"""Load and validate config/config.yaml, with .env and environment overrides.

Precedence, lowest to highest: the YAML file, then a .env file, then the real
process environment. Secrets never live in the YAML -- it is committed as an
example and would leak.

Overrides use RADAR__<section>__<key>, e.g. RADAR__db__path=/tmp/test.db.
Values are coerced to the type already present in the YAML, so a config that
parses as an int stays an int when it arrives as an env string.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"
ENV_PREFIX = "RADAR__"

# An enabled source with no key collects nothing, which is exactly how the
# predecessor project shipped a complete YouTube collector that returned zero
# rows. Startup asserts these rather than discovering it a week later.
SOURCE_SECRETS: dict[str, tuple[str, ...]] = {
    "youtube": ("YOUTUBE_API_KEY",),
    "reddit": ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
    "product_hunt": ("PRODUCTHUNT_TOKEN",),
    "google_trends": (),
    "github": (),
    "tikwm": (),
    "hackernews": (),
}


class ConfigError(RuntimeError):
    """Raised for a config that cannot produce a correct run."""


@dataclass(frozen=True)
class Operator:
    """A human allowed to command the bot and label decisions."""

    id: int
    name: str


@dataclass
class Config:
    data: dict[str, Any]
    path: Path
    operators: list[Operator] = field(default_factory=list)
    # The environment this config was resolved against. Held rather than read
    # from os.environ later, so a caller passing an explicit environ gets an
    # answer about *that* environment -- otherwise tests silently consult the
    # developer's own .env and stop testing anything.
    environ: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, section: str) -> Any:
        return self.data[section]

    def get(self, dotted: str, default: Any = None) -> Any:
        """Read a nested value: cfg.get('alerts.max_per_day')."""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def db_path(self) -> Path:
        raw = Path(self.get("db.path", "data/radar.db"))
        return raw if raw.is_absolute() else ROOT / raw

    def is_operator(self, telegram_user_id: int) -> bool:
        """The bot's only access control -- a group is joinable by anyone."""
        return any(op.id == telegram_user_id for op in self.operators)

    def operator_name(self, telegram_user_id: int) -> str | None:
        for op in self.operators:
            if op.id == telegram_user_id:
                return op.name
        return None

    def enabled_sources(self) -> list[str]:
        sources = self.get("sources", {}) or {}
        return [name for name, conf in sources.items() if (conf or {}).get("enabled")]

    def missing_secrets(self) -> dict[str, list[str]]:
        """Enabled sources mapped to the env vars they need and do not have."""
        missing: dict[str, list[str]] = {}
        for source in self.enabled_sources():
            absent = [k for k in SOURCE_SECRETS.get(source, ()) if not self.environ.get(k)]
            if absent:
                missing[source] = absent
        return missing


def load_dotenv(path: Path | None = None) -> None:
    """Read .env into the environment without overwriting what is already set."""
    dotenv = path or ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _coerce(value: str, like: Any) -> Any:
    if isinstance(like, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(like, int) and not isinstance(like, bool):
        return int(value)
    if isinstance(like, float):
        return float(value)
    return value


def _resolve_key(node: dict[str, Any], part: str) -> str:
    """Match a config key case-insensitively.

    Windows uppercases every name in os.environ, so RADAR__sources__github
    arrives as RADAR__SOURCES__GITHUB. Matching case-sensitively created a
    second, uppercase section and left the real setting untouched -- an
    override that reported success and changed nothing.
    """
    if part in node:
        return part
    lowered = part.lower()
    for existing in node:
        if existing.lower() == lowered:
            return existing
    return part


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> None:
    for key, value in environ.items():
        if not key.upper().startswith(ENV_PREFIX.upper()):
            continue
        parts = key[len(ENV_PREFIX) :].split("__")
        node: Any = data
        for part in parts[:-1]:
            resolved = _resolve_key(node, part)
            if not isinstance(node.get(resolved), dict):
                node[resolved] = {}
            node = node[resolved]
        leaf = _resolve_key(node, parts[-1])
        node[leaf] = _coerce(value, node.get(leaf))


def _parse_operators(data: dict[str, Any]) -> list[Operator]:
    raw = (data.get("telegram") or {}).get("operators") or []
    operators: list[Operator] = []
    for entry in raw:
        try:
            op = Operator(id=int(entry["id"]), name=str(entry["name"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"telegram.operators entry is malformed: {entry!r}") from exc
        if op.id == 0:
            # The placeholder from config.example.yaml. Shipping it would mean
            # an operator who can never be recognised and whose decisions
            # cannot be attributed.
            raise ConfigError(
                f"telegram.operators has an unfilled placeholder for {op.name!r}. "
                "Run scripts/telegram_setup.py to get the real user ID."
            )
        operators.append(op)
    if not operators:
        raise ConfigError("telegram.operators is empty; nobody could command the bot.")
    return operators


def load(path: Path | str | None = None, *, environ: dict[str, str] | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        raise ConfigError(
            f"{cfg_path} not found. Copy config/config.example.yaml to config/config.yaml."
        )

    if environ is None:
        # Real run: .env feeds the process environment.
        load_dotenv()
        resolved = dict(os.environ)
    else:
        # Explicit environment: use exactly what the caller gave, and do not
        # let a local .env leak into it.
        resolved = dict(environ)

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{cfg_path} did not parse to a mapping.")
    _apply_env_overrides(data, resolved)
    return Config(
        data=data, path=cfg_path, operators=_parse_operators(data), environ=resolved
    )
