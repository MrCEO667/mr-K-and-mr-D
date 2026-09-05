"""Hacker News and GitHub collectors, against stubbed HTTP."""
import pytest

from radar.collectors.base import SourceUnavailable, Term
from radar.collectors.github import GitHubCollector
from radar.collectors.hackernews import HackerNewsCollector

TERMS = [
    Term(id=1, term="ai voice clone", normalized="ai voice clone"),
    Term(id=2, term="notion template", normalized="notion template"),
]


class StubHttp:
    def __init__(self, payloads=None, raises=None):
        self.payloads = payloads if payloads is not None else []
        self.raises = raises
        self.calls = []

    def get(self, url, *, params=None):
        self.calls.append(params or {})
        if self.raises:
            raise self.raises
        return self.payloads.pop(0) if self.payloads else {}


# --- Hacker News -----------------------------------------------------------

def test_hn_queries_the_exact_phrase():
    http = StubHttp([{"nbHits": 22, "hits": []}, {"nbHits": 5, "hits": []}])
    HackerNewsCollector(http=http).collect(TERMS, "r1")
    # Unquoted, Algolia ORs the words: 219 loose hits vs 22 real ones.
    assert http.calls[0]["query"] == '"ai voice clone"'


def test_hn_stores_the_hit_count_and_evidence():
    hits = [
        {"objectID": "1", "title": "Show HN: voice clone", "url": "https://x.test", "points": 40}
    ]
    http = StubHttp([{"nbHits": 22, "hits": hits}, {"nbHits": 5, "hits": []}])
    result = HackerNewsCollector(http=http).collect(TERMS, "r1")
    assert [r.value for r in result.readings] == [22.0, 5.0]
    assert result.evidence[0].url == "https://x.test"
    assert result.evidence[0].metric_json["points"] == 40


def test_hn_evidence_falls_back_to_the_discussion_link():
    hits = [{"objectID": "42", "title": "Ask HN", "url": None}]
    http = StubHttp([{"nbHits": 1, "hits": hits}, {"nbHits": 0, "hits": []}])
    result = HackerNewsCollector(http=http).collect(TERMS, "r1")
    assert result.evidence[0].url.endswith("id=42")


def test_hn_one_bad_response_is_partial():
    http = StubHttp([{"hits": []}, {"nbHits": 5, "hits": []}])  # first lacks nbHits
    result = HackerNewsCollector(http=http).collect(TERMS, "r1")
    assert result.partial
    assert len(result.readings) == 1


def test_hn_every_term_failing_is_a_dead_source():
    http = StubHttp([{"hits": []}, {"hits": []}])
    with pytest.raises(SourceUnavailable):
        HackerNewsCollector(http=http).collect(TERMS, "r1")


# --- GitHub ----------------------------------------------------------------

def repos(*stars):
    return {
        "items": [
            {
                "stargazers_count": s,
                "html_url": f"https://github.com/x/{s}",
                "full_name": f"x/{s}",
                "description": None,
            }
            for s in stars
        ]
    }


def test_github_sums_stars_of_the_top_matches():
    http = StubHttp([repos(100, 50, 25), repos(7)])
    result = GitHubCollector(http=http).collect(TERMS, "r1")
    assert [r.value for r in result.readings] == [175.0, 7.0]
    assert result.readings[0].metric == "stars"


def test_github_records_evidence_links():
    http = StubHttp([repos(100, 50), repos(7)])
    result = GitHubCollector(http=http).collect(TERMS, "r1")
    assert result.evidence[0].url.startswith("https://github.com/")


def test_github_zero_results_is_a_real_zero_not_a_failure():
    # No repos matching a term is a genuine measurement: nobody has built it.
    http = StubHttp([{"items": []}, repos(3)])
    result = GitHubCollector(http=http).collect(TERMS, "r1")
    assert not result.partial
    assert result.readings[0].value == 0.0


def test_github_quota_is_enforced_not_discovered():
    http = StubHttp([repos(1) for _ in range(5)])
    c = GitHubCollector(http=http)
    c.quota_per_day = 1
    with pytest.raises(Exception, match="quota"):
        c.collect(TERMS, "r1")
