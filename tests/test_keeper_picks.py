"""Keepers that arrive as PRICED PICKS rather than as budget_<slot> keys.

The commissioner can enter a season's keepers either way. When they arrive
as picks (``is_keeper`` with a dollar ``metadata.amount``) the same player
is reachable from two sources at once — the roster's ``keepers`` array and
the picks feed — and three code paths used to assume he could only ever be
in the first: the budget resolver, the open-slot count, and the inflation
model's per-position spend.

Every fixture here is built on a NON-IDENTITY slot map, and every keeper
that can be is placed in BOTH sources. Both are load-bearing anti-cheats.
An identity map lets an implementation that reads a roster id where a
draft slot belongs pass by luck, and a keeper present in only one source
lets a single-source implementation pass by luck. The suite could not see
either confusion before this module.
"""

from __future__ import annotations

from draftbot.draft_engine import positional_inflation
from draftbot.tracker import DraftTracker, Sale, keepers_by_slot_from_rosters

from .conftest import raw_auction_pick, raw_keeper_pick
from .helpers_dashboard import make_poller, make_tick, team_by_slot
from .helpers_engine import make_board, sheet_row

# The draft order as this league actually deals it: a full permutation,
# fixing nothing. My roster id is 7 (league_config.toml) and it drafts
# from slot 8 here, so any rule that reads the roster id where the draft
# slot belongs lands on slot 7 — which this map hands to roster 12.
LIVE_SLOTS = {
    1: 6,
    2: 3,
    3: 9,
    4: 5,
    5: 10,
    6: 11,
    7: 12,
    8: 7,
    9: 8,
    10: 2,
    11: 4,
    12: 1,
}
MY_ROSTER_ID = 7
MY_SLOT = 8

# The 3-keeper team (mine, roster 7 -> slot 8) and the 2-keeper team
# (roster 9 -> slot 3). The live league carries exactly one of each shape,
# and the off-by-one in the open-slot count reads differently on each.
MY_KEEPERS = (("K1", "RB", 48), ("K2", "WR", 37), ("K3", "WR", 19))
MY_KEEPER_SPEND = 104
OTHER_KEEPERS = (("J1", "TE", 30), ("J2", "RB", 25))
OTHER_KEEPER_SPEND = 55

# A slot holding no keepers and carrying no budget key: the control that
# keeps the budget guard honest. Slot 5 seats roster 10 on the live map.
UNSIGNALLED_SLOT = 5

RAW_ROSTERS = (
    {"roster_id": MY_ROSTER_ID, "keepers": [pid for pid, _, _ in MY_KEEPERS]},
    {"roster_id": 9, "keepers": [pid for pid, _, _ in OTHER_KEEPERS]},
    {"roster_id": 10, "keepers": None},
)


def keeper_picks() -> list[dict]:
    """Both keeper teams' keepers as priced picks, in feed order.

    The SAME players the rosters carry: an implementation that counts only
    the picks feed and one that counts only the roster array both read
    every number here correctly, and only one that reconciles the two
    reads the open-slot count right.
    """
    picks = []
    for slot, keepers in ((MY_SLOT, MY_KEEPERS), (3, OTHER_KEEPERS)):
        for player_id, position, amount in keepers:
            picks.append(
                raw_keeper_pick(len(picks) + 1, player_id, slot, str(amount), position)
            )
    return picks


def keeper_tick(picks=None, **kwargs):
    """One tick on the live slot map, keeper picks already in the feed."""
    return make_tick(
        keeper_picks() + list(picks or []), slot_to_roster_id=LIVE_SLOTS, **kwargs
    )


def keeper_tracker(config, **kwargs):
    """A tracker bridged through the live map, exactly as startup builds it."""
    return DraftTracker(
        config,
        keepers_by_slot=keepers_by_slot_from_rosters(keeper_tick().draft, RAW_ROSTERS),
        clock=lambda: 0.0,
        **kwargs,
    )


def test_the_roster_bridge_seats_keepers_by_draft_slot_not_roster_id():
    """The fixture's own premise, asserted before anything rests on it:
    roster 7's keepers belong to draft slot 8, and slot 7 (roster 12)
    holds none. A bridge that skipped the map would put them on slot 7."""
    by_slot = keepers_by_slot_from_rosters(keeper_tick().draft, RAW_ROSTERS)

    assert by_slot[MY_SLOT] == ("K1", "K2", "K3")
    assert by_slot[3] == ("J1", "J2")
    assert by_slot[MY_ROSTER_ID] == ()


