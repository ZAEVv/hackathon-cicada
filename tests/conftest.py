"""Shared test setup: offline weather; witness ledger in-process unless a test opts in."""
import os

import pytest

# The app defaults to live Open-Meteo weather; tests always use the synthetic
# provider (tests/test_weather.py exercises the live path with a mocked HTTP layer).
os.environ["WEATHER_PROVIDER"] = "synthetic"


@pytest.fixture(autouse=True)
def _in_process_ledger(monkeypatch):
    # A developer shell with LEDGER_WITNESS_URLS set (network mode) must not leak into tests;
    # tests/test_witness_network.py sets it explicitly for its own witness services.
    monkeypatch.delenv("LEDGER_WITNESS_URLS", raising=False)
    monkeypatch.delenv("LEDGER_WITNESS_TIMEOUT", raising=False)
