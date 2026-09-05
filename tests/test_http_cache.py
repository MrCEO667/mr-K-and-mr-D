"""Transport rules: honest UA, backoff on 429, no retry on a verdict, and the
same term twice in a day costing one call."""
import io
import json
import urllib.error

import pytest

from radar.cache import Cache
from radar.collectors.base import RateLimiter, SourceUnavailable
from radar.collectors.http import USER_AGENT, JsonHttp


def response(payload):
    class R(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return R(json.dumps(payload).encode())


def http(opener, tmp_path, **kw):
    return JsonHttp(
        "test",
        rate_limiter=RateLimiter(min_interval_s=0, sleep=lambda s: None, **kw),
        cache=Cache(tmp_path / "cache"),
        opener=opener,
    )


def test_the_user_agent_identifies_the_project(tmp_path):
    seen = {}

    def opener(request, timeout=None):
        seen["ua"] = request.get_header("User-agent")
        return response({"ok": True})

    http(opener, tmp_path).get("https://example.test/x")
    # No browser impersonation: a source that blocks this is treated as down.
    assert seen["ua"] == USER_AGENT
    assert "nicheradar" in seen["ua"]


def test_a_429_is_retried(tmp_path):
    calls = []

    def opener(request, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError("u", 429, "slow down", {}, None)
        return response({"ok": True})

    assert http(opener, tmp_path).get("https://example.test/x") == {"ok": True}
    assert len(calls) == 2


def test_a_403_is_not_retried(tmp_path):
    calls = []

    def opener(request, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)

    with pytest.raises(SourceUnavailable, match="403"):
        http(opener, tmp_path, max_retries=4).get("https://example.test/x")
    # Retrying a verdict only burns the backoff budget the next source needs.
    assert len(calls) == 1


def test_the_same_request_twice_in_a_day_costs_one_call(tmp_path):
    calls = []

    def opener(request, timeout=None):
        calls.append(1)
        return response({"n": 1})

    client = http(opener, tmp_path)
    client.get("https://example.test/x", params={"q": "ai voice clone"})
    client.get("https://example.test/x", params={"q": "ai voice clone"})
    assert len(calls) == 1


def test_a_corrupt_cache_entry_is_a_miss_not_a_crash(tmp_path):
    cache = Cache(tmp_path / "cache")
    cache.put("test", "key", {"a": 1})
    path = cache._path("test", "key")
    path.write_text("{not json", encoding="utf-8")
    assert cache.get("test", "key") is None
