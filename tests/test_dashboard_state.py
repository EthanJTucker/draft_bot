"""The dashboard poller: one poll cycle in, one JSON-ready snapshot out.

Every test drives the public seam (``DashboardPoller.step`` /
``.snapshot``) with scripted sources — no network, no wall clock, no
FastAPI. The fixtures are anti-cheat by construction: budgets where spent
and remaining differ, verdicts at exact bid-equality, profit with an
asymmetric sign, and boards that a fail-open renderer would misread.

The budget-provenance family (the ``--budget`` override, its precedence
over Sleeper's keys, the fail-closed verdict and the page's marks) lives
in ``test_dashboard_budget.py``: this module reached pylint's 1000-line
ceiling, and splitting it beat adding a disable.
"""

from __future__ import annotations

from draftbot.sleeper_client import SleeperUnavailableError
from draftbot.tracker import DraftTracker

from .conftest import raw_auction_pick
from .helpers_dashboard import (
    ENTERED_BUDGETS,
    IDENTITY_SLOTS,
    MY_DRAFT_SLOT,
    PERMUTED_SLOTS,
    make_poller,
    make_tick,
    team_by_slot,
)
from .helpers_engine import sheet_row


def test_budgets_render_as_remaining_never_spent(config):
    """A team that spent $37 of $200 must show $163 — the remaining figure —
    with open slots and the max possible bid; the snapshot carries no
    'spent' key at all, so a page cannot even bind the wrong number."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "RB", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    entries = [make_tick([raw_auction_pick(1, "A", 3, "37")])]
    state = make_poller(config, entries, rows).step()

    team = team_by_slot(state, 3)
    assert team["remaining"] == 163
    assert team["open_slots"] == 14
    assert team["max_bid"] == 163 - 13  # $1 held for every OTHER open slot
    assert "spent" not in team
    assert team_by_slot(state, 1)["remaining"] == 200
    assert team_by_slot(state, 7)["is_me"] is True  # roster 7 -> slot 7 here
    assert team_by_slot(state, 3)["is_me"] is False


def test_remaining_players_exclude_sold_and_sales_are_listed(config):
    """The positional table only offers players still buyable; the sale
    ledger carries the sold player with his hammer price and buying slot."""
    rows = [
        sheet_row(1, "A", "WR", 37.0, name="Big Wideout"),
        sheet_row(2, "B", "RB", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    entries = [make_tick([raw_auction_pick(1, "A", 3, "37")])]
    state = make_poller(config, entries, rows).step()

    assert [player["player_id"] for player in state["players"]] == ["B", "C"]
    assert state["sales"] == [
        {
            "player_id": "A",
            "name": "Big Wideout",
            "position": "WR",
            "amount": 37,
            "slot": 3,
        }
    ]
    assert state["ok"] is True
    assert state["poll_count"] == 1


def test_new_nomination_is_visible_in_the_next_snapshot(config):
    """The latency criterion: a nomination that appears in the source is in
    the very next snapshot — live status, priced analysis, and a verdict."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    entries = [
        # MOVED for the budget rule: this test is about latency, not about
        # budget provenance, so its board carries entered budgets. Without
        # them the verdict is correctly withheld and the latency claim
        # could not be made at all.
        make_tick(budgets=ENTERED_BUDGETS),
        make_tick(
            nominee="B",
            offer=5,
            nominating_slot=2,
            offering_slot=4,
            budgets=ENTERED_BUDGETS,
        ),
    ]
    poller = make_poller(config, entries, rows)

    first = poller.step()
    assert first["nomination"]["status"] == "none"
    assert first["nomination"]["verdict"] is None

    second = poller.step()
    nomination = second["nomination"]
    assert nomination["status"] == "live"
    assert nomination["is_live"] is True
    assert nomination["player_id"] == "B"
    assert nomination["name"] == "B"
    assert nomination["high_bid"] == 5
    assert nomination["nominating_slot"] == 2
    assert nomination["offering_slot"] == 4
    assert nomination["analysis"] is not None
    assert isinstance(nomination["analysis"]["max_bid"], int)
    assert nomination["analysis"]["worth"] == 20.0
    assert nomination["verdict"] is not None
    assert nomination["verdict"]["action"] in {"BID", "PASS"}


