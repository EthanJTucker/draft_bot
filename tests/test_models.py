"""Clean parsed types for picks and draft state (Sleeper's gotchas covered)."""

from __future__ import annotations

from draftbot.models import parse_draft, parse_pick, parse_picks, spent_by_slot


def _raw_pick(**overrides):
    """A realistic auction pick payload, with an anti-cheat decoy: the real
    winning bid lives ONLY in ``metadata.amount`` as a string; the top-level
    int ``amount`` here is a trap for implementations that skip the metadata
    path."""
    pick = {
        "round": 1,
        "pick_no": 3,
        "draft_slot": 5,
        "player_id": "4034",
        "picked_by": "111111111111111111",
        "is_keeper": None,
        "amount": 1,  # decoy — not where Sleeper puts the winning bid
        "metadata": {
            "amount": "43",
            "first_name": "Tyreek",
            "last_name": "Hill",
            "position": "WR",
            "team": "MIA",
        },
    }
    pick.update(overrides)
    return pick


def test_winning_bid_parses_from_metadata_amount_string():
    """metadata.amount arrives as a STRING and is the only true bid."""
    pick = parse_pick(_raw_pick())
    assert pick.amount == 43
    assert pick.player_id == "4034"
    assert pick.draft_slot == 5
    assert pick.is_keeper is False


def test_pick_without_amount_parses_as_none():
    """A pick with no bid in metadata must not crash (amount is None)."""
    raw = _raw_pick()
    del raw["metadata"]["amount"]
    del raw["amount"]
    assert parse_pick(raw).amount is None


def test_purchases_attribute_by_draft_slot_not_picked_by():
    """Anti-cheat: picked_by deliberately disagrees with draft_slot.

    Sleeper's picked_by can be empty or point at whoever clicked, so slot 5's
    two purchases here carry two DIFFERENT picked_by ids, and slot 2's pick
    shares its picked_by with a slot-5 pick. Grouping by picked_by produces
    {A: 50, B: 10}; grouping by slot produces {5: 53, 2: 7}.
    """
    picks = parse_picks(
        [
            _raw_pick(
                draft_slot=5,
                picked_by="A",
                metadata={"amount": "43"},
            ),
            _raw_pick(
                draft_slot=5,
                picked_by="B",
                player_id="1466",
                metadata={"amount": "10"},
            ),
            _raw_pick(
                draft_slot=2,
                picked_by="A",
                player_id="6794",
                metadata={"amount": "7"},
            ),
        ]
    )

    assert spent_by_slot(picks) == {5: 53, 2: 7}


def test_spent_by_slot_ignores_amountless_picks():
    """Picks with no bid (e.g. malformed rows) add nothing to a slot's spend."""
    raw = _raw_pick(draft_slot=4, metadata={})
    assert spent_by_slot(parse_picks([raw])) == {4: 0}


def _raw_draft(**overrides):
    draft = {
        "draft_id": "1389692302259138561",
        "status": "drafting",
        "type": "auction",
        "start_time": 1_756_598_100_000,
        "metadata": {
            "nominated_player_id": "4034",
            "highest_offer": "23",
            "offering_user_id": "111111111111111111",
            "nominating_slot": "5",
            "timer_end_at": "1000000",
        },
        "settings": {"budget": 200, "teams": 12},
        "slot_to_roster_id": {"1": 3, "5": 7},
    }
    draft.update(overrides)
    return draft


def test_paused_draft_does_not_read_as_expired():
    """Anti-cheat: overnight auto-pause freezes timer_end_at in the past.

    A bare now >= timer_end_at comparison calls this draft expired; honoring
    the pause flag must win.
    """
    metadata = dict(_raw_draft()["metadata"], paused="true")
    state = parse_draft(_raw_draft(metadata=metadata))
    assert state.paused is True
    assert state.is_timer_expired(now_ms=2_000_000) is False


def test_paused_status_also_counts_as_paused():
    """Sleeper can flag the pause via draft status instead of metadata."""
    state = parse_draft(_raw_draft(status="paused"))
    assert state.paused is True
    assert state.is_timer_expired(now_ms=2_000_000) is False


def test_active_draft_timer_expiry_is_a_plain_comparison():
    """An unpaused draft's timer expires exactly when the clock passes it."""
    state = parse_draft(_raw_draft())
    assert state.paused is False
    assert state.is_timer_expired(now_ms=2_000_000) is True
    assert state.is_timer_expired(now_ms=999_999) is False


def test_live_auction_metadata_exposed_raw():
    """Live-auction fields pass through unparsed (interpretation is issue 4)."""
    state = parse_draft(_raw_draft())
    assert state.nominated_player_id == "4034"
    assert state.highest_offer == "23"
    assert state.offering_user_id == "111111111111111111"
    assert state.nominating_slot == "5"


def test_budget_by_slot_parses_settings_budget_n():
    """Once keepers are entered, per-team budgets arrive as budget_<slot>."""
    settings = {"budget": 200, "teams": 12, "budget_1": 143, "budget_5": 96}
    state = parse_draft(_raw_draft(settings=settings))
    assert state.budget_by_slot == {1: 143, 5: 96}


def test_budget_by_slot_empty_before_keepers_entered():
    """No budget_<slot> keys yet (keepers not entered) parses to empty."""
    state = parse_draft(_raw_draft())
    assert not state.budget_by_slot
    assert state.status == "drafting"
