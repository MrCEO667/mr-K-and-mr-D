"""HTTP transport shared by the collectors.

Collector rules 2 and 3: an honest User-Agent that identifies the project, and
exponential backoff on 429/503. Nothing here pretends to be a browser -- a
source that blocks this User-Agent is a source we treat as unavailable, not one
we sneak past.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..cache import Cache
from .base import RateLimiter, SourceUnavailable

USER_AGENT = "nicheradar/0.1 (+https://github.com/MrCEO667/mr-K-and-mr-D)"
RETRYABLE_STATUS = (429, 500, 502, 503, 504)


class RetryableHTTPError(RuntimeError):
    """A status worth backing off for, as opposed to one worth giving up on."""


class JsonHttp:
    def __init__(
        self,
        source: str,
        *,
        rate_limiter: RateLimiter | None = None,
        cache: Cache | None = None,
        timeout_s: float = 25.0,
        opener=urllib.request.urlopen,
    ) -> None:
        self.source = source
        self.rate_limiter = rate_limiter or RateLimiter(min_interval_s=1.0)
        self.cache = cache if cache is not None else Cache()
        self.timeout_s = timeout_s
        self._opener = opener
        self.last_status: int | None = None

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url

        cached = self.cache.get(self.source, full)
        if cached is not None:
            return cached

        def call() -> Any:
            request = urllib.request.Request(
                full, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            try:
                with self._opener(request, timeout=self.timeout_s) as response:
                    self.last_status = getattr(response, "status", 200)
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                self.last_status = exc.code
                if exc.code in RETRYABLE_STATUS:
                    raise RetryableHTTPError(f"HTTP {exc.code} from {self.source}") from exc
                # 403/404 will not improve by asking again.
                raise SourceUnavailable(f"HTTP {exc.code} from {self.source}: {full}") from exc

        payload = self.rate_limiter.call(call, describe=f"{self.source} GET")
        self.cache.put(self.source, full, payload)
        return payload
