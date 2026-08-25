"""The override layer inside the engine: applied last, capped by reality.

Every board here is built on a PERMUTED slot map. The rest of this
suite's hand fixtures seat roster id N at draft slot N, which makes them
structurally blind to slot/roster confusion -- and this league assigns
draft order at draft time, so the two identifiers genuinely differ on the
night. Here roster id 7 (the config's ``my_roster_id``) sits at draft
SLOT 4, and draft slot 7 belongs to roster id 11. Nothing below passes
``my_slot``: the engine must resolve my seat through the map, so any code
that reaches for the roster id lands on somebody else's money.
"""

from __future__ import annotations

from draftbot.draft_engine import analyze_player
from draftbot.overrides import PlayerOverride
from draftbot.tracker import BoardState, Sale, TeamState

from .helpers_engine import sheet_row

#: Draft slot -> roster id. A real permutation: no slot holds its own
#: number, and MY roster id (7) is seated at slot 4.
SLOT_TO_ROSTER = {1: 12, 2: 5, 3: 9, 4: 7, 5: 1, 6: 3, 7: 11, 8: 2, 9: 4, 10: 6}

#: My DRAFT slot, derived from the map rather than written down twice.
MY_SLOT = next(slot for slot, roster in SLOT_TO_ROSTER.items() if roster == 7)

DRAFTED_SLOTS = 15


def rows():
    """A sheet rich enough that inflation, need and the spend schedule
    all have something to do."""
    return [
        sheet_row(1, "rb40", "RB", 40.0),
        sheet_row(2, "wr36", "WR", 36.0),
        sheet_row(3, "rb31", "RB", 31.0),
        sheet_row(4, "wr24", "WR", 24.0),
        sheet_row(5, "qb18", "QB", 18.0),
        sheet_row(6, "te14", "TE", 14.0),
        sheet_row(7, "rb11", "RB", 11.0),
        sheet_row(8, "wr9", "WR", 9.0),
        sheet_row(9, "qb6", "QB", 6.0),
        sheet_row(10, "k2", "K", 2.0),
    ]


def board(sales=(), budgets=None, *, drafted_slots=DRAFTED_SLOTS):
    """A consistent board on the PERMUTED map: spend, purchase counts and
    open slots all derive from the sales, exactly as the tracker computes
    them, so no fixture can claim a sale without debiting the buyer."""
    budgets = budgets or {slot: 200 for slot in SLOT_TO_ROSTER}
    teams = []
    for slot in sorted(SLOT_TO_ROSTER):
        purchases = [sale for sale in sales if sale.draft_slot == slot]
        teams.append(
            TeamState(
                slot=slot,
                roster_id=SLOT_TO_ROSTER[slot],
                budget=budgets[slot],
                budget_is_default=False,
                spent=sum(sale.amount or 0 for sale in purchases),
                keeper_count=0,
                purchase_count=len(purchases),
                open_slots=max(0, drafted_slots - len(purchases)),
                needs={},
            )
        )
    return BoardState(
        status="drafting", paused=False, teams=tuple(teams), sales=tuple(sales)
    )


def book(**fields):
    """One override row for ``rb40``, defaults everywhere else."""
    defaults = {
        "player_id": "rb40",
        "name": None,
        "tier": None,
        "target": False,
        "avoid": False,
        "delta": 0,
        "note": None,
    }
    return {"rb40": PlayerOverride(**{**defaults, **fields})}


def test_the_permuted_map_seats_me_away_from_my_roster_id(config):
    """The fixture's own guard: if this ever collapses to identity, every
    slot/roster assertion below silently stops testing anything. The last
    line pins that the engine resolves MY seat through the map -- slot 4
    holds fifteen open slots' worth of money, slot 7 belongs to roster
    id 11, and reading the roster id as a slot would price the wrong team."""
    assert MY_SLOT == 4
    assert SLOT_TO_ROSTER[MY_SLOT] == 7
    assert SLOT_TO_ROSTER[7] != 7
    assert all(slot != roster for slot, roster in SLOT_TO_ROSTER.items())

    thin = board(budgets={**{slot: 200 for slot in SLOT_TO_ROSTER}, MY_SLOT: 60})
    assert (
        analyze_player("rb40", rows(), thin, config).my_cap
        == thin.team(MY_SLOT).max_bid
    )
    assert thin.team(MY_SLOT).max_bid != thin.team(7).max_bid


def test_a_delta_moves_the_bid_by_exactly_the_delta_on_two_boards(config):
    """Anti-cheat, and the reason two boards: on ONE board an
    implementation that sets an absolute price passes, and so does one
    that folds the delta in BEFORE the spend margin (a 0.93 margin turns
    +$8 into +$7). Both die against a fixed difference on two boards
    whose model prices differ."""
    early = board()
    late = board(
        sales=[
            Sale("wr36", 30, 1),
            Sale("rb31", 26, 2),
            Sale("wr24", 19, 3),
            Sale("qb18", 14, 5),
        ]
    )

    for state in (early, late):
        base = analyze_player("rb40", rows(), state, config)
        tweaked = analyze_player("rb40", rows(), state, config, overrides=book(delta=8))
        assert tweaked.max_bid == base.max_bid + 8
        assert tweaked.max_bid < tweaked.my_cap  # the cap is not binding here

    assert (
        analyze_player("rb40", rows(), early, config).max_bid
        != analyze_player("rb40", rows(), late, config).max_bid
    )


