"""Product Hunt and Reddit collectors.

Both are written against documented APIs and have never run against the live
services -- the token and the script app do not exist yet. These tests pin the
shape and the failure behaviour; they do not prove the field names are right.
That only happens on first contact with the real API.
"""
import io
import json
import urllib.error

import pytest

from radar.collectors.base import SourceUnavailable, Term
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


# --- Product Hunt ----------------------------------------------------------

def ph_payload(total, *posts):
    return {
        "data": {
            "posts": {
                "totalCount": total,
                "edges": [{"node": p} for p in posts],
            }
        }
    }


def test_ph_requires_a_token(monkeypatch):
    monkeypatch.delenv("PRODUCTHUNT_TOKEN", raising=False)
    with pytest.raises(SourceUnavailable, match="PRODUCTHUNT_TOKEN"):
        ProductHuntCollector()


def test_ph_stores_launch_count_and_votes():
    post = {"id": "1", "name": "Voicey", "url": "https://ph.test/1", "votesCount": 120}
    c = ProductHuntCollector(token="t", opener=sequence(ph_payload(7, post)))
    result = c.collect(TERMS, "r1")
    by = {r.metric: r.value for r in result.readings}
    assert by["launch_count"] == 7.0
    assert by["vote_sum"] == 120.0
    assert result.evidence[0].url == "https://ph.test/1"


def test_ph_graphql_errors_are_not_read_as_zero():
    # GraphQL answers 200 with an errors array. Treating that as no data would
    # store a zero meaning "nobody launched this" -- the worst wrong answer.
    c = ProductHuntCollector(
        token="t", opener=sequence({"errors": [{"message": "bad field"}]})
    )
    with pytest.raises(SourceUnavailable):
        c.collect(TERMS, "r1")


def test_ph_a_rejected_token_is_a_dead_source():
    err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    c = ProductHuntCollector(token="bad", opener=sequence(err))
    with pytest.raises(SourceUnavailable, match="rejected the token"):
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
