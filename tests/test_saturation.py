"""Supply counting: labels, persistence, and failure isolation."""
import io

import pytest

from radar import db, saturation
from radar.collectors.base import SourceUnavailable, Term

TERMS = [
    Term(id=1, term="notion template", normalized="notion template"),
    Term(id=2, term="ai voice clone", normalized="ai voice clone"),
]


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    connection.execute(
        "INSERT INTO terms (id, term, normalized, origin, first_seen_ts, last_seen_ts) "
        "VALUES (1, 'notion template', 'notion template', 'seed', 1, 1), "
        "(2, 'ai voice clone', 'ai voice clone', 'seed', 1, 1)"
    )
    yield connection
    connection.close()


class Counter(saturation.SaturationCounter):
    def __init__(self, source, values=None, raises=None):
        self.source = source
        self.values = values or {}
        self.raises = raises

    def count(self, term):
        if self.raises:
            raise self.raises
        if term.id not in self.values:
            raise ValueError("no count")
        return self.values[term.id]


def test_labels_come_from_raw_counts(conn):
    # Stored raw, labelled at score time so thresholds stay tunable.
    assert saturation.label(10, low_max=200, med_max=2000) == "LOW"
    assert saturation.label(500, low_max=200, med_max=2000) == "MED"
    assert saturation.label(50_000, low_max=200, med_max=2000) == "HIGH"
    assert saturation.label(200, low_max=200, med_max=2000) == "LOW"


def test_counts_are_persisted_with_the_run(conn):
    with db.run(conn, "saturation") as run_id:
        written = saturation.collect_saturation(
            conn, [Counter("github", {1: 2009, 2: 15})], TERMS, run_id
        )
    assert written == 2
    rows = conn.execute("SELECT source, count FROM saturation_snapshots ORDER BY count").fetchall()
    assert [(r["source"], r["count"]) for r in rows] == [("github", 15), ("github", 2009)]


def test_one_failing_term_does_not_lose_the_others(conn):
    with db.run(conn, "saturation") as run_id:
        written = saturation.collect_saturation(
            conn, [Counter("gumroad", {1: 19875})], TERMS, run_id
        )
    assert written == 1


def test_one_dead_counter_does_not_stop_the_others(conn):
    counters = [
        Counter("gumroad", raises=SourceUnavailable("403")),
        Counter("github", {1: 5, 2: 6}),
    ]
    with db.run(conn, "saturation") as run_id:
        written = saturation.collect_saturation(conn, counters, TERMS, run_id)
    assert written == 2
    sources = {r["source"] for r in conn.execute("SELECT source FROM saturation_snapshots")}
    assert sources == {"github"}


def test_latest_counts_reads_back_per_source(conn):
    with db.run(conn, "saturation") as run_id:
        saturation.collect_saturation(
            conn, [Counter("github", {1: 7}), Counter("gumroad", {1: 99})], TERMS, run_id
        )
    assert saturation.latest_counts(conn, 1) == {"github": 7, "gumroad": 99}


# --- Gumroad parsing -------------------------------------------------------

def gumroad_page(total):
    return (
        '<html><script>{"x":1,&quot;search_results&quot;:{&quot;total&quot;:'
        + str(total)
        + ',&quot;tags_data&quot;:[]}}</script></html>'
    )


def opener_for(body):
    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda request, timeout=None: R(body.encode())


def test_gumroad_reads_the_embedded_total():
    counter = saturation.GumroadSaturation(opener=opener_for(gumroad_page(19875)))
    assert counter.count(TERMS[0]) == 19875


def test_gumroad_missing_total_fails_loudly_rather_than_returning_zero():
    # A zero would read as "nobody is selling this", which is the most
    # dangerous possible wrong answer for a saturation signal.
    counter = saturation.GumroadSaturation(opener=opener_for("<html>redesigned</html>"))
    with pytest.raises(ValueError, match="not found"):
        counter.count(TERMS[0])
