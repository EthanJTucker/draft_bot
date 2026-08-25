"""The keeper bridge, and the draft order moving out from under it.

Keeper lists arrive from the rosters keyed by ROSTER ID and are bridged
onto DRAFT SLOTS once, at startup. My slot is re-resolved from the live
board every tick, so a draft order dealt after startup makes the two
halves describe different teams. This module holds that one fail-closed
rule and the only fixtures in the suite that drive two DIFFERENT slot
maps through one poller.

Split out of ``test_dashboard_state.py`` when that module reached
pylint's 1000-line ceiling; see also ``test_dashboard_verdict.py`` (the
rest of the verdict chain) and ``test_dashboard_budget.py`` (the
budget-provenance family).
"""

from __future__ import annotations

from draftbot.tracker import DraftTracker

from .helpers_dashboard import (
    IDENTITY_SLOTS,
    MY_DRAFT_SLOT,
    PERMUTED_SLOTS,
    make_poller,
    make_tick,
    team_by_slot,
)
from .helpers_engine import sheet_row

# Every slot funded for real, at a figure three keepers can leave: this
# family is about the keeper BRIDGE, so nothing here may be explained by a
# defaulted budget or an impossible one.
_FUNDED_ROOM = {slot: 96 for slot in range(1, 13)}
# The keeper lists as `keepers_by_slot_from_rosters` builds them at
# startup: my roster id is 7, and on the placeholder map roster 7 sits at
# draft slot 7.
_MY_KEEPERS = ("KA", "KB", "KC")
# The one buyable player on the sheet, so the nominated lot prices.
_NOMINEE = "N"


def _stale_map_rows():
    """My three kept players plus the single buyable nominee."""
    return [
        sheet_row(1, "KA", "QB", 28.0, name="Kept Passer"),
        sheet_row(2, "KB", "RB", 22.0, name="Kept Back"),
        sheet_row(3, "KC", "TE", 12.0, name="Kept End"),
        sheet_row(4, _NOMINEE, "WR", 18.0, name="The Nominee"),
    ]


def _bridged_poller(config, rows, startup_slots, tick_slots, **tick_kwargs):
    """A poller whose keeper bridge was built against ``startup_slots``,
    then fed one tick per entry in ``tick_slots``.

    This is the only fixture in the suite that drives two DIFFERENT slot
    maps through one poller, which is the whole shape of the defect: the
    bridge is built once and the board is re-read every tick.

    ``tick_kwargs`` go to every tick, which is how a stale board can also
    carry one of the conditions this rule has to outrank.
    """
    keepers = {
        slot: _MY_KEEPERS if roster_id == config.my_roster_id else ()
        for slot, roster_id in startup_slots.items()
    }
    tracker = DraftTracker(
        config,
        keepers_by_slot=keepers,
        keeper_slot_map=startup_slots,
        clock=lambda: 0.0,
    )
    entries = [
        make_tick(
            nominee=_NOMINEE,
            offer=5,
            budgets=_FUNDED_ROOM,
            slot_to_roster_id=slots,
            **tick_kwargs,
        )
        for slots in tick_slots
    ]
    return make_poller(config, entries, rows, keepers_by_slot=keepers, tracker=tracker)


def _stale_banners(state):
    return [
        warning
        for warning in state["settings_warnings"]
        if warning["field"] == "keeper_map_stale"
    ]