def test_bid_only_below_max_bid_equality_is_pass(config):
    """At high bid == my max bid I can only match, never beat: that is
    PASS with margin 0. One dollar below is BID with margin 1. Kills the
    off-by-one (<=) mutant exactly at the boundary."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]

    def nomination_at(offer):
        # MOVED for the budget rule: the boundary being probed is
        # high_bid vs max_bid, so the board carries entered budgets. The
        # $200 figure is unchanged — only its provenance is.
        entries = [make_tick(nominee="B", offer=offer, budgets=ENTERED_BUDGETS)]
        return make_poller(config, entries, rows).step()["nomination"]

    # The engine's max bid is a function of the board, not the offer, so
    # discover it once and replay the exact boundary offers against it.
    max_bid = nomination_at(1)["analysis"]["max_bid"]
    assert max_bid > 2  # fixture guard: the boundary probes are distinct

    at_equality = nomination_at(max_bid)
    assert at_equality["verdict"]["action"] == "PASS"
    assert at_equality["verdict"]["margin"] == 0

    one_below = nomination_at(max_bid - 1)
    assert one_below["verdict"]["action"] == "BID"
    assert one_below["verdict"]["margin"] == 1


def test_profit_is_price_minus_value_signed(config):
    """The decided sign convention: profit = current price MINUS value,
    centered at $0. A $13 bid on a $20 player reads -7 (under value); a
    $27 bid reads +7. The asymmetric fixture kills the flipped-sign
    (value-minus-price) mutant on both sides."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]

    def profit_at(offer):
        entries = [make_tick(nominee="B", offer=offer)]
        return make_poller(config, entries, rows).step()["nomination"]["profit"]

    assert profit_at(13) == -7.0
    assert profit_at(27) == 7.0
    assert profit_at(20) == 0.0


def test_untrusted_nomination_never_carries_a_verdict(config):
    """A stale picks feed cannot prove the lot is open, so the nomination
    renders UNTRUSTED and the verdict is suppressed — even at a $1 offer
    that would scream BID on a trusted board (the fail-open mutant)."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    entries = [make_tick(nominee="B", offer=1, stale=("picks",))]
    state = make_poller(config, entries, rows).step()

    nomination = state["nomination"]
    assert nomination["status"] == "untrusted"
    assert nomination["is_live"] is False
    assert nomination["verdict"] is None
    assert "untrusted" in nomination["verdict_reason"]
    assert state["stale_endpoints"] == ["picks"]
    # The board data itself still renders: values, budgets, the table.
    assert state["teams"]
    assert state["players"]


def test_stale_draft_endpoint_never_carries_a_verdict(config):
    """One failed GET on /draft (picks fine) serves the nomination pointer
    and high bid from a disk cache with no age limit: the lot on screen
    can be arbitrarily old. A confident BID computed from cached money is
    the fail-open bug; the verdict must be withheld with an honest reason
    while the board data (values, budgets, table) still renders."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    # MOVED for the budget rule: entered budgets, so the ONLY reason this
    # verdict can be withheld is the cached draft object. On a guessed
    # board the budget rule would suppress first and this test would pass
    # while proving nothing about the cache guard.
    entries = [
        make_tick(nominee="B", offer=1, stale=("draft",), budgets=ENTERED_BUDGETS)
    ]
    state = make_poller(config, entries, rows).step()

    nomination = state["nomination"]
    assert nomination["status"] == "live"  # the picks feed itself is trusted
    assert nomination["verdict"] is None
    assert "cache" in nomination["verdict_reason"]
    assert state["stale_endpoints"] == ["draft"]
    # The sheet math still shows (it does not depend on the draft object).
    assert nomination["analysis"] is not None
    assert state["teams"]
    assert state["players"]


