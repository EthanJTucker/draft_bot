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


def _permuted_poller(config, rows, *, budgets=None, overrides=None):
    """The permuted board: my three keepers sit on DRAFT slot 4 and slot 7
    is an opponent. My slot is resolved from the config's roster id against
    the map, never passed in, exactly as live mode resolves it."""
    keepers = {MY_DRAFT_SLOT: ("K1", "K2", "K3")}
    tracker = DraftTracker(
        config,
        keepers_by_slot=keepers,
        budget_overrides=overrides,
        clock=lambda: 0.0,
    )
    entries = [
        make_tick(
            nominee="B", offer=5, budgets=budgets, slot_to_roster_id=PERMUTED_SLOTS
        )
    ]
    return make_poller(config, entries, rows, keepers_by_slot=keepers, tracker=tracker)


def _banners(state, field):
    """Every standing settings banner carrying ``field``."""
    return [
        warning for warning in state["settings_warnings"] if warning["field"] == field
    ]


def _missed_banners(state):
    """The standing banner for a --budget that never landed on my slot."""
    return _banners(state, "budget_override_missed_my_slot")


def _banner_text(state):
    """Every word the page would paint from this board's settings banners,
    so a test can assert that a string appears in NONE of them."""
    return " ".join(
        warning["field"] + warning["expected"] + warning["actual"]
        for warning in state["settings_warnings"]
    )


def test_an_override_that_missed_my_slot_is_named_on_every_poll(config):
    """The startup check, re-run against the LIVE board on every poll.

    The CLI resolves my draft slot once, from the draft object it fetched
    at startup. On draft night the dashboard comes up before the
    commissioner assigns the order, so that lone check runs against the
    placeholder slot map and cannot conclude anything — and a warning
    printed into scrollback before the order landed is not where the
    operator is looking anyway. So the same condition rides the board.

    This is the mis-key it exists for: my roster id is 7, my draft slot is
    4, and `--budget 7=96` funded an opponent. My own money is still the
    league default, so the banner fires, names slot 4, and gives the flag
    to type. Nothing downstream can detect this on its own: roster ids are
    1-12 as well, and slot 7 now holds a perfectly plausible $96.

    ANTI-CHEAT on both quiet cases, because a banner that fires on every
    board is the wallpaper this whole issue is about. Keyed correctly, it
    is silent. Given no override at all it is silent too — that board is
    the ordinary un-entered one, already carrying the keeper_budgets
    banner and a withheld verdict, and a second banner saying the same
    thing would only crowd them."""
    rows = _keeper_board_rows()

    mis_keyed = _permuted_poller(config, rows, overrides={7: 96}).step()
    assert mis_keyed["me"]["slot"] == MY_DRAFT_SLOT
    assert mis_keyed["me"]["budget_is_default"] is True  # the money went elsewhere
    assert team_by_slot(mis_keyed, 7)["remaining"] == 96
    (missed,) = _missed_banners(mis_keyed)
    assert "--budget 4=AMOUNT" in missed["expected"]
    assert "slot 7" in missed["actual"]
    # It must never tell me to key the slot my roster id happens to name.
    assert "--budget 7=AMOUNT" not in missed["expected"] + missed["actual"]

    keyed = _permuted_poller(config, rows, overrides={MY_DRAFT_SLOT: 96}).step()
    assert keyed["me"]["remaining"] == 96
    assert not _missed_banners(keyed)

    # A real budget_<slot> key for my slot covers it just as well: the
    # override landing elsewhere is then somebody else's problem, and the
    # discard banner is what names it.
    covered = _permuted_poller(
        config, rows, budgets={MY_DRAFT_SLOT: 96}, overrides={7: 143}
    ).step()
    assert not _missed_banners(covered)

    assert not _missed_banners(_permuted_poller(config, rows).step())


# The placeholder seats roster 7 at draft slot 7, so the keeper lists the
# rosters produce hang there until the order is dealt and moves them to 4.
PLACEHOLDER_SEAT = 7


