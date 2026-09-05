"""Harvest: the gates that keep conversation out of the term list.

Reads Hacker News, not Reddit. Reddit's API is approval-only under the
Responsible Builder Policy and its robots.txt disallows everything, so the
earlier RSS-based harvest was removed as a rule-2 violation.
"""
from radar import db, discover


class StubHttp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, *, params=None):
        self.calls.append(params or {})
        return self.pages.pop(0) if self.pages else {"hits": []}


def page(*titles):
    return {"hits": [{"title": t} for t in titles]}


def harvest(conn, *pages, **kw):
    kw.setdefault("min_mentions", 2)
    kw.setdefault("pages", len(pages) or 1)
    return discover.harvest_hackernews(conn, http=StubHttp(pages), **kw)


def test_a_sellable_phrase_is_harvested(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    body = page(
        "My notion template made $400",
        "Show HN: a notion template shop",
        "notion template pricing advice",
    )
    assert "notion template" in harvest(conn, body)


def test_conversation_fragments_are_rejected(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # The first live harvest returned exactly these shapes.
    body = page(
        "I feel like giving up on the wrong path",
        "I feel like the wrong path can keep you stuck",
        "feel like the wrong path again",
    )
    assert harvest(conn, body) == []


def test_dates_are_rejected(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # "september 2026" was the single result of the first live harvest.
    body = page(
        "Success Saturday September 2026 thread",
        "Wins of September 2026",
        "September 2026 roundup",
    )
    assert harvest(conn, body) == []


def test_a_phrase_mentioned_once_is_not_a_signal(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    body = page("my chrome extension launch", "unrelated title about a podcast editor")
    assert harvest(conn, body) == []


def test_existing_terms_are_not_duplicated(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO terms (term, normalized, origin, first_seen_ts, last_seen_ts) "
        "VALUES ('notion template', 'notion template', 'seed', 1, 1)"
    )
    body = page("notion template one", "notion template two", "notion template three")
    assert harvest(conn, body) == []
    assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 1


def test_harvested_terms_record_their_origin(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    body = page("a discord bot idea", "another discord bot idea", "discord bot pricing")
    harvest(conn, body)
    row = conn.execute("SELECT origin FROM terms WHERE term = 'discord bot'").fetchone()
    assert row["origin"] == "harvest:hackernews"


def test_a_failing_page_does_not_stop_the_harvest(tmp_path):
    conn = db.connect(tmp_path / "t.db")

    class Flaky(StubHttp):
        def get(self, url, *, params=None):
            if (params or {}).get("page") == 0:
                raise OSError("500")
            return page("a discord bot idea", "another discord bot idea")

    added = discover.harvest_hackernews(
        conn, http=Flaky([]), min_mentions=2, pages=2
    )
    assert "discord bot" in added


def test_the_search_window_is_bounded(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    stub = StubHttp([page("a discord bot idea")])
    discover.harvest_hackernews(conn, http=stub, pages=1)
    assert "created_at_i>" in stub.calls[0]["numericFilters"]