def test_a_draft_order_dealt_after_startup_blanks_the_tool_and_says_restart(config):
    """The eighth fail-closed rule, and the only two-map fixture here.

    Keeper lists arrive from the rosters keyed by ROSTER ID and are
    bridged onto DRAFT SLOTS once, at startup, through whatever
    ``slot_to_roster_id`` the draft carried then. Nothing re-fetches the
    rosters. My slot, though, is re-resolved from the live board every
    tick — so the moment the commissioner deals the order the two halves
    describe different teams, and the panel labelled MY TEAM lists players
    the operator does not own, with the needs and the max bid to match.

    The full repair re-derives the bridge every tick (issue #23). Until
    then this fails closed: the board is not silently wrong, it stops
    advising and says to restart.

    ANTI-CHEAT, four pollers over seven boards, and each of the
    trivially-wrong implementations dies on one of them:

    * tick 1 is the SAME map the bridge was built on, and it must be
      completely quiet with a live verdict. An implementation that fires
      whenever a startup map was declared fails here.
    * tick 2 permutes it, and must raise the banner AND withhold the
      verdict. An implementation that compares the live map to itself, or
      that decides staleness once and caches it, sees no change on this
      tick and fails.
    * the ``steady`` poller's map never moves across two ticks, so a rule
      that keys on "not the first tick" rather than on the map fails there.
    * the last two boards are stale AND paused, and stale AND served a
      degraded picks feed. A rule demoted below either predecessor hands
      back that predecessor's reason and fails there.

    The mis-attribution itself is measured, not assumed: on the permuted
    tick my roster panel is empty and my three keepers are still counted
    against slot 7, which is now somebody else.
    """
    rows = _stale_map_rows()
    poller = _bridged_poller(
        config, rows, IDENTITY_SLOTS, [IDENTITY_SLOTS, PERMUTED_SLOTS]
    )

    fresh = poller.step()
    assert fresh["me"]["slot"] == 7  # placeholder seat: roster 7 at slot 7
    assert [entry["player_id"] for entry in fresh["me"]["roster"]] == list(_MY_KEEPERS)
    assert not _stale_banners(fresh)
    assert fresh["nomination"]["verdict"] is not None
    # The flag the page's amber reads, in the QUIET direction too: an
    # export hard-wired to True marks a correctly bridged board's 30px
    # figure as untrustworthy, which trains the operator to ignore it.
    assert fresh["keeper_map_stale"] is False

    dealt = poller.step()
    assert dealt["me"]["slot"] == MY_DRAFT_SLOT  # roster 7 now drafts from slot 4
    # The defect, measured. The bridge still hangs my keepers on slot 7.
    assert dealt["me"]["roster"] == []
    assert dealt["me"]["open_slots"] == 15  # three keepers unaccounted for
    assert team_by_slot(dealt, 7)["open_slots"] == 12
    # So the tool stops, loudly, in the one way the operator can act on.
    (stale,) = _stale_banners(dealt)
    assert "RESTART" in stale["actual"]
    # And the 30px MAX BID tile stops reading as a fact. The page cannot
    # see the banner, so the flag has to reach it through /state: without
    # this key `guessedRoom` is false on this exact board (both slot sets
    # below are empty) and the figure paints in confident accent blue.
    assert dealt["keeper_map_stale"] is True
    assert dealt["defaulted_keeper_slots"] == []
    assert dealt["impossible_keeper_slots"] == []
    assert dealt["nomination"]["analysis"] is not None  # priced; the CALL is withheld
    assert dealt["nomination"]["verdict"] is None
    assert "RESTART" in dealt["nomination"]["verdict_reason"]
    # Nothing else on this board can explain the suppression.
    assert dealt["me"]["budget_is_default"] is False
    assert dealt["paused"] is False
    assert dealt["nomination"]["status"] == "live"

    # And it STAYS stopped. The comparison is against the map the bridge
    # was built on, not against the previous tick: a rule that re-baselines
    # each poll fires once at the transition and then hands back a
    # confident verdict on a board that is still wrong.
    still = poller.step()
    assert _stale_banners(still)
    assert still["nomination"]["verdict"] is None

    steady = _bridged_poller(
        config, rows, PERMUTED_SLOTS, [PERMUTED_SLOTS, PERMUTED_SLOTS]
    )
    for _ in range(2):
        state = steady.step()
        assert not _stale_banners(state)
        assert state["nomination"]["verdict"] is not None

    # And it LEADS the chain. These two boards are stale AND carry a
    # predecessor, which is the pairing draft night actually produces: a
    # commissioner very plausibly pauses the draft to assign the order,
    # and the picks feed is at its most unsettled at draft start. The
    # rule's whole argument is that those clear themselves while this one
    # never does without an operator action, so demoting it below either
    # hands back the predecessor's reason and fails here.
    paused_board = _bridged_poller(
        config, rows, IDENTITY_SLOTS, [PERMUTED_SLOTS], paused=True
    ).step()
    assert paused_board["paused"] is True  # the predecessor really is true
    assert "RESTART" in paused_board["nomination"]["verdict_reason"]

    untrusted_board = _bridged_poller(
        config, rows, IDENTITY_SLOTS, [PERMUTED_SLOTS], stale=("picks",)
    ).step()
    assert untrusted_board["nomination"]["status"] == "untrusted"
    assert "RESTART" in untrusted_board["nomination"]["verdict_reason"]
