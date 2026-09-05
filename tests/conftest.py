"""No test may touch the network.

test_runner drove runner.main() end to end, which built real collectors and
made live GitHub and Hacker News calls -- 25 terms at 7s spacing, four minutes
of CI, against somebody else's rate limit. Hermeticity is enforced here rather
than left to each test remembering to stub.

A test that genuinely needs HTTP injects its own opener; this only blocks the
real one.
"""
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError(
            "a test tried to open a real network connection. Inject a stub "
            "opener or a fake client instead."
        )

    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", blocked)
