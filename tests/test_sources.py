"""The source seam: live poll and historical replay present one interface.

The tracker consumes ``SourceTick`` objects and cannot tell which source
produced them; issue #6 drives the full engine and issue #7 polls the
dashboard through this same seam.
"""

from __future__ import annotations

from draftbot.sleeper_client import SleeperClient
from draftbot.sources import LivePollSource, ReplaySource

from .conftest import FakeTransport


def _raw_pick(pick_no: int, player_id: str, slot: int, amount: str) -> dict:
    return {
        "round": 1 + (pick_no - 1) // 12,
        "pick_no": pick_no,
        "draft_slot": slot,
        "player_id": player_id,
        "picked_by": "",
        "is_keeper": None,
        "metadata": {"amount": amount, "position": "WR"},
    }


def _raw_completed_draft() -> dict:
    return {
        "draft_id": "1257407146123857920",
        "status": "complete",
        "type": "auction",
        "start_time": 1_756_000_000_000,
        "metadata": {
            # A real completed draft keeps pointing at the last sale.
            "nominated_player_id": "12498",
            "highest_offer": "1",
            "nominating_slot": "11",
        },
        "settings": {"budget": 200, "teams": 12, "budget_1": 133, "budget_2": 115},
        "slot_to_roster_id": {"1": 4, "2": 8},
    }


def test_replay_reveals_picks_one_per_poll_in_pick_order():
    """The first tick has no sales yet; each poll reveals exactly one pick,
    in pick_no order even when the raw feed arrives shuffled."""
    raw_picks = [
        _raw_pick(2, "B", 2, "10"),
        _raw_pick(1, "A", 1, "51"),
        _raw_pick(3, "C", 1, "7"),
    ]
    source = ReplaySource(_raw_completed_draft(), raw_picks)

    first = source.poll()
    assert not first.picks

    revealed = [source.poll().picks for _ in range(3)]
    assert [len(picks) for picks in revealed] == [1, 2, 3]
    assert [pick.player_id for pick in revealed[-1]] == ["A", "B", "C"]
    assert revealed[-1][0].amount == 51


def test_replay_mimics_the_stale_nomination_pointer_and_status():
    """Mid-replay the draft reads as live Sleeper would serve it: drafting,
    with the nomination metadata still pointing at the just-sold player.
    After the last sale the draft completes, and further polls repeat the
    finished state (exactly like polling a real completed draft)."""
    raw_picks = [_raw_pick(1, "A", 1, "51"), _raw_pick(2, "B", 2, "10")]
    source = ReplaySource(_raw_completed_draft(), raw_picks)

    first = source.poll()
    assert first.draft.status == "drafting"
    assert first.draft.nominated_player_id is None

    mid = source.poll()
    assert mid.draft.status == "drafting"
    assert mid.draft.nominated_player_id == "A"
    assert mid.draft.highest_offer == "51"
    assert mid.draft.nominating_slot == "1"

    last = source.poll()
    assert last.draft.status == "complete"
    assert last.draft.nominated_player_id == "B"

    again = source.poll()
    assert again.draft.status == "complete"
    assert len(again.picks) == 2
    assert again.stale_endpoints == frozenset()


def test_live_poll_reports_staleness_per_endpoint_per_tick(config, tmp_path):
    """Anti-cheat: ``last_fetch_degraded`` is LAST-CALL state. A source that
    reads it once after both fetches reports nothing stale when the draft
    fetch degraded but the picks fetch then succeeded live."""
    draft_key = f"/draft/{config.draft_id}"
    transport = FakeTransport(
        {
            draft_key: _raw_completed_draft() | {"status": "drafting"},
            "/picks": [_raw_pick(1, "A", 1, "51")],
        }
    )
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )
    source = LivePollSource(client)

    live = source.poll()
    assert live.stale_endpoints == frozenset()
    assert live.draft.status == "drafting"
    assert [pick.player_id for pick in live.picks] == ["A"]

    draft_payload = transport.payloads.pop(draft_key)
    draft_stale = source.poll()
    assert draft_stale.stale_endpoints == frozenset({"draft"})
    assert draft_stale.draft.status == "drafting"  # cached copy, still parsed

    transport.payloads[draft_key] = draft_payload
    del transport.payloads["/picks"]
    picks_stale = source.poll()
    assert picks_stale.stale_endpoints == frozenset({"picks"})
    assert [pick.player_id for pick in picks_stale.picks] == ["A"]


def test_live_poll_defaults_to_the_live_draft_and_accepts_historical_ids(
    config, tmp_path
):
    """The source polls the config's live draft unless given a historical id
    (the seam the 2025 replay demo and the backtest both use)."""
    historical_id = config.historical_draft_ids["2025"]
    transport = FakeTransport(
        {
            f"/draft/{historical_id}": _raw_completed_draft(),
            "/picks": [_raw_pick(1, "A", 1, "51")],
        }
    )
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )

    tick = LivePollSource(client, draft_id=historical_id).poll()
    assert tick.draft.draft_id == historical_id
    assert any(historical_id in url for url in transport.requests)