def test_a_keeper_in_both_sources_fills_exactly_one_roster_slot(config):
    """Anti-cheat on the double count. My three keepers sit in the roster
    array AND in the picks feed, so counting both sources says 15-3-3 = 9
    open slots and a $88 cap. The truth is 12 open slots and $85: each
    keeper takes one slot, and the cap reserves $1 for the other eleven."""
    board = keeper_tracker(config).update(keeper_tick())

    me = board.team(MY_SLOT)
    assert me.budget == 200
    assert me.spent == MY_KEEPER_SPEND
    assert me.remaining == 96
    assert me.keeper_count == 3
    assert me.purchase_count == 0
    assert me.open_slots == 12
    assert me.max_bid == 85


def test_the_two_keeper_team_reads_one_slot_further_open(config):
    """The other live shape. The off-by-one is proportional to the keeper
    count, so a fixture with only 3-keeper teams cannot tell a fix that
    subtracts the keepers twice from one that subtracts a constant."""
    board = keeper_tracker(config).update(keeper_tick())

    other = board.team(3)
    assert other.keeper_count == 2
    assert other.spent == OTHER_KEEPER_SPEND
    assert other.remaining == 145
    assert other.open_slots == 13
    assert other.max_bid == 133


def test_keepers_from_either_source_alone_still_count_once(config):
    """The union, from both sides. A keeper the picks feed carries but the
    roster array does not still fills a slot; a keeper the roster array
    carries but the feed does not still fills a slot. Reading only the
    intersection would count neither."""
    tracker = DraftTracker(config, keepers_by_slot={2: ("R1",)}, clock=lambda: 0.0)
    board = tracker.update(
        make_tick(
            [
                raw_keeper_pick(1, "F1", 4, "12", "RB"),
                raw_keeper_pick(2, "R1", 2, "9", "WR"),
            ],
            slot_to_roster_id=LIVE_SLOTS,
        )
    )

    feed_only = board.team(4)
    assert feed_only.keeper_count == 1
    assert feed_only.open_slots == 14
    assert feed_only.spent == 12

    both_sources = board.team(2)
    assert both_sources.keeper_count == 1
    assert both_sources.open_slots == 14
    assert both_sources.spent == 9


def test_needs_place_each_keeper_once_and_leave_twelve_unfilled(config):
    """``needs`` rides the same count. Placing my RB/WR/WR twice consumes
    the FLEX and a bench slot as well and reads 9 unfilled; placing them
    once leaves RB at 1, WR at 0, and 12 slots to buy."""
    board = keeper_tracker(config).update(keeper_tick())

    needs = board.team(MY_SLOT).needs
    assert sum(needs.values()) == 12
    assert needs["RB"] == 1
    assert needs["WR"] == 0
    assert needs["FLEX"] == 1
    assert needs["BN"] == 6


def test_purchases_after_the_keepers_still_debit_a_slot_each(config):
    """The keeper filter must not swallow ordinary auction buys sharing the
    feed with them: a $30 purchase takes a slot and $30."""
    board = keeper_tracker(config).update(
        keeper_tick([raw_auction_pick(90, "B", MY_SLOT, "30", "TE")])
    )

    me = board.team(MY_SLOT)
    assert me.purchase_count == 1
    assert me.keeper_count == 3
    assert me.spent == MY_KEEPER_SPEND + 30
    assert me.remaining == 66
    assert me.open_slots == 11
    assert me.max_bid == 56


def test_priced_keepers_are_real_money_and_a_silent_slot_still_is_not(config):
    """Both halves of the budget guard, on one board.

    Slot 8's keepers carry dollars in the feed, so its money is derived,
    not guessed, and must not read as defaulted. Slot 5 carries no keeper
    pick, no ``budget_<slot>`` key and no override, so it has no budget
    signal at all and must still fail closed. A guard that only proved the
    first half would pass with the flag wired to ``False``.
    """
    board = keeper_tracker(config).update(keeper_tick())

    assert board.team(MY_SLOT).budget_is_default is False
    assert board.team(3).budget_is_default is False
    assert board.team(UNSIGNALLED_SLOT).budget_is_default is True
    assert board.team(UNSIGNALLED_SLOT).budget == 200

    named = [
        warning
        for warning in board.settings_warnings
        if warning.field == "keeper_budgets"
    ]
    assert named == []


