"""Product Hunt and Reddit collectors.

Product Hunt is verified against the live API. Reddit is not -- the script app
does not exist yet, so its tests pin shape and failure behaviour without
proving the field names. Product Hunt is the cautionary tale: its first version
queried posts(query: ...), passed its stubs, and turned out to be a field the
schema does not have.
"""
import io
import json
import urllib.error

import pytest

from radar.cache import Cache
from radar.collectors.base import RateLimiter, SourceUnavailable, Term
from radar.collectors.producthunt import ProductHuntCollector
from radar.collectors.reddit import RedditCollector

TERMS = [Term(id=1, term="ai voice clone", normalized="ai voice clone")]


def response(payload):
    class R(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return R(json.dumps(payload).encode())


def sequence(*payloads):
    remaining = list(payloads)

    def opener(request, timeout=None):
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return response(item)

    return opener


# --- Product Hunt ---------------------------------------------------------
# Verified against the live API on 2026-09-05. The first version of this
# collector queried posts(query: ...), a field that does not exist.

def ph_page(has_next, cursor, *posts):
    return {
        "data": {
            "posts": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "edges": [{"node": p} for p in posts],
            }
        }
    }


def token_page():
    return {"access_token": "tok", "token_type": "Bearer"}


def ph_collector(tmp_path, *payloads, **kw):
    return ProductHuntCollector(
        client_id="a",
        client_secret="b",
        opener=sequence(*payloads),
        rate_limiter=RateLimiter(min_interval_s=0, sleep=lambda s: None),
        cache=Cache(tmp_path / "cache"),
        **kw,
    )


def test_ph_requires_credentials(monkeypatch):
    monkeypatch.delenv("PRODUCTHUNT_CLIENT_ID", raising=False)
    monkeypatch.delenv("PRODUCTHUNT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PRODUCTHUNT_TOKEN", raising=False)
    with pytest.raises(SourceUnavailable, match="PRODUCTHUNT_CLIENT_ID"):
        ProductHuntCollector()


def test_ph_matches_terms_against_one_scan(tmp_path):
    posts = [
        {"id": "1", "name": "Voicey", "tagline": "an ai voice clone tool",
         "url": "https://ph.test/1", "votesCount": 120},
        {"id": "2", "name": "Notionly", "tagline": "a notion template shop",
         "url": "https://ph.test/2", "votesCount": 8},
    ]
    c = ph_collector(tmp_path, token_page(), ph_page(False, None, *posts))
    result = c.collect(TERMS, "r1")
    by = {r.metric: r.value for r in result.readings}
    assert by["launch_count"] == 1.0
    assert by["vote_sum"] == 120.0
    assert result.evidence[0].url == "https://ph.test/1"


def test_ph_matches_on_word_boundaries_not_substrings(tmp_path):
    # Plain `in` made the term "ai" match 39 of 160 live launches by finding
    # it inside "email", "training" and "explain".
    posts = [
        {"id": "1", "name": "Mailer", "tagline": "email training explained",
         "url": "https://ph.test/1", "votesCount": 5},
    ]
    c = ph_collector(tmp_path, token_page(), ph_page(False, None, *posts))
    result = c.collect([Term(id=9, term="ai", normalized="ai")], "r1")
    assert {r.metric: r.value for r in result.readings}["launch_count"] == 0.0


def test_ph_walks_pages_until_the_feed_ends(tmp_path):
    c = ph_collector(
        tmp_path,
        token_page(),
        ph_page(True, "cur1", {"id": "1", "name": "a", "tagline": "", "url": "u", "votesCount": 1}),
        ph_page(False, None, {"id": "2", "name": "b", "tagline": "", "url": "u", "votesCount": 1}),
    )
    assert len(c.recent_launches()) == 2
    assert c.complexity_used == 400


def test_ph_stops_at_max_pages(tmp_path):
    # The budget is 6250 complexity per 15 minutes at 200 a page; walking the
    # whole feed would spend it and strand the next run.
    pages = [ph_page(True, f"c{i}", {"id": str(i), "name": "x", "tagline": "",
                                     "url": "u", "votesCount": 1}) for i in range(5)]
    c = ph_collector(tmp_path, token_page(), *pages, max_pages=3)
    assert len(c.recent_launches()) == 3
    assert c.complexity_used == 600