def _pre_order_poller(config, rows, *, overrides=None, my_slot=None, slots=None):
    """The launch the README calls the likely one: the dashboard comes up
    BEFORE the commissioner deals the draft order, so the draft sits in
    pre_draft still carrying the placeholder map (slot N = roster N).

    ``slots`` replaces that map with a dealt one, for the anti-cheat board
    that is still pre_draft but whose order HAS landed. Keepers follow the
    map in force, exactly as ``keepers_by_slot_from_rosters`` builds them.
    """
    keepers = {MY_DRAFT_SLOT if slots else PLACEHOLDER_SEAT: ("K1", "K2", "K3")}
    tracker = DraftTracker(
        config,
        keepers_by_slot=keepers,
        budget_overrides=overrides,
        clock=lambda: 0.0,
    )
    entries = [make_tick(status="pre_draft", slot_to_roster_id=slots)]
    return make_poller(
        config, entries, rows, keepers_by_slot=keepers, tracker=tracker, my_slot=my_slot
    )


def test_no_budget_flag_is_judged_before_the_draft_order_is_dealt(config):
    """The startup guard's other half, on the surface the operator reads.

    Startup refuses to rule on --budget against a placeholder map because
    BOTH of its answers are wrong there. The page re-runs the same check
    every poll, on the same placeholder, and must refuse for the same
    reason — otherwise the instruction the CLI stopped printing simply
    moved onto a standing amber banner and got louder.

    ANTI-CHEAT in both directions, which is what the poller layer never
    had: every other board in this module carries a dealt order.

    * `--budget 4=96` is CORRECT for my eventual slot. Read off the
      placeholder my slot resolves to 7, my slot-7 money is the default,
      and the shipped guard fires — telling the operator to move his real
      money onto what becomes an opponent's row, in a sentence that
      prints 7 and 7 while asserting the two numbers differ.
    * `--budget 7=96` is the mis-key this whole guard exists to catch. It
      matches the placeholder, so the money lands and every provenance
      test reads clean. Going merely quiet here would be a clean bill on
      a board where the answer is unknowable, so the same banner fires
      and says only what is true: the order is not dealt.

    Silence is therefore wrong on BOTH boards, and each of the two
    trivially-wrong implementations (keep the old check; return nothing
    on a placeholder) fails one of them.
    """
    rows = _keeper_board_rows()

    correct = _pre_order_poller(config, rows, overrides={MY_DRAFT_SLOT: 96}).step()
    assert not _missed_banners(correct)
    # The instruction the CLI stopped printing must not reappear here.
    assert "--budget 7=AMOUNT" not in _banner_text(correct)
    assert "AMOUNT" not in _banner_text(correct)
    (undealt,) = _banners(correct, "budget_order_not_dealt")
    assert "slot 4" in undealt["actual"]
    assert "placeholder" in undealt["actual"]

    mis_keyed = _pre_order_poller(config, rows, overrides={PLACEHOLDER_SEAT: 96}).step()
    # The money DID land on the placeholder seat, which is exactly why
    # every provenance signal reads clean and a clean bill is tempting.
    assert mis_keyed["me"]["slot"] == PLACEHOLDER_SEAT
    assert mis_keyed["me"]["remaining"] == 96
    assert mis_keyed["me"]["budget_is_default"] is False
    assert not _missed_banners(mis_keyed)
    (undealt_mis,) = _banners(mis_keyed, "budget_order_not_dealt")
    assert "slot 7" in undealt_mis["actual"]
    assert "AMOUNT" not in _banner_text(mis_keyed)

    # Quiet without --budget: nothing was given, so nothing is unchecked.
    quiet = _pre_order_poller(config, rows).step()
    assert not _banners(quiet, "budget_order_not_dealt")
    assert not _missed_banners(quiet)


def test_the_page_guard_reads_the_slot_map_and_not_merely_the_status(config):
    """ANTI-CHEAT on the placeholder guard's two halves, the pair that
    keeps it from swallowing the warning it was added to protect.

    A guard keyed on `status == "pre_draft"` alone goes quiet on the first
    board here, where the order HAS been dealt and the mis-key is real and
    checkable. A guard that ignores an explicit --my-slot goes quiet on
    the second, where my slot came from the operator rather than from the
    map and an undealt order cannot make it stale — the same carve-out
    the startup check makes.
    """
    rows = _keeper_board_rows()

    dealt = _pre_order_poller(
        config, rows, overrides={PLACEHOLDER_SEAT: 96}, slots=PERMUTED_SLOTS
    ).step()
    assert dealt["status"] == "pre_draft"
    (missed,) = _missed_banners(dealt)
    assert "--budget 4=AMOUNT" in missed["expected"]
    assert "--budget 7=AMOUNT" not in missed["expected"] + missed["actual"]
    assert not _banners(dealt, "budget_order_not_dealt")

    told = _pre_order_poller(
        config, rows, overrides={PLACEHOLDER_SEAT: 96}, my_slot=MY_DRAFT_SLOT
    ).step()
    assert told["me"]["slot"] == MY_DRAFT_SLOT
    (missed_told,) = _missed_banners(told)
    assert "--budget 4=AMOUNT" in missed_told["expected"]
    assert not _banners(told, "budget_order_not_dealt")


