"""Smoke check that the testcontainer + build_client wiring works end-to-end.

Phase-0 verification grows here in Task 16; this initial test only confirms the
client can answer SELECT 1.
"""

from __future__ import annotations


def test_build_client_can_run_select_one(ch_client):
    result = ch_client.query("SELECT 1 AS one")
    rows = list(result.named_results())
    assert rows == [{"one": 1}]
