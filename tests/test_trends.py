"""Google Trends collector, against a fake client. Never touches the network."""
import pandas as pd
import pytest

from radar.collectors.base import RateLimiter, SourceUnavailable, Term
from radar.collectors.trends import ANCHOR_TERM, TrendsCollector

TERMS = [
    Term(id=1, term="ai voice clone", normalized="ai voice clone"),
    Term(id=2, term="notion template", normalized="notion template"),
]


class FakeTrends:
    """Mimics pytrends: build_payload then interest_over_time."""

    def __init__(self, frames=None, raises=None):
        self.frames = frames or {}
        self.raises = raises
        self.payloads = []
        self._current = None

    def build_payload(self, kw_list, timeframe=None, geo=None):
        if self.raises:
            raise self.raises
        self.payloads.append(list(kw_list))
        self._current = kw_list

    def interest_over_time(self):
        return self.frames.get(tuple(self._current))


def frame(index_len=3, **columns):
    idx = pd.date_range("2026-01-01", periods=index_len, freq="D", tz="UTC")
    return pd.DataFrame(columns, index=idx)


def collector(client):
    return TrendsCollector(
        client=client, rate_limiter=RateLimiter(min_interval_s=0, sleep=lambda s: None)
    )


def test_the_anchor_is_included_in_every_request():
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    client = FakeTrends(
        {
            key: frame(
                **{
                    ANCHOR_TERM: [50] * 3,
                    "ai voice clone": [10, 20, 30],
                    "notion template": [5] * 3,
                }
            )
        }
    )
    collector(client).collect(TERMS, "run-1")
    assert client.payloads[0][0] == ANCHOR_TERM


def test_values_are_rescaled_against_the_anchor():
    # Anchor mean 50, term value 25 -> 0.5. Without this, two requests'
    # numbers would not share a scale and M5 would train on noise.
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    client = FakeTrends(
        {
            key: frame(
                **{ANCHOR_TERM: [50] * 3, "ai voice clone": [25] * 3, "notion template": [100] * 3}
            )
        }
    )
    result = collector(client).collect(TERMS, "run-1")
    by_term = {r.term_id: r.value for r in result.readings}
    assert by_term[1] == pytest.approx(0.5)
    assert by_term[2] == pytest.approx(2.0)


def test_the_anchor_itself_is_never_stored():
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    client = FakeTrends(
        {
            key: frame(
                **{ANCHOR_TERM: [50] * 3, "ai voice clone": [25] * 3, "notion template": [1] * 3}
            )
        }
    )
    result = collector(client).collect(TERMS, "run-1")
    assert {r.term_id for r in result.readings} == {1, 2}


def test_a_flat_anchor_is_refused_rather_than_stored_wrong():
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    client = FakeTrends(
        {
            key: frame(
                **{ANCHOR_TERM: [0] * 3, "ai voice clone": [25] * 3, "notion template": [1] * 3}
            )
        }
    )
    with pytest.raises(SourceUnavailable, match="unscalable"):
        collector(client).collect(TERMS, "run-1")


def test_a_term_trends_refuses_to_return_is_partial_not_fatal():
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    # Trends drops low-volume terms from the frame entirely.
    client = FakeTrends({key: frame(**{ANCHOR_TERM: [50] * 3, "ai voice clone": [25] * 3})})
    result = collector(client).collect(TERMS, "run-1")
    assert result.partial
    assert any("notion template" in e for e in result.errors)
    assert [r.term_id for r in result.readings] == [1, 1, 1]


def test_a_dead_source_raises_source_unavailable():
    client = FakeTrends(raises=RuntimeError("google said no"))
    with pytest.raises(SourceUnavailable):
        collector(client).collect(TERMS, "run-1")


def test_health_reports_degraded_when_some_terms_failed():
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    client = FakeTrends({key: frame(**{ANCHOR_TERM: [50] * 3, "ai voice clone": [25] * 3})})
    c = collector(client)
    c.collect(TERMS, "run-1")
    assert c.health().status == "degraded"


