"""The poller's budget rules: whose money is real, and what a guess may say.

Split out of ``test_dashboard_state.py`` when that module reached pylint's
1000-line module ceiling. Same seam as its parent — ``DashboardPoller.step``
/ ``.snapshot`` driven by scripted sources, no network and no wall clock —
and the same anti-cheat habit: every fixture here is a board on which an
implementation reading the wrong identifier, or the wrong source, returns a
detectably wrong number rather than passing by luck.

What lives here: budget provenance (a hand-keyed ``--budget`` outranking
Sleeper's ``budget_<slot>``), the fail-closed verdict a defaulted budget
withholds, the marks the page reads for my own money and for the room's,
and the draft-slot-not-roster-id rule the whole family rests on.
"""

from __future__ import annotations

from dataclasses import replace

from draftbot.tracker import DraftTracker

from .helpers_dashboard import (
    ENTERED_BUDGETS,
    MY_DRAFT_SLOT,
    PERMUTED_SLOTS,
    make_poller,
    make_tick,
    team_by_slot,
)
from .helpers_engine import sheet_row


def _keeper_board_rows():
    """A sheet for the three-keeper fixture: my kept players plus one
    buyable nominee."""
    return [
        sheet_row(1, "K1", "RB", 30.0),
        sheet_row(2, "K2", "WR", 25.0),
        sheet_row(3, "K3", "TE", 15.0),
        sheet_row(4, "B", "WR", 20.0),
        sheet_row(5, "C", "QB", 11.0),
    ]


def _keeper_board_poller(config, rows, *, overrides=None, budgets=None):
    """The 2026 shape: three keepers on my slot 7 and, unless ``budgets``
    says otherwise, a board where Sleeper carries NO budget_<slot> key
    for anyone."""
    tracker = DraftTracker(
        config,
        keepers_by_slot={7: ("K1", "K2", "K3")},
        budget_overrides=overrides,
        clock=lambda: 0.0,
    )
    return make_poller(
        config,
        [make_tick(nominee="B", offer=5, budgets=budgets)],
        rows,
        keepers_by_slot={7: ("K1", "K2", "K3")},
        tracker=tracker,
    )


def test_budget_override_moves_my_real_dollars_not_merely_the_flag(config):
    """ANTI-CHEAT, the point of the fail-closed budget work. Sleeper
    carries no budget_<slot> key for anyone; my slot 7 holds three
    keepers, so 12 of my 15 slots are open and my cap is pure
    subtraction: real budget minus $1 for each of the 11 OTHER open
    slots. The league default of $200 says $189. My real $96, keyed by
    hand, says $85.

    An implementation that flips budget_is_default but keeps feeding
    config.auction_budget to the tracker's TeamState — and through it to
    my_cap, max_bid, and the inflation numerator — still reads $189 here
    and fails. Pinning the flag alone would not have caught that."""
    rows = _keeper_board_rows()

    defaulted = _keeper_board_poller(config, rows).step()
    assert defaulted["me"]["remaining"] == 200
    assert defaulted["me"]["max_bid"] == 189
    assert defaulted["nomination"]["analysis"]["my_cap"] == 189
    assert team_by_slot(defaulted, 7)["budget_is_default"] is True

    overridden = _keeper_board_poller(config, rows, overrides={7: 96}).step()
    assert overridden["me"]["remaining"] == 96
    assert overridden["me"]["max_bid"] == 85
    assert overridden["nomination"]["analysis"]["my_cap"] == 85
    assert team_by_slot(overridden, 7)["budget_is_default"] is False
    # The room's other eleven slots were NOT overridden, so they stay
    # honestly labeled as defaulted.
    assert team_by_slot(overridden, 3)["budget_is_default"] is True


def test_verdict_is_suppressed_only_while_my_budget_is_a_guess(config):
    """The sixth fail-closed rule, and its deliberate limit.

    A BID/PASS call computed off a made-up budget is a confident wrong
    answer, so a defaulted budget suppresses the verdict. But suppressing
    unconditionally would blank the tool for the whole draft whenever the
    commissioner never enters budgets — trading a wrong number for no
    tool at all, which is worse. So the rule clears the moment my slot
    carries a REAL budget, from either source: a hand-keyed override, or
    a budget_<slot> key the commissioner finally entered.

    All three boards here are otherwise identical and priceable — live
    nomination, a recorded high bid, an unpaused draft — so nothing but
    the budget can explain the difference."""
    rows = _keeper_board_rows()

    guessed = _keeper_board_poller(config, rows).step()["nomination"]
    assert guessed["analysis"] is not None  # priced; only the CALL is withheld
    assert guessed["verdict"] is None
    assert "budget" in guessed["verdict_reason"]
    assert "--budget" in guessed["verdict_reason"]

    overridden = _keeper_board_poller(config, rows, overrides={7: 96}).step()
    assert overridden["nomination"]["verdict"] is not None
    assert overridden["nomination"]["verdict"]["action"] in {"BID", "PASS"}

    entered = _keeper_board_poller(config, rows, budgets={7: 96}).step()
    assert entered["nomination"]["verdict"] is not None
    assert entered["me"]["remaining"] == 96