def test_a_keeper_slot_with_unpriced_picks_still_fails_closed(config):
    """A keeper pick whose amount will not parse carries no money, so it
    proves nothing: the slot falls back to the default and stays marked.
    Keying on ``is_keeper`` alone would clear the flag on a feed that
    never said what the keeper cost."""
    unpriced = raw_keeper_pick(1, "K1", MY_SLOT, "", "RB")
    tracker = DraftTracker(
        config, keepers_by_slot={MY_SLOT: ("K1",)}, clock=lambda: 0.0
    )
    board = tracker.update(make_tick([unpriced], slot_to_roster_id=LIVE_SLOTS))

    me = board.team(MY_SLOT)
    assert me.budget_is_default is True
    assert me.keeper_count == 1
    assert me.open_slots == 14
    assert [warning.field for warning in board.settings_warnings].count(
        "keeper_budgets"
    ) == 1


def test_measured_keeper_spend_raises_no_impossible_money_banner(config):
    """The ceiling rule infers a keeper's cost from the $5 floor because it
    cannot see one. On a priced slot it can: the budget is the whole $200
    by construction and the keeper dollars are already spend, so the
    inequality has nothing left to test and must stay quiet."""
    board = keeper_tracker(config).update(keeper_tick())

    assert not board.impossible_keeper_slots


def test_a_budget_override_on_a_priced_slot_is_refused_and_named(config):
    """AC 5. ``--budget 8=96`` is the post-keeper figure off the league
    sheet; the feed already subtracted the same $104, so honoring it would
    show my team $8 in the hole. The measured money wins and the conflict
    is named — silently discarding either number is what this forbids."""
    board = keeper_tracker(config, budget_overrides={MY_SLOT: 96}).update(keeper_tick())

    me = board.team(MY_SLOT)
    assert me.budget == 200
    assert me.remaining == 96
    assert me.budget_is_default is False

    conflicts = [
        warning
        for warning in board.settings_warnings
        if warning.field == "budget_keeper_conflict"
    ]
    assert len(conflicts) == 1
    assert f"slot {MY_SLOT}" in conflicts[0].actual
    assert "$96" in conflicts[0].actual


def test_a_sleeper_budget_key_on_a_priced_slot_is_refused_and_named(config):
    """The same conflict from the other source. A ``budget_8`` key entered
    beside priced keeper picks subtracts the keeper money twice as surely
    as a hand-keyed one does, and the banner must not blame the flag for
    a figure Sleeper supplied."""
    board = keeper_tracker(config).update(keeper_tick(budgets={MY_SLOT: 96}))

    assert board.team(MY_SLOT).remaining == 96
    conflicts = [
        warning
        for warning in board.settings_warnings
        if warning.field == "budget_keeper_conflict"
    ]
    assert len(conflicts) == 1
    assert "budget_8" in conflicts[0].actual


def test_the_board_carries_the_keeper_flag_on_every_sale(config):
    """The engine reads keeper-ness off the board, not off a second copy
    of the feed, so the flag has to survive the tracker."""
    board = keeper_tracker(config).update(
        keeper_tick([raw_auction_pick(90, "B", MY_SLOT, "30", "TE")])
    )

    by_id = {sale.player_id: sale for sale in board.sales}
    assert by_id["K1"].is_keeper is True
    assert by_id["K1"].draft_slot == MY_SLOT
    assert by_id["B"].is_keeper is False


# --- the inflation model -------------------------------------------------


def _inflation_rows():
    """A two-position sheet with the keeper priced well above the tail, so
    excluding him moves the ratio by more than rounding. Four roster slots
    and these budgets keep every ratio below the model's ceiling, where a
    clamped board would compare equal whatever the rule did."""
    return [
        sheet_row(1, "K1", "RB", 65.0),
        sheet_row(2, "R2", "RB", 40.0),
        sheet_row(3, "R3", "RB", 20.0),
        sheet_row(4, "W1", "WR", 30.0),
        sheet_row(5, "W2", "WR", 10.0),
    ]


def test_a_priced_keeper_prices_the_room_exactly_as_a_kept_player_does():
    """The whole point of AC 3, stated as an equality.

    Left board: the keeper is a roster keeper, his $48 already netted out
    of a $60 budget_<slot>. Right board: the identical team, the same
    keeper reaching the model as a $48 keeper PICK against the full $108.
    The two describe ONE room, so they must price it identically — same
    discretionary money, same pool, no position debited by a chain price.
    """
    rows = _inflation_rows()
    kept = make_board({1: 60, 2: 60}, keeper_counts={1: 1}, drafted_slots=4)
    priced = make_board(
        {1: 108, 2: 60}, [Sale("K1", 48, 1, is_keeper=True)], drafted_slots=4
    )

    assert positional_inflation(rows, kept, {1: ("K1",)}) == positional_inflation(
        rows, priced
    )


