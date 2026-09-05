"""Seeding: idempotent, and the stoplist is applied at discovery."""
import yaml

from radar import db, discover

SEEDS = {
    "categories": {
        "ai_tools": ["ai voice clone", "ai avatar generator"],
        "junk": ["buy $DOGE coin"],
    },
    "exclude_patterns": [r"\b(coin|token|crypto|nft)\b"],
}


def seed_file(tmp_path):
    path = tmp_path / "seeds.yaml"
    path.write_text(yaml.safe_dump(SEEDS), encoding="utf-8")
    return path


def test_seeds_land_as_active_terms(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    terms = discover.seed_terms(conn, seed_file(tmp_path))
    assert {t.term for t in terms} == {"ai voice clone", "ai avatar generator"}


def test_financial_terms_are_dropped_at_discovery(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    terms = discover.seed_terms(conn, seed_file(tmp_path))
    # PROMPT.md: instruments are excluded by design, at the discovery stage.
    assert not any("DOGE" in t.term for t in terms)


def test_reseeding_does_not_fork_a_term_history(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    path = seed_file(tmp_path)
    discover.seed_terms(conn, path)
    discover.seed_terms(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 2


def test_normalization_is_stable():
    assert discover.normalize("  AI  Voice   Clone ") == "ai voice clone"
