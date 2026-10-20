"""Real retry test for DataFetcher._retry_call.

The proposal's "chaos loop" raised an error from the same code that handles it, which
proves only that the handler executes. Here the failure is injected from outside and
the behaviour is asserted: retries, backoff, eventual success, eventual give-up.
"""

from __future__ import annotations

import ccxt
import pytest

from src.data_fetcher import DataFetcher


@pytest.fixture()
def fetcher(monkeypatch):
    """A fetcher whose sleeps are recorded instead of performed."""
    f = DataFetcher(enable_rate_limit=False)
    slept: list[float] = []
    monkeypatch.setattr("src.data_fetcher.time.sleep", lambda s: slept.append(s))
    f._slept = slept
    return f


def test_retries_twice_then_succeeds(fetcher):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ccxt.RateLimitExceeded("429 simulated")
        return {"ok": True}

    assert fetcher._retry_call(flaky) == {"ok": True}
    assert calls["n"] == 3, "must have retried twice before succeeding"
    assert fetcher._slept == [1, 4], "backoff must be 1s then 4s"


def test_gives_up_after_retry_budget(fetcher):
    def always_429():
        raise ccxt.RateLimitExceeded("429 simulated")

    with pytest.raises(ccxt.RateLimitExceeded):
        fetcher._retry_call(always_429)
    assert fetcher._slept == [1, 4, 16], "three attempts -> three backoffs"


def test_network_error_is_also_retried(fetcher):
    calls = {"n": 0}

    def flaky_network():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ccxt.NetworkError("connection reset")
        return "ok"

    assert fetcher._retry_call(flaky_network) == "ok"
    assert calls["n"] == 2


def test_non_transient_error_is_not_retried(fetcher):
    def bad_symbol():
        raise ccxt.BadSymbol("no such market")

    with pytest.raises(ccxt.BadSymbol):
        fetcher._retry_call(bad_symbol)
    assert fetcher._slept == [], "a fatal error must not be retried"

