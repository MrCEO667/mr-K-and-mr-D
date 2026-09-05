"""Reddit RSS harvest: the gate that keeps conversation out of the term list."""
import io

from radar import db, discover


def feed(*titles):
    entries = "".join(f"<entry><title>{t}</title></entry>" for t in titles)
    return f"<feed><title>r/test</title>{entries}</feed>"


def opener_for(body):
    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda request, timeout=None: R(body.encode())


def harvest(conn, body, **kw):
    return discover.harvest_reddit(
        conn, subreddits=["test"], opener=opener_for(body), **kw
    )


def test_a_sellable_phrase_is_harvested(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    body = feed(
        "My notion template made $400",
        "Anyone else selling a notion template?",
        "notion template pricing advice",
    )
    assert "notion template" in harvest(conn, body, min_mentions=2)


def test_conversation_fragments_are_rejected(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # The first live harvest returned exactly these shapes.
    body = feed(
        "I feel like giving up on the wrong path",
        "I feel like the wrong path can keep you stuck",
        "feel like the wrong path again",
    )
    assert harvest(conn, body, min_mentions=2) == []


def test_dates_are_rejected(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # "september 2026" was the single result of the first live harvest.
    body = feed(
        "Success Saturday September 2026 thread",
        "Wins of September 2026",
        "September 2026 roundup",
    )
    assert harvest(conn, body, min_mentions=2) == []


def test_a_phrase_mentioned_once_is_not_a_signal(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    body = feed("my chrome extension launch", "unrelated title about a podcast editor")
    assert harvest(conn, body, min_mentions=2) == []


def test_existing_terms_are_not_duplicated(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO terms (term, normalized, origin, first_seen_ts, last_seen_ts) "
        "VALUES ('notion template', 'notion template', 'seed', 1, 1)"
    )
    body = feed("notion template one", "notion template two", "notion template three")
    assert harvest(conn, body, min_mentions=2) == []
    assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 1


def test_harvested_terms_record_their_origin(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    body = feed("a discord bot idea", "another discord bot idea", "discord bot pricing")
    harvest(conn, body, min_mentions=2)
    origin = conn.execute(
        "SELECT origin FROM terms WHERE term = 'discord bot'"
    ).fetchone()
    assert origin["origin"] == "harvest:reddit"


def test_a_dead_feed_does_not_raise(tmp_path):
    conn = db.connect(tmp_path / "t.db")

    def dead(request, timeout=None):
        raise OSError("429")

    assert discover.harvest_reddit(conn, subreddits=["test"], opener=dead) == []