def test_ph_the_scan_is_cached_for_the_day(tmp_path):
    c = ph_collector(
        tmp_path,
        token_page(),
        ph_page(False, None, {"id": "1", "name": "a", "tagline": "", "url": "u", "votesCount": 1}),
    )
    c.recent_launches()
    # A second walk would exhaust the opener sequence and raise IndexError.
    assert len(c.recent_launches()) == 1


def test_ph_graphql_errors_are_not_read_as_zero(tmp_path):
    # GraphQL answers 200 with an errors array. Treating that as no data would
    # store zeros meaning "nobody launched this" -- the worst wrong answer.
    c = ph_collector(tmp_path, token_page(), {"errors": [{"message": "bad field"}]})
    with pytest.raises(SourceUnavailable):
        c.collect(TERMS, "r1")


def test_ph_a_rejected_credential_is_a_dead_source(tmp_path):
    err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    c = ph_collector(tmp_path, err)
    with pytest.raises(SourceUnavailable, match="rejected the credentials"):
        c.collect(TERMS, "r1")


def test_ph_a_rate_limited_scan_keeps_the_pages_it_got(tmp_path):
    # The first full sweep 429'd part way through. Pages already walked are
    # real launches; reporting nothing would be worse than reporting fewer.
    err = urllib.error.HTTPError("u", 429, "too many", {}, None)
    c = ph_collector(
        tmp_path,
        token_page(),
        ph_page(True, "c1", {"id": "1", "name": "Voicey",
                             "tagline": "an ai voice clone tool",
                             "url": "https://ph.test/1", "votesCount": 3}),
        err, err, err, err,
    )
    result = c.collect(TERMS, "r1")
    assert result.partial
    assert any("truncated" in e for e in result.errors)
    assert {r.metric: r.value for r in result.readings}["launch_count"] == 1.0
    assert c.health().status == "degraded"


def test_ph_a_truncated_scan_is_not_cached(tmp_path):
    err = urllib.error.HTTPError("u", 429, "too many", {}, None)
    c = ph_collector(
        tmp_path,
        token_page(),
        ph_page(True, "c1", {"id": "1", "name": "a", "tagline": "",
                             "url": "u", "votesCount": 1}),
        err, err, err, err,
    )
    assert len(c.recent_launches()) == 1
    # Caching a short answer would freeze it in for the rest of the day, so a
    # second call must walk again rather than serve the cache. Here that means
    # exhausting the stubbed opener and coming back with nothing.
    assert c.recent_launches() == []


def test_ph_a_scan_that_returns_nothing_is_a_dead_source(tmp_path):
    err = urllib.error.HTTPError("u", 429, "too many", {}, None)
    c = ph_collector(tmp_path, token_page(), err, err, err, err)
    with pytest.raises(SourceUnavailable, match="no launches scanned"):
        c.collect(TERMS, "r1")


# --- Reddit ----------------------------------------------------------------

def reddit_listing(*posts):
    return {"data": {"children": [{"data": p} for p in posts]}}


def test_reddit_requires_credentials(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    with pytest.raises(SourceUnavailable, match="REDDIT_CLIENT_ID"):
        RedditCollector()


def test_reddit_authenticates_then_searches():
    post = {"permalink": "/r/x/1", "title": "t", "score": 40, "subreddit": "x"}
    opener = sequence(
        {"access_token": "tok", "expires_in": 3600},
        reddit_listing(post, {"permalink": "/r/x/2", "score": 2}),
    )
    c = RedditCollector(client_id="a", client_secret="b", opener=opener)
    result = c.collect(TERMS, "r1")
    by = {r.metric: r.value for r in result.readings}
    assert by["post_count"] == 2.0
    assert by["score_sum"] == 42.0
    assert result.evidence[0].url == "https://www.reddit.com/r/x/1"


def test_reddit_reuses_the_token_across_terms():
    terms = [
        Term(id=1, term="a", normalized="a"),
        Term(id=2, term="b", normalized="b"),
    ]
    opener = sequence(
        {"access_token": "tok", "expires_in": 3600},
        reddit_listing({"permalink": "/r/x/1", "score": 1}),
        reddit_listing({"permalink": "/r/x/2", "score": 1}),
    )
    c = RedditCollector(client_id="a", client_secret="b", opener=opener)
    # A second token request would exhaust the sequence and raise IndexError.
    assert len(c.collect(terms, "r1").readings) == 4


def test_reddit_bad_credentials_are_a_dead_source():
    err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    c = RedditCollector(client_id="a", client_secret="b", opener=sequence(err))
    with pytest.raises(SourceUnavailable, match="rejected the credentials"):
        c.collect(TERMS, "r1")