def test_live_lot_with_no_recorded_high_bid_withholds_the_verdict(config):
    """An open lot always carries a high bid; a live nomination whose
    draft object has NO highest_offer is suspect data. Pricing it against
    a fabricated $0 offer would render BID at full margin — the verdict
    is withheld instead, and profit shows nothing rather than a number
    computed from an invented price."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    entries = [make_tick(nominee="B")]  # nominee present, no offer at all
    state = make_poller(config, entries, rows).step()

    nomination = state["nomination"]
    assert nomination["status"] == "live"
    assert nomination["high_bid"] is None
    assert nomination["verdict"] is None
    assert "high bid" in nomination["verdict_reason"]
    assert nomination["profit"] is None
    assert nomination["analysis"] is not None  # worth/max-bid still inform


def test_paused_draft_never_carries_a_verdict(config):
    """Sleeper's overnight auto-pause freezes the lot; advising into a
    paused draft is the same fail-open bug in a different coat."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    entries = [make_tick(nominee="B", offer=1, paused=True)]
    state = make_poller(config, entries, rows).step()

    assert state["paused"] is True
    assert state["nomination"]["verdict"] is None
    assert state["nomination"]["verdict_reason"] == "draft paused"


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


def _bridged_poller(config, rows, startup_slots, tick_slots):
    """A poller whose keeper bridge was built against ``startup_slots``,
    then fed one tick per entry in ``tick_slots``.

    This is the only fixture in the suite that drives two DIFFERENT slot
    maps through one poller, which is the whole shape of the defect: the
    bridge is built once and the board is re-read every tick.
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
            nominee=_NOMINEE, offer=5, budgets=_FUNDED_ROOM, slot_to_roster_id=slots
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
    """The seventh fail-closed rule, and the only two-map fixture here.

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

    ANTI-CHEAT, three boards, and each of the trivially-wrong
    implementations dies on one of them:

    * tick 1 is the SAME map the bridge was built on, and it must be
      completely quiet with a live verdict. An implementation that fires
      whenever a startup map was declared fails here.
    * tick 2 permutes it, and must raise the banner AND withhold the
      verdict. An implementation that compares the live map to itself, or
      that decides staleness once and caches it, sees no change on this
      tick and fails.
    * the third poller's map never moves across two ticks, so a rule that
      keys on "not the first tick" rather than on the map fails there.

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

    dealt = poller.step()
    assert dealt["me"]["slot"] == MY_DRAFT_SLOT  # roster 7 now drafts from slot 4
    # The defect, measured. The bridge still hangs my keepers on slot 7.
    assert dealt["me"]["roster"] == []
    assert dealt["me"]["open_slots"] == 15  # three keepers unaccounted for
    assert team_by_slot(dealt, 7)["open_slots"] == 12
    # So the tool stops, loudly, in the one way the operator can act on.
    (stale,) = _stale_banners(dealt)
    assert "RESTART" in stale["actual"]
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


def test_source_outage_keeps_serving_the_last_good_state(config):
    """A SleeperUnavailableError (live outage AND cache miss) must not
    kill the loop or blank the page: the last good budgets stay up,
    labeled with the error; the next good poll clears the label."""
    rows = [sheet_row(1, "A", "WR", 37.0), sheet_row(2, "B", "RB", 20.0)]
    good = make_tick([raw_auction_pick(1, "A", 3, "37")])
    entries = [
        good,
        SleeperUnavailableError("live fetch of 'picks' failed and no cache"),
        good,
    ]
    poller = make_poller(config, entries, rows)

    first = poller.step()
    assert first["source_error"] is None

    degraded = poller.step()
    assert degraded["poll_count"] == 2
    assert "no cache" in degraded["source_error"]
    # Everything the last good poll knew is still on screen.
    assert team_by_slot(degraded, 3)["remaining"] == 163
    assert [player["player_id"] for player in degraded["players"]] == ["B"]
    assert degraded["sales"][0]["player_id"] == "A"

    recovered = poller.step()
    assert recovered["source_error"] is None
    assert recovered["poll_count"] == 3


