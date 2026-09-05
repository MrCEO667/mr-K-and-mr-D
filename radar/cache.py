"""Day-scoped response cache.

Collector rule 4: same term, same day, same source = one call. Trends and the
GitHub search API both punish repetition, and a re-run while debugging should
not cost quota that the scheduled run needs.

Keys are scoped by date, so the cache expires on its own without a sweeper.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "cache"


class Cache:
    def __init__(self, directory: Path | None = None, *, enabled: bool = True) -> None:
        self.directory = Path(directory) if directory else DEFAULT_DIR
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def _path(self, source: str, key: str) -> Path:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        return self.directory / source / day / f"{digest}.json"

    def get(self, source: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(source, key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt cache entry is a miss, never a crash.
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, source: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(source, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Caching is an optimisation; never let it fail a run.
        with contextlib.suppress(OSError, TypeError):
            path.write_text(json.dumps(value), encoding="utf-8")
