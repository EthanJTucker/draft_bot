"""The 2025 replay end to end through the dashboard's own CLI wiring.

Exactly what the demo command runs — ``main(["--replay", fixture])`` with
only the server call swapped for a capture — stepped through all 180
sales. Every mid-replay snapshot must carry the full top bar (nominee,
high bid, analysis, verdict, profit) with no manual input anywhere.
"""

from __future__ import annotations

import io
import json

from draftbot.dashboard.app import main

from .conftest import REPO_ROOT
from .test_dashboard_app import FIXTURE, CapturingServer


def _poller():
    server = CapturingServer()
    exit_code = main(
        ["--replay", str(FIXTURE), "--config", str(REPO_ROOT / "league_config.toml")],
        server=server,
        out=io.StringIO(),
    )
    assert exit_code == 0
    return server.poller


def test_every_replay_snapshot_carries_the_full_top_bar():
    """181 polls, zero input: each sale's snapshot names the just-sold
    nominee with its hammer price, a pre-sale analysis, a retrospective
    verdict, and a profit figure — the one-poll-cycle latency criterion
    holds on every single lot."""
    picks = json.loads(FIXTURE.read_text(encoding="utf-8"))["picks"]
    picks.sort(key=lambda pick: pick["pick_no"])
    poller = _poller()

    opening = poller.step()
    assert opening["nomination"]["status"] == "none"
    assert opening["sales"] == []

    actions = set()
    for pick in picks:
        state = poller.step()
        nomination = state["nomination"]
        # The sale revealed by THIS poll is already on screen: latency
        # is one poll cycle by construction.
        assert state["sales"][-1]["player_id"] == pick["player_id"]
        assert nomination["player_id"] == pick["player_id"]
        assert nomination["status"] == "sold_between_lots"
        assert nomination["high_bid"] == int(pick["metadata"]["amount"])
        assert nomination["pre_sale"] is True
        assert nomination["analysis"] is not None
        assert nomination["verdict"] is not None
        assert nomination["verdict"]["basis"] == "final"
        actions.add(nomination["verdict"]["action"])
        # The replay-derived sheet prices at the hammer, so profit sits
        # exactly at its $0 center on every lot (documented demo shape).
        assert nomination["profit"] == 0.0
        assert state["off_model_player_ids"] == []
    # With worth pinned AT the hammer price, a bargain-margin bot can
    # never beat the winning bid: all-PASS is the rational output of the
    # self-derived demo sheet (a real --sheet CSV varies the calls).
    assert actions == {"PASS"}


def test_replay_finishes_complete_with_an_empty_board_and_full_rosters():
    """After the last sale: status complete, no players left to buy, my
    team (slot 8 in 2025) full with nothing left to bid."""
    poller = _poller()
    state = poller.step()
    for _ in range(180):
        state = poller.step()

    assert state["status"] == "complete"
    assert state["players"] == []  # every sheet player sold
    assert len(state["sales"]) == 180
    me = state["me"]
    assert me["slot"] == 8
    assert len(me["roster"]) == 15
    assert me["open_slots"] == 0
    assert me["max_bid"] == 0
    assert sum(len(team["needs"]) == 0 for team in state["teams"]) >= 8