def test_the_same_dollars_as_an_auction_sale_move_the_ratio():
    """Anti-cheat on the equality above: it must come from the keeper rule,
    not from a model too blunt to notice a $48 sale at all, or from a
    clamp both boards hit. Flip the one flag and the RB ratio moves."""
    rows = _inflation_rows()
    priced = make_board(
        {1: 108, 2: 60}, [Sale("K1", 48, 1, is_keeper=True)], drafted_slots=4
    )
    bought = make_board({1: 108, 2: 60}, [Sale("K1", 48, 1)], drafted_slots=4)

    assert (
        positional_inflation(rows, priced)["RB"]
        != positional_inflation(rows, bought)["RB"]
    )


def test_keeper_dollars_never_count_as_money_chasing_the_pool():
    """The other half of the ratio. Keeper spend has already left the room,
    so a board carrying it must not price like the same room that still
    holds the money — which is what happens when the per-position spend is
    filtered and the money term is not."""
    rows = _inflation_rows()
    priced = make_board(
        {1: 108, 2: 60}, [Sale("K1", 48, 1, is_keeper=True)], drafted_slots=4
    )
    unspent = make_board({1: 108, 2: 60}, keeper_counts={1: 1}, drafted_slots=4)

    assert (
        positional_inflation(rows, priced)["WR"]
        < positional_inflation(rows, unspent, {1: ("K1",)})["WR"]
    )


# --- the page ------------------------------------------------------------


def _page_rows():
    return [
        sheet_row(1, "K1", "RB", 55.0),
        sheet_row(2, "K2", "WR", 40.0),
        sheet_row(3, "K3", "WR", 22.0),
        sheet_row(4, "B", "TE", 18.0),
    ]


def _page_poller(config, **kwargs):
    tracker = keeper_tracker(config, **kwargs)
    return make_poller(
        config,
        [keeper_tick(nominee="B", offer=5)],
        _page_rows(),
        keepers_by_slot=keepers_by_slot_from_rosters(keeper_tick().draft, RAW_ROSTERS),
        tracker=tracker,
        my_slot=MY_SLOT,
    )


def test_the_verdict_is_not_withheld_on_a_priced_keeper_board(config):
    """The live symptom: with no ``budget_<slot>`` key anywhere, the tool
    used to blank the call for every lot of the draft. The money is real,
    so the call is made — and the room's amber mark comes off with it."""
    state = _page_poller(config).step()

    nomination = state["nomination"]
    assert nomination["verdict"] is not None
    assert state["defaulted_keeper_slots"] == []
    assert team_by_slot(state, MY_SLOT)["budget_is_default"] is False


def test_a_board_with_no_budget_signal_still_blanks_the_verdict(config):
    """The control the fix must not break: strip the keeper prices and the
    same board has nothing to derive my money from, so it fails closed."""
    tracker = DraftTracker(
        config,
        keepers_by_slot=keepers_by_slot_from_rosters(keeper_tick().draft, RAW_ROSTERS),
        clock=lambda: 0.0,
    )
    poller = make_poller(
        config,
        [make_tick(nominee="B", offer=5, slot_to_roster_id=LIVE_SLOTS)],
        _page_rows(),
        keepers_by_slot=keepers_by_slot_from_rosters(keeper_tick().draft, RAW_ROSTERS),
        tracker=tracker,
        my_slot=MY_SLOT,
    )
    nomination = poller.step()["nomination"]

    assert nomination["verdict"] is None
    assert "league-wide default" in nomination["verdict_reason"]


def test_my_roster_panel_lists_each_keeper_once_at_its_keeper_price(config):
    """The panel read every keeper twice — once from the roster array with
    a null price, once from the feed with its dollars. One row per player,
    carrying both the price and the keeper mark."""
    roster = _page_poller(config).step()["me"]["roster"]

    assert [entry["player_id"] for entry in roster] == ["K1", "K2", "K3"]
    assert [entry["price"] for entry in roster] == [48, 37, 19]
    assert all(entry["keeper"] for entry in roster)
