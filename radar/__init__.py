"""NicheRadar — local opportunity scanner.

Layer boundaries are enforced by SQLite, not by imports: collect, score,
compose and deliver talk through tables, never through each other's
functions. See docs/ARCHITECTURE.md.
"""

__version__ = "0.1.0"