def test_any_source_exception_labels_the_snapshot_instead_of_raising(config):
    """Only SleeperUnavailableError used to be absorbed; any other
    exception killed the poll thread and left the page serving a
    confident, unlabeled, frozen state forever. A TypeError from the
    source (Sleeper serving JSON null is one live trigger) must keep the
    last good snapshot served WITH the error stamped on it — so the red
    FEED UNAVAILABLE banner fires — and the next good poll recovers."""
    rows = [sheet_row(1, "A", "WR", 37.0), sheet_row(2, "B", "RB", 20.0)]
    good = make_tick([raw_auction_pick(1, "A", 3, "37")])
    entries = [good, TypeError("'NoneType' object is not iterable"), good]
    poller = make_poller(config, entries, rows)

    first = poller.step()
    assert first["source_error"] is None

    degraded = poller.step()  # must NOT raise
    assert degraded["poll_count"] == 2
    assert "TypeError" in degraded["source_error"]
    assert "not iterable" in degraded["source_error"]
    # The last good board is still on screen, labeled.
    assert team_by_slot(degraded, 3)["remaining"] == 163
    assert degraded["sales"][0]["player_id"] == "A"

    recovered = poller.step()
    assert recovered["source_error"] is None
    assert recovered["poll_count"] == 3


def test_processing_error_before_first_good_poll_surfaces_on_the_page(config):
    """A my-slot with no team on the board raises deep in snapshot
    building (board.team). Before the catch-all this killed the thread on
    poll #1 and the page hung at 'waiting for first poll' with the
    traceback only on the console. Now the error is IN the served
    snapshot, and the loop stays alive for later polls."""
    rows = [sheet_row(1, "A", "WR", 37.0), sheet_row(2, "B", "RB", 20.0)]
    entries = [make_tick(nominee="B", offer=5)]
    poller = make_poller(config, entries, rows, my_slot=99)

    state = poller.step()  # must NOT raise
    assert state["poll_count"] == 1
    assert "no team in draft slot 99" in state["source_error"]
    assert state["ok"] is False  # never had a good poll; honest not-ready

    again = poller.step()  # the loop survives to keep reporting
    assert again["poll_count"] == 2
    assert "no team in draft slot 99" in again["source_error"]


def test_missing_nomination_metadata_still_serves_values_budgets_sales(config):
    """Degraded mode: with the draft metadata blanked (no nomination
    fields at all) the page still gets values, budgets, and sold players
    from picks polling; the nomination area honestly reports no data
    instead of hiding the whole page."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "RB", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    entries = [make_tick([raw_auction_pick(1, "A", 3, "37")])]
    state = make_poller(config, entries, rows).step()

    nomination = state["nomination"]
    assert nomination["status"] == "none"
    assert nomination["player_id"] is None
    assert nomination["verdict"] is None
    assert nomination["verdict_reason"] == "no nomination data"
    # The rest of the page is fully populated from the picks feed.
    assert len(state["teams"]) == 12
    assert [player["player_id"] for player in state["players"]] == ["B", "C"]
    assert state["sales"][0]["amount"] == 37
    assert state["me"] is not None


def test_keeper_board_without_keeper_ids_shows_the_error_not_a_number(config):
    """The engine's fail-loud keeper guard must surface on the page: a
    board that shows kept players, analyzed without keeper ids, renders
    the ValueError as an on-page analysis error — never a confident max
    bid, never a dead poll loop."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    # Mis-wired on purpose: the tracker knows slot 1 kept a player, but the
    # poller was handed an empty keeper mapping.
    tracker = DraftTracker(config, keepers_by_slot={1: ("KEPT",)}, clock=lambda: 0.0)
    entries = [make_tick(nominee="B", offer=1)]
    state = make_poller(config, entries, rows, tracker=tracker).step()

    nomination = state["nomination"]
    assert nomination["analysis"] is None
    assert "keeper" in nomination["analysis_error"]
    assert nomination["verdict"] is None
    # The rest of the page still serves.
    assert len(state["teams"]) == 12


