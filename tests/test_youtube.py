"""YouTube collector: quota accounting, batching, and the two metrics."""
import pytest

from radar.collectors.base import QuotaExceeded, SourceUnavailable, Term
from radar.collectors.youtube import HYDRATE_BATCH, SEARCH_COST, YouTubeCollector

TERMS = [
    Term(id=1, term="ai voice clone", normalized="ai voice clone"),
    Term(id=2, term="notion template", normalized="notion template"),
]


def search_payload(total, *video_ids):
    return {
        "pageInfo": {"totalResults": total},
        "items": [{"id": {"videoId": v}, "snippet": {"title": v}} for v in video_ids],
    }


def videos_payload(**views):
    return {
        "items": [
            {"id": v, "statistics": {"viewCount": str(n)}, "snippet": {"title": v}}
            for v, n in views.items()
        ]
    }


class StubHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, *, params=None):
        self.calls.append((url, params or {}))
        return self.payloads.pop(0) if self.payloads else {}


def collector(payloads, **kw):
    return YouTubeCollector(http=StubHttp(payloads), api_key="test-key", **kw)


def test_a_missing_key_is_refused_at_construction(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    # The predecessor shipped this collector complete and collected zero rows
    # because the key was never provisioned. Fail loudly instead.
    with pytest.raises(SourceUnavailable, match="YOUTUBE_API_KEY"):
        YouTubeCollector(http=StubHttp([]))


def test_both_metrics_are_stored():
    c = collector(
        [
            search_payload(23581, "a", "b"),
            search_payload(400, "c"),
            videos_payload(a=100, b=50, c=7),
        ]
    )
    result = c.collect(TERMS, "r1")
    by = {(r.term_id, r.metric): r.value for r in result.readings}
    assert by[(1, "video_count")] == 23581.0
    assert by[(1, "view_sum")] == 150.0
    assert by[(2, "view_sum")] == 7.0


def test_statistics_are_hydrated_in_one_batched_call():
    c = collector(
        [search_payload(10, "a", "b"), search_payload(10, "c"), videos_payload(a=1, b=1, c=1)]
    )
    c.collect(TERMS, "r1")
    videos_calls = [p for url, p in c.http.calls if "videos" in url]
    # Fifty ids for one unit, not one call per video.
    assert len(videos_calls) == 1
    assert videos_calls[0]["id"] == "a,b,c"


def test_quota_is_counted_in_units_not_requests():
    c = collector([search_payload(10, "a"), search_payload(10, "b"), videos_payload(a=1, b=1)])
    c.collect(TERMS, "r1")
    assert c.units_used == 2 * SEARCH_COST + 1


def test_quota_is_enforced_before_the_call_not_after():
    c = collector([search_payload(10, "a"), search_payload(10, "b")])
    c.quota_per_day = SEARCH_COST  # room for exactly one search
    with pytest.raises(QuotaExceeded, match="units used"):
        c.collect(TERMS, "r1")
    assert c.units_used == SEARCH_COST


def test_the_search_phrase_is_quoted():
    c = collector([search_payload(10, "a"), search_payload(10, "b"), videos_payload(a=1, b=1)])
    c.collect(TERMS, "r1")
    assert c.http.calls[0][1]["q"] == '"ai voice clone"'


def test_a_term_with_no_videos_still_reports_its_estimate():
    c = collector([search_payload(0), search_payload(5, "c"), videos_payload(c=9)])
    result = c.collect(TERMS, "r1")
    by = {(r.term_id, r.metric): r.value for r in result.readings}
    assert by[(1, "video_count")] == 0.0
    # No videos means no view_sum row rather than a fabricated zero.
    assert (1, "view_sum") not in by


def test_evidence_links_are_watchable():
    c = collector(
        [search_payload(10, "abc"), search_payload(10, "def"), videos_payload(abc=5, def_=1)]
    )
    result = c.collect(TERMS, "r1")
    assert result.evidence[0].url == "https://www.youtube.com/watch?v=abc"


def test_hydrate_batch_size_matches_the_api_limit():
    assert HYDRATE_BATCH == 50
