"""The source seam: live poll and historical replay present one interface.

The tracker consumes ``SourceTick`` objects and cannot tell which source
produced them; issue #6 drives the full engine and issue #7 polls the
dashboard through this same seam.
"""

from __future__ import annotations

import pytest

from draftbot.sleeper_client import SleeperClient
from draftbot.sources import LivePollSource, ReplaySource

from .conftest import FakeTransport, raw_auction_pick


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
        raw_auction_pick(2, "B", 2, "10"),
        raw_auction_pick(1, "A", 1, "51"),
        raw_auction_pick(3, "C", 1, "7"),
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
    raw_picks = [raw_auction_pick(1, "A", 1, "51"), raw_auction_pick(2, "B", 2, "10")]
    source = ReplaySource(_raw_completed_draft(), raw_picks)

    first = source.poll()
    assert first.draft.status == "drafting"
    assert first.draft.nominated_player_id is None

    mid = source.poll()
    assert mid.draft.status == "drafting"
    assert mid.draft.nominated_player_id == "A"
    assert mid.draft.highest_offer == "51"

    last = source.poll()
    assert last.draft.status == "complete"
    assert last.draft.nominated_player_id == "B"

    again = source.poll()
    assert again.draft.status == "complete"
    assert len(again.picks) == 2
    assert again.stale_endpoints == frozenset()


def test_replay_synthesizes_only_what_the_picks_feed_can_know():
    """Honest wire semantics: the sale's winner IS the high bidder at the
    hammer, so the winner's slot goes in ``offering_slot`` and the winner's
    ``picked_by`` (when present) in ``offering_user_id``. The NOMINATOR is
    unknowable from the picks feed, so ``nominating_slot`` must be None —
    a replay that fabricates it would let nomination-behavior logic
    backtest against fiction."""
    with_ids = raw_auction_pick(1, "A", 1, "51") | {"picked_by": "888"}
    without_ids = raw_auction_pick(2, "B", 2, "10")
    source = ReplaySource(_raw_completed_draft(), [with_ids, without_ids])
    source.poll()  # opening tick, no sales yet

    first_sale = source.poll().draft
    assert first_sale.offering_slot == "1"
    assert first_sale.offering_user_id == "888"
    assert first_sale.nominating_slot is None

    second_sale = source.poll().draft
    assert second_sale.offering_slot == "2"
    assert second_sale.offering_user_id is None  # picked_by blank in feed
    assert second_sale.nominating_slot is None


def test_replay_ticks_cannot_be_poisoned_by_a_mutating_consumer():
    """Replay shares parsed objects across ticks, so the seam relies on
    the models being read-only: a consumer annotating pick metadata or
    editing budgets must get a TypeError, not corrupt its own backtest."""
    source = ReplaySource(_raw_completed_draft(), [raw_auction_pick(1, "A", 1, "51")])
    source.poll()
    tick = source.poll()

    with pytest.raises(TypeError):
        tick.draft.budget_by_slot[1] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        tick.picks[0].metadata["amount"] = "999"  # type: ignore[index]

    repeat = source.poll()  # completed: repeats the finished state
    assert repeat.draft.budget_by_slot[1] == 133
    assert repeat.picks[0].metadata["amount"] == "51"


def test_live_poll_reports_staleness_per_endpoint_per_tick(config, tmp_path):
    """Anti-cheat: ``last_fetch_degraded`` is LAST-CALL state. A source that
    reads it once after both fetches reports nothing stale when the draft
    fetch degraded but the picks fetch then succeeded live."""
    draft_key = f"/draft/{config.draft_id}"
    transport = FakeTransport(
        {
            draft_key: _raw_completed_draft() | {"status": "drafting"},
            "/picks": [raw_auction_pick(1, "A", 1, "51")],
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
            "/picks": [raw_auction_pick(1, "A", 1, "51")],
        }
    )
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )

    tick = LivePollSource(client, draft_id=historical_id).poll()
    assert tick.draft.draft_id == historical_id
    assert any(historical_id in url for url in transport.requests)