def test_my_panel_binds_my_remaining_never_my_spent(config):
    """The my-panel money figure is the number Ethan reads for his own
    money. Asymmetric fixture: my slot 7 spends $37 of $200, so remaining
    (163), spent (37), and max bid (150) are three DIFFERENT numbers —
    binding any wrong field in _me_json fails here."""
    rows = [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "RB", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    entries = [make_tick([raw_auction_pick(1, "A", 7, "37")])]
    state = make_poller(config, entries, rows).step()

    me = state["me"]
    assert me["slot"] == 7
    assert me["remaining"] == 163
    assert me["max_bid"] == 150  # 163 - 13 held for the other open slots
    assert me["open_slots"] == 14
    assert "spent" not in me


def test_keeper_wired_poller_prices_and_renders_the_keeper_board(config):
    """The configuration the real 2026 draft night will use: a non-empty
    keeper mapping through the poller. The kept player is off the buyable
    table, pinned on my roster as a keeper at no auction price, the open
    slots reflect the keeper, and the engine prices a live lot without
    tripping its keeper guard."""
    rows = [
        sheet_row(1, "KP", "RB", 30.0, name="Kept Back"),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    # MOVED for the budget rule, in two coupled ways, because this
    # fixture asserts BOTH a verdict and the pre-entry keeper banner and
    # those now pull apart. My slot 7 gets a real budget_<slot> key, so
    # the verdict assert below still means what it meant. Slot 3 is a
    # second keeper team left uncovered, so the banner assert below
    # still means what IT meant: the room is not fully entered. Its
    # keeper is off the value sheet, so the buyable table is unchanged.
    keepers = {7: ("KP",), 3: ("OFFSHEET",)}
    budgets = {slot: 200 for slot in range(1, 13) if slot != 3}
    entries = [make_tick(nominee="B", offer=5, budgets=budgets)]
    state = make_poller(config, entries, rows, keepers_by_slot=keepers).step()

    assert [player["player_id"] for player in state["players"]] == ["B", "C"]
    me = state["me"]
    assert me["slot"] == 7
    assert me["open_slots"] == 14  # 15 drafted slots minus the keeper
    assert me["roster"] == [
        {
            "player_id": "KP",
            "name": "Kept Back",
            "position": "RB",
            "price": None,
            "keeper": True,
        }
    ]
    nomination = state["nomination"]
    assert nomination["analysis_error"] is None
    assert nomination["analysis"] is not None
    assert nomination["verdict"] is not None
    assert team_by_slot(state, 7)["open_slots"] == 14
    # And the pre-entry condition banners: a keeper slot with no
    # budget_<slot> key renders at the default until it is entered.
    assert any(
        warning["field"] == "keeper_budgets" for warning in state["settings_warnings"]
    )


def test_snapshot_json_contract_is_pinned_for_a_rich_fixture(config):
    """The static page binds these exact field names; any rename anywhere
    in the snapshot (players-row worth, settings_warnings field, roster
    price, budget_is_default, analysis inflation, and the rest of the
    class) must fail HERE, not in a browser at draft time. The fixture is
    rich on purpose: a keeper, a purchase, a live priced nomination with
    tier and verdict, and a settings warning."""
    rows = [
        sheet_row(1, "KP", "RB", 30.0, name="Kept Back"),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "A", "WR", 19.0),
        sheet_row(4, "C", "QB", 11.0),
    ]
    # MOVED for the budget rule (see the `me` key set below, which also
    # moved): the fixture must stay rich in BOTH a verdict and a settings
    # warning, and a defaulted budget now suppresses the verdict. So my
    # slot 7 carries a real budget_<slot> key while slot 3 is a second,
    # uncovered keeper team that keeps the warning firing.
    keepers = {7: ("KP",), 3: ("OFFSHEET",)}
    budgets = {slot: 200 for slot in range(1, 13) if slot != 3}
    entries = [
        make_tick(
            [raw_auction_pick(1, "A", 7, "19")], nominee="B", offer=5, budgets=budgets
        )
    ]
    state = make_poller(config, entries, rows, keepers_by_slot=keepers).step()

    assert set(state) == {
        "ok",
        "poll_count",
        "updated_at",
        "source_error",
        "status",
        "paused",
        "stale_endpoints",
        "settings_warnings",
        "timer_end_at",
        "nomination",
        "teams",
        # MOVED: `defaulted_keeper_slots` is a deliberate addition to the
        # /state contract. My max bid is computed from all twelve budgets,
        # so it is a guess while ANY keeper team's money is fabricated,
        # not only while mine is — and the page has no other way to know
        # which defaulted slots are provably wrong rather than merely
        # unentered.
        "defaulted_keeper_slots",
        # A deliberate addition too, and a SIBLING of the key above rather
        # than more entries in it. Those slots show money nobody entered,
        # which provenance already marks; these show money somebody DID
        # enter and that a keeper roster provably cannot have left, which
        # provenance calls real. Merging them would make one of the page's
        # two captions a lie.
        "impossible_keeper_slots",
        "me",
        "players",
        "sales",
        "off_model_player_ids",
        "note",
    }
    # Slot 3 is the uncovered keeper team; slot 7 has a real key.
    assert state["defaulted_keeper_slots"] == [3]
    # And non-empty here, which is the point of pinning it in the contract
    # fixture: slot 7's real budget_7 key of $200 is above the $195 its one
    # keeper leaves, so the ceiling banner names it while its provenance
    # stays real. The two keys hold DIFFERENT slots on this one board.
    assert state["impossible_keeper_slots"] == [7]
    nomination = state["nomination"]
    assert set(nomination) == {
        "status",
        "is_live",
        "player_id",
        "name",
        "position",
        "high_bid",
        "nominating_slot",
        "offering_slot",
        "analysis",
        "analysis_error",
        "pre_sale",
        "verdict",
        "verdict_reason",
        "profit",
        "last_of_tier",
    }
    assert set(nomination["analysis"]) == {
        "rank",
        "worth",
        "room_price",
        "keeper_premium",
        "value",
        "inflation",
        "inflation_adjusted",
        "marginal_worth",
        "need_bump",
        "spend_margin",
        "spend_boost",
        "spend_adjusted",
        "tier",
        "my_cap",
        "max_bid",
    }
    assert set(nomination["analysis"]["tier"]) == {
        "tier",
        "size",
        "remaining",
        "last_of_tier",
    }
    assert set(nomination["verdict"]) == {"action", "margin", "basis"}
    assert state["settings_warnings"], "the rich fixture must carry a warning"
    assert set(state["settings_warnings"][0]) == {"field", "expected", "actual"}
    assert set(state["teams"][0]) == {
        "slot",
        "roster_id",
        "remaining",
        "open_slots",
        "max_bid",
        "budget_is_default",
        "needs",
        "is_me",
    }
    me = state["me"]
    # MOVED: `budget_is_default` is a deliberate addition to the /state
    # contract. Every money figure in this panel is budget minus spend,
    # so when the budget is the league default rather than a real one the
    # page must be able to mark the numbers instead of rendering them at
    # full confidence.
    assert set(me) == {
        "slot",
        "remaining",
        "open_slots",
        "max_bid",
        "budget_is_default",
        "needs",
        "roster",
    }
    assert len(me["roster"]) == 2  # the keeper AND the purchase shape
    for entry in me["roster"]:
        assert set(entry) == {"player_id", "name", "position", "price", "keeper"}
    assert set(state["players"][0]) == {
        "rank",
        "player_id",
        "name",
        "position",
        "worth",
        "room_price",
        "value",
    }
    assert set(state["sales"][0]) == {
        "player_id",
        "name",
        "position",
        "amount",
        "slot",
    }


def test_last_of_tier_binds_to_the_tier_flag_not_a_lookalike(config):
    """The same nominated player flips the warning exactly when his final
    tier-mate sells: unsold tier-mate -> no warning; tier-mate sold ->
    warning. Kills any binding to a lookalike field (tier index, rank,
    remaining-anywhere counts)."""
    rows = [
        sheet_row(1, "D", "RB", 40.0),
        sheet_row(2, "E", "RB", 38.0),  # D's only tier-mate (gap < 4.8)
        sheet_row(3, "F", "RB", 20.0),  # next tier down
        sheet_row(4, "G", "WR", 30.0),
    ]

    both_available = make_poller(
        config, [make_tick(nominee="D", offer=5)], rows
    ).step()["nomination"]
    assert both_available["analysis"]["tier"] == {
        "tier": 1,
        "size": 2,
        "remaining": 2,
        "last_of_tier": False,
    }
    assert both_available["last_of_tier"] is False

    mate_sold = make_poller(
        config,
        [make_tick([raw_auction_pick(1, "E", 2, "38", "RB")], nominee="D", offer=5)],
        rows,
    ).step()["nomination"]
    assert mate_sold["analysis"]["tier"]["remaining"] == 1
    assert mate_sold["analysis"]["tier"]["last_of_tier"] is True
    assert mate_sold["last_of_tier"] is True


def test_sold_nominee_is_priced_on_the_pre_sale_board(config):
    """Between lots the pointer names the just-sold winner. His analysis
    must come from the board BEFORE his sale folded in (the engine's seam
    contract): my own $60 purchase must not debit the budget his numbers
    are computed from. The retrospective verdict is labeled final, and
    the lull's later polls reuse the same pre-sale record."""
    rows = [
        sheet_row(1, "X", "WR", 55.0),
        sheet_row(2, "B", "RB", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]
    # MOVED for the budget rule: the retrospective 'final' verdict is the
    # claim under test, so this board carries entered budgets. The $200
    # figure and every number below it are unchanged.
    sold_tick = make_tick(
        [raw_auction_pick(1, "X", 7, "60")],
        nominee="X",
        offer=60,
        offering_slot=7,
        budgets=ENTERED_BUDGETS,
    )
    entries = [make_tick(budgets=ENTERED_BUDGETS), sold_tick, sold_tick]
    poller = make_poller(config, entries, rows)
    poller.step()

    state = poller.step()
    nomination = state["nomination"]
    assert nomination["status"] == "sold_between_lots"
    assert nomination["pre_sale"] is True
    # Pre-sale, my team had $200 and 15 open slots: cap = 200 - 14 = 186.
    # Priced post-sale the cap would be (200-60) - 13 = 127.
    assert nomination["analysis"]["my_cap"] == 186
    assert nomination["verdict"] is not None
    assert nomination["verdict"]["basis"] == "final"
    # But the TEAM rows show the post-sale truth: the money is spent.
    assert team_by_slot(state, 7)["remaining"] == 140

    lull = poller.step()
    assert lull["nomination"]["pre_sale"] is True
    assert lull["nomination"]["analysis"] == nomination["analysis"]


def test_recovery_tick_with_extra_sales_refuses_retrospective_pricing(config):
    """When one observed tick carries the nominee's sale PLUS other sales
    the dashboard missed (dropped polls), the previous board predates all
    of them — pricing the nominee against it and labeling it 'pre-sale'
    would put a several-sales-old number under an honest-looking chip.
    The refusal path fires instead, and the lull's later ticks repeat it."""
    rows = [
        sheet_row(1, "X", "WR", 55.0),
        sheet_row(2, "A", "RB", 22.0),
        sheet_row(3, "B", "RB", 20.0),
    ]
    recovery_tick = make_tick(
        # Two sales revealed by the SAME poll: A (missed) then X (nominee).
        [raw_auction_pick(1, "A", 3, "22", "RB"), raw_auction_pick(2, "X", 7, "60")],
        nominee="X",
        offer=60,
        offering_slot=7,
    )
    entries = [make_tick(), recovery_tick, recovery_tick]
    poller = make_poller(config, entries, rows)
    poller.step()

    state = poller.step()
    nomination = state["nomination"]
    assert nomination["status"] == "sold_between_lots"
    assert nomination["analysis"] is None
    assert nomination["pre_sale"] is False
    assert "2 sales landed in one poll" in nomination["analysis_error"]
    assert nomination["verdict"] is None
    # The board itself is current: both sales debited, both players gone.
    assert team_by_slot(state, 3)["remaining"] == 178
    assert team_by_slot(state, 7)["remaining"] == 140
    assert [player["player_id"] for player in state["players"]] == ["B"]

    lull = poller.step()  # the cached refusal holds through the lull
    assert lull["nomination"]["analysis"] is None
    assert "2 sales landed in one poll" in lull["nomination"]["analysis_error"]


def test_nominee_sold_before_first_look_reports_why_instead_of_pricing(config):
    """Started mid-lull, the dashboard has no pre-sale board for the
    already-sold nominee: it must say so, not reprice the lot on
    post-sale money."""
    rows = [sheet_row(1, "X", "WR", 55.0), sheet_row(2, "B", "RB", 20.0)]
    entries = [make_tick([raw_auction_pick(1, "X", 7, "60")], nominee="X", offer=60)]
    state = make_poller(config, entries, rows).step()

    nomination = state["nomination"]
    assert nomination["analysis"] is None
    assert nomination["pre_sale"] is False
    assert "pre-sale" in nomination["analysis_error"]
    assert nomination["verdict"] is None