def test_health_is_ok_when_everything_returned():
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    client = FakeTrends(
        {
            key: frame(
                **{ANCHOR_TERM: [50] * 3, "ai voice clone": [25] * 3, "notion template": [5] * 3}
            )
        }
    )
    c = collector(client)
    c.collect(TERMS, "run-1")
    assert c.health().status == "ok"


def test_empty_term_list_makes_no_requests():
    client = FakeTrends()
    assert collector(client).collect([], "run-1").readings == []
    assert client.payloads == []


def test_a_flat_zero_term_is_refused_not_stored():
    # Measured against too popular an anchor, a real term returns all zeros.
    # Storing that is 90 days of fabricated silence, so it must be a term
    # failure instead.
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    client = FakeTrends(
        {
            key: frame(
                **{ANCHOR_TERM: [50] * 3, "ai voice clone": [0] * 3, "notion template": [5] * 3}
            )
        }
    )
    c = collector(client)
    result = c.collect(TERMS, "run-1")
    assert result.partial
    assert any("flat zero" in e for e in result.errors)
    assert {r.term_id for r in result.readings} == {2}
    assert c.health().status == "degraded"


def test_incomplete_final_period_is_dropped():
    # Trends marks the current period isPartial and revises it later.
    key = (ANCHOR_TERM, "ai voice clone", "notion template")
    f = frame(
        **{ANCHOR_TERM: [50] * 3, "ai voice clone": [25] * 3, "notion template": [5] * 3}
    )
    f["isPartial"] = [False, False, True]
    result = collector(FakeTrends({key: f})).collect(TERMS, "run-1")
    assert len([r for r in result.readings if r.term_id == 1]) == 2


def test_one_throttled_batch_does_not_abort_the_others():
    # Trends 429s routinely; the batch after a backoff usually succeeds.
    terms = [Term(id=i, term=f"term {i}", normalized=f"term {i}") for i in range(1, 9)]
    good = {}
    for start in (1, 5):
        names = [f"term {i}" for i in range(start, start + 4)]
        columns = {ANCHOR_TERM: [50] * 3, **{n: [25] * 3 for n in names}}
        good[(ANCHOR_TERM, *names)] = frame(**columns)

    class Flaky(FakeTrends):
        def __init__(self, frames):
            super().__init__(frames)
            self.calls = 0

        def build_payload(self, kw_list, timeframe=None, geo=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("429 too many requests")
            super().build_payload(kw_list, timeframe=timeframe, geo=geo)

    c = TrendsCollector(
        client=Flaky(good),
        rate_limiter=RateLimiter(min_interval_s=0, max_retries=1, sleep=lambda s: None),
    )
    result = c.collect(terms, "run-1")
    assert result.partial
    assert result.readings  # the second batch still landed
    assert c.health().status == "degraded"


def test_every_batch_failing_is_a_dead_source():
    terms = [Term(id=1, term="a", normalized="a")]
    client = FakeTrends(raises=RuntimeError("google said no"))
    c = TrendsCollector(
        client=client,
        rate_limiter=RateLimiter(min_interval_s=0, max_retries=1, sleep=lambda s: None),
    )
    with pytest.raises(SourceUnavailable, match="all 1 batches failed"):
        c.collect(terms, "run-1")


def test_a_term_equal_to_the_anchor_is_never_sent_twice():
    # Trends rejects a request carrying the same keyword twice, which cost a
    # whole batch the first time this ran against the live API.
    terms = [
        Term(id=1, term=ANCHOR_TERM, normalized=ANCHOR_TERM),
        Term(id=2, term="ai voice clone", normalized="ai voice clone"),
    ]
    key = (ANCHOR_TERM, "ai voice clone")
    client = FakeTrends({key: frame(**{ANCHOR_TERM: [50] * 3, "ai voice clone": [25] * 3})})
    result = collector(client).collect(terms, "run-1")
    assert client.payloads == [[ANCHOR_TERM, "ai voice clone"]]
    assert {r.term_id for r in result.readings} == {2}