def test_me_panel_marks_a_defaulted_budget_for_the_page(config):
    """The 34px green number is the figure read under a bid timer, and
    until now nothing in the `me` payload said it was a guess — the lone
    signals were a `*` in the 12-row team table and a banner that fires
    on every poll all night. The flag rides on `me` so the page can mark
    the number itself."""
    rows = _keeper_board_rows()

    assert _keeper_board_poller(config, rows).step()["me"]["budget_is_default"] is True
    overridden = _keeper_board_poller(config, rows, overrides={7: 96}).step()
    assert overridden["me"]["budget_is_default"] is False


def test_config_budget_lever_still_moves_the_money_but_not_the_verdict(config):
    """The pre-existing whole-league lever, pinned on both sides.

    Editing `[auction] budget` re-points the SAME fallback the tracker
    has always used, so the money it produces is unchanged by this work:
    $96 across my 12 open slots is still a cap of $85. What changed is
    the verdict. That lever sets every team in the room to one number,
    which is a fiction for the other eleven, and the flag stays True
    because the figure is still a default rather than a real per-team
    budget — so the BID/PASS call is now withheld and says so. Keying
    --budget for my slot is what restores it."""
    lever = replace(config, auction_budget=96)
    rows = _keeper_board_rows()

    state = _keeper_board_poller(lever, rows).step()

    assert state["me"]["remaining"] == 96
    assert state["me"]["max_bid"] == 85
    assert state["nomination"]["analysis"]["my_cap"] == 85
    assert state["me"]["budget_is_default"] is True
    assert state["nomination"]["verdict"] is None
    assert "--budget" in state["nomination"]["verdict_reason"]


def _permuted_poller(config, rows, *, budgets=None):
    """The permuted board: my three keepers sit on DRAFT slot 4 and slot 7
    is an opponent. My slot is resolved from the config's roster id against
    the map, never passed in, exactly as live mode resolves it."""
    keepers = {MY_DRAFT_SLOT: ("K1", "K2", "K3")}
    tracker = DraftTracker(config, keepers_by_slot=keepers, clock=lambda: 0.0)
    entries = [
        make_tick(
            nominee="B", offer=5, budgets=budgets, slot_to_roster_id=PERMUTED_SLOTS
        )
    ]
    return make_poller(config, entries, rows, keepers_by_slot=keepers, tracker=tracker)


def test_the_budget_rule_reads_my_draft_slot_and_not_the_roster_id(config):
    """ANTI-CHEAT for the one property the whole fail-closed budget rule
    rests on: it must read MY DRAFT SLOT.

    Every other dashboard fixture builds slot_to_roster_id as the identity
    map, and the config's roster id is 7, so "my slot" resolves to 7 in
    all of them and a rule hardcoded to `board.team(7)` passes every one.
    The hazard is not hypothetical: a draft carries the identity map only
    while it sits in pre_draft, and every completed season in this league
    carries a permuted one. So this board permutes, and both halves pin:

    * mine defaulted, slot 7 real -> verdict WITHHELD. A rule reading slot
      7 sees real money and advises off my fabricated budget.
    * mine real, slot 7 defaulted -> verdict RENDERS. A rule reading slot
      7 sees a default and blanks the tool while my money is fine.

    The withheld reason must also name slot 4: "pass --budget SLOT=AMOUNT"
    is unactionable at a 10-second timer, and an operator who reaches for
    his roster id funds slot 7, which is somebody else.
    """
    rows = _keeper_board_rows()

    mine_guessed = _permuted_poller(config, rows, budgets={7: 96}).step()
    assert mine_guessed["me"]["slot"] == MY_DRAFT_SLOT  # roster 7 sits here
    assert team_by_slot(mine_guessed, 7)["is_me"] is False
    assert team_by_slot(mine_guessed, 7)["budget_is_default"] is False
    assert mine_guessed["me"]["budget_is_default"] is True
    assert mine_guessed["nomination"]["analysis"] is not None  # priced, not blank
    assert mine_guessed["nomination"]["verdict"] is None
    reason = mine_guessed["nomination"]["verdict_reason"]
    assert "--budget 4=AMOUNT" in reason
    assert "--budget 7" not in reason
    assert "budget_4" in reason
    mine_real = _permuted_poller(config, rows, budgets={4: 96}).step()
    assert mine_real["me"]["slot"] == MY_DRAFT_SLOT
    assert mine_real["me"]["remaining"] == 96  # the real money, on MY slot
    assert mine_real["me"]["budget_is_default"] is False
    assert team_by_slot(mine_real, 7)["budget_is_default"] is True
    assert mine_real["nomination"]["verdict"] is not None