def test_the_anti_cheat_boards_really_do_scale_dollars(config):
    """Guard on the guard above: the +$8 test only kills the
    delta-before-the-margin mutant while the margin is actually below
    par (0.93 × 8 = 7.44, a different integer). If a later calibration
    moves the schedule to 1.0 here, that test silently stops proving it."""
    early = analyze_player("rb40", rows(), board(), config)
    assert early.spend_margin < 1.0
    assert round(early.spend_margin * 8) != 8


def test_an_override_above_my_cap_is_clamped_and_says_so(config):
    """``my_cap`` is wire reality, not a preference: ``remaining -
    (open_slots - 1)`` is what Sleeper accepts while leaving $1 a slot.
    So it stays outermost. But a silently-eaten tweak reads as "my file
    did not load", so the record must carry the whole story."""
    thin = board(sales=[Sale("wr36", 60, MY_SLOT)])
    cap = thin.team(MY_SLOT).max_bid
    model = analyze_player("rb40", rows(), thin, config).max_bid
    # The cap is NOT binding until the override lands, so the clamp below
    # is provably the override's doing and not a pre-existing ceiling.
    assert (cap, model) == (127, 107)

    capped = analyze_player("rb40", rows(), thin, config, overrides=book(delta=40))

    assert capped.max_bid == 127
    assert capped.override.clamped is True
    assert capped.override.pre_cap == 147
    assert capped.override.model_max_bid == 107
    assert capped.override.label == (
        "override +$40: model $107 → $147, CAPPED at $127 by my remaining budget"
    )


def test_an_override_that_stays_under_the_cap_is_not_marked_clamped(config):
    """The other direction, so ``clamped`` cannot be hard-coded True: a
    permanently-amber cap warning is a warning nobody reads."""
    tweaked = analyze_player("rb40", rows(), board(), config, overrides=book(delta=8))

    assert tweaked.override.clamped is False
    assert "CAPPED" not in tweaked.override.label
    assert tweaked.override.pre_cap == tweaked.max_bid


def test_avoid_zeroes_the_bid_exactly(config):
    """Exactly $0, not $1. The ``max(1, ...)`` floor is the trap: an
    off-by-one here tells the operator to bid a dollar on a player he
    marked never. A negative delta still floors at $1, which is what
    keeps the two levers distinguishable."""
    avoided = analyze_player(
        "rb40", rows(), board(), config, overrides=book(avoid=True)
    )
    assert avoided.max_bid == 0

    discounted = analyze_player(
        "rb40", rows(), board(), config, overrides=book(delta=-10_000)
    )
    assert discounted.max_bid == 1


def test_a_delta_cannot_resurrect_a_bid_on_a_full_roster(config):
    """The two-decision-site trap. The max bid used to be assigned in an
    ``if me.open_slots > 0`` branch AND hard-coded to 0 in the ``else``.
    An override wired into the first branch only looks perfect on every
    ordinary board and silently no-ops here -- and ``avoid`` would look
    correct BY ACCIDENT, because 0 is what that branch already said. So
    the case that proves the single seam is a POSITIVE delta on a roster
    with nothing left to fill."""
    full = board(
        sales=[Sale(f"x{n}", 1, MY_SLOT) for n in range(DRAFTED_SLOTS)],
        drafted_slots=DRAFTED_SLOTS,
    )
    assert full.team(MY_SLOT).open_slots == 0

    tweaked = analyze_player("rb40", rows(), full, config, overrides=book(delta=25))

    assert tweaked.max_bid == 0
    assert tweaked.override.clamped is True
    assert "CAPPED at $0" in tweaked.override.label


def test_an_override_on_another_player_leaves_this_lot_alone(config):
    """The join is by player id and nothing else. A book keyed on someone
    else must not leak a delta onto the nominee, and must leave the
    record's ``override`` empty rather than an all-zero one -- an empty
    record on screen would claim a row the operator never wrote."""
    elsewhere = {"wr36": book(delta=40)["rb40"]}
    baseline = analyze_player("rb40", rows(), board(), config)

    other = analyze_player("rb40", rows(), board(), config, overrides=elsewhere)

    assert other.max_bid == baseline.max_bid
    assert other.override is None


def test_a_listed_player_with_no_dollar_opinion_keeps_the_model_number(config):
    """A row carrying only a tier, a note and a target flag is display
    only: the number must not move, but the record must exist so the page
    can show the chips."""
    baseline = analyze_player("rb40", rows(), board(), config)

    listed = analyze_player(
        "rb40",
        rows(),
        board(),
        config,
        overrides=book(target=True, tier=1, note="volume bump"),
    )

    assert listed.max_bid == baseline.max_bid
    assert listed.override.delta == 0
    assert (listed.override.target, listed.override.tier) == (True, 1)
    assert listed.override.note == "volume bump"
    assert listed.override.label == "override listed (no dollar change)"