def test_a_live_draft_on_the_identity_map_is_still_checked(config):
    """ANTI-CHEAT on the OTHER half of the placeholder guard, the one
    every board in this module left unpinned.

    The guard reads two facts and its docstring says both are required:
    the status is still ``pre_draft``, AND the slot map is still the
    identity. Every fixture next door varies the map, so the status half
    was free — dropping it left the suite entirely green while changing
    what the page says on a real board.

    This is that board. ``_keeper_board_poller`` is already ``drafting``
    on the identity map, which is exactly the shape a league gets when the
    commissioner never assigns a custom order: the placeholder is never
    permuted, so it survives into the live draft and stops being a
    placeholder at all. My roster id is 7 and the map seats it at slot 7,
    so an override keyed to slot 4 is a real, checkable mis-key here.

    A guard that reads the map alone calls this board provisional, prints
    "the order is not dealt" over a draft that is underway, and swallows
    the loud warning naming the flag to type.
    """
    rows = _keeper_board_rows()

    live = _keeper_board_poller(config, rows, overrides={MY_DRAFT_SLOT: 96}).step()

    assert live["status"] == "drafting"
    assert live["me"]["slot"] == PLACEHOLDER_SEAT  # identity map: roster 7 -> slot 7
    assert live["me"]["budget_is_default"] is True  # the money went to slot 4
    assert team_by_slot(live, MY_DRAFT_SLOT)["remaining"] == 96
    (missed,) = _missed_banners(live)
    assert "--budget 7=AMOUNT" in missed["expected"]
    assert "slot 4" in missed["actual"]
    # The placeholder banner belongs to a draft that has not started.
    assert not _banners(live, "budget_order_not_dealt")


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


def test_an_impossible_keeper_budget_is_marked_and_not_merely_announced(config):
    """The ceiling banner's proof reaches the page's marking feeds.

    The flat-league-budget board is the one this whole family exists for:
    every ``budget_<slot>`` key present at $200, my slot 7 holding three
    keepers that cannot leave more than $185. The banner says the figure
    is impossible, and until now every mark disagreed with it — my money
    green, my cap unmarked, the 30px max bid in confident accent blue.
    ``impossible_keeper_slots`` is what the page reads to mark them.

    ANTI-CHEAT on the two quiet boards, because a mark on every board is
    the wallpaper this issue is about: nothing entered at all stays empty
    (the keeper_budgets banner owns that hole, and the default is already
    marked by provenance), and a real affordable figure stays empty too.

    The verdict is deliberately NOT touched. My budget came from a real
    key, so ``budget_is_default`` stays False and the call still renders;
    marking a figure and blanking the tool are different remedies, and
    this is the marking one."""
    rows = _keeper_board_rows()

    flat = _keeper_board_poller(config, rows, budgets=ENTERED_BUDGETS).step()
    (ceiling,) = _banners(flat, "budget_ceiling")
    assert "slot 7 = $200" in ceiling["actual"]
    assert flat["impossible_keeper_slots"] == [7]
    assert flat["me"]["slot"] == 7
    # Untouched: a real key is a real key, and the call still renders.
    assert flat["me"]["budget_is_default"] is False
    assert flat["nomination"]["verdict"] is not None
    # Not the room-default mark either; these are different faults.
    assert flat["defaulted_keeper_slots"] == []

    unentered = _keeper_board_poller(config, rows).step()
    assert unentered["impossible_keeper_slots"] == []
    assert not _banners(unentered, "budget_ceiling")
    assert unentered["defaulted_keeper_slots"] == [7]

    affordable = _keeper_board_poller(config, rows, overrides={7: 96}).step()
    assert affordable["impossible_keeper_slots"] == []
    assert not _banners(affordable, "budget_ceiling")
