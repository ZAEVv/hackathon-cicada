"""Shared test setup: the suite runs the witness ledger in-process unless a test opts in."""
import pytest


@pytest.fixture(autouse=True)
def _in_process_ledger(monkeypatch):
    # A developer shell with LEDGER_WITNESS_URLS set (network mode) must not leak into tests;
    # tests/test_witness_network.py sets it explicitly for its own witness services.
    monkeypatch.delenv("LEDGER_WITNESS_URLS", raising=False)
    monkeypatch.delenv("LEDGER_WITNESS_TIMEOUT", raising=False)