def test_a_hand_keyed_budget_outranks_a_sleeper_key(config):
    """ANTI-CHEAT for the decided precedence: explicit operator input
    beats remote data.

    The case that forced the decision is a commissioner who enters $200
    for all twelve keeper teams. Every key is present, so nothing is
    flagged and `budget_is_default` reads False, yet every figure in the
    room is fiction. Under Sleeper-first the override is discarded and the
    operator has no lever at all.

    So this board carries a real budget_7 of $200 AND `--budget 7=96`. My
    cap must pin at $85; an implementation that keeps Sleeper-first reads
    $189 and fails. The discard is not silent either: a standing banner
    names the slot and both amounts, because trading an invisible remote
    failure for an invisible local one would be no improvement."""
    rows = _keeper_board_rows()

    entered = _keeper_board_poller(config, rows, budgets=ENTERED_BUDGETS).step()
    assert entered["me"]["remaining"] == 200
    assert entered["me"]["budget_is_default"] is False  # a real key, just wrong
    assert entered["nomination"]["analysis"]["my_cap"] == 189
    assert entered["nomination"]["verdict"] is not None  # nothing flags it

    corrected = _keeper_board_poller(
        config, rows, budgets=ENTERED_BUDGETS, overrides={7: 96}
    ).step()
    assert corrected["me"]["remaining"] == 96
    assert corrected["me"]["max_bid"] == 85
    assert corrected["nomination"]["analysis"]["my_cap"] == 85
    assert corrected["me"]["budget_is_default"] is False
    assert corrected["nomination"]["verdict"] is not None
    (replaced,) = [
        warning
        for warning in corrected["settings_warnings"]
        if warning["field"] == "budget_override"
    ]
    assert "slot 7 = $96" in replaced["expected"]
    assert "slot 7 = $200" in replaced["actual"]
    # Filling a hole is the ordinary case and stays quiet.
    hole = _keeper_board_poller(config, rows, overrides={7: 96}).step()
    assert not [
        warning
        for warning in hole["settings_warnings"]
        if warning["field"] == "budget_override"
    ]


def test_the_room_flag_marks_fabricated_keeper_money_and_only_that(config):
    """My max bid is a function of ALL twelve budgets (inflation sums the
    room's money, the pace pool sums the other eleven teams' remaining),
    so keying my own slot clears the verdict while the recommended number
    still rides fabricated money. `defaulted_keeper_slots` marks it.

    ANTI-CHEAT on the scoping. The naive version — any team whose budget
    is defaulted — is checked here and fails: the second board has eleven
    defaulted slots, none holding keepers, and must report an EMPTY list.
    A defaulted budget is only provably wrong for a team that kept players
    and therefore paid for them; amber on a keeper-free board would be a
    permanent false warning on every lot."""
    rows = _keeper_board_rows()

    guessed = _keeper_board_poller(config, rows).step()
    assert guessed["defaulted_keeper_slots"] == [7]

    # My keeper slot is now covered; the other ELEVEN are still at the
    # league default, hold no keepers, and so fabricate nothing.
    covered = _keeper_board_poller(config, rows, overrides={7: 96}).step()
    defaulted = [t["slot"] for t in covered["teams"] if t["budget_is_default"]]
    assert defaulted == [slot for slot in range(1, 13) if slot != 7]
    assert covered["defaulted_keeper_slots"] == []

    # A second keeper team nobody entered: my verdict renders (my own
    # money is real) while the room figure stays marked.
    keepers = {7: ("K1", "K2", "K3"), 3: ("OFFSHEET",)}
    tracker = DraftTracker(config, keepers_by_slot=keepers, clock=lambda: 0.0)
    partial = make_poller(
        config,
        [make_tick(nominee="B", offer=5, budgets={7: 96})],
        rows,
        keepers_by_slot=keepers,
        tracker=tracker,
    ).step()
    assert partial["me"]["budget_is_default"] is False
    assert partial["nomination"]["verdict"] is not None
    assert partial["defaulted_keeper_slots"] == [3]
