"""The verdict chain: when the dashboard advises, and when it refuses.

Every test drives the public seam (``DashboardPoller.step`` /
``.snapshot``) with scripted sources — no network, no wall clock, no
FastAPI. This module owns the CALL: how fast a new nomination reaches a
verdict, the bid/pass boundary, the profit sign, and every condition that
suppresses the verdict instead of advising into a board the tool cannot
vouch for — an untrusted picks feed, a cached draft object, a lot with
no recorded high bid, a paused draft, a source outage, an arbitrary
exception out of the poll, and a mis-wired keeper mapping. The fixtures
are anti-cheat by construction: verdicts at exact bid-equality, profit
with an asymmetric sign, and fail-open mutants that a trusting board
would wave through at $1.

Split out of ``test_dashboard_state.py`` (the snapshot payload and its
key-set contract) when that module reached pylint's 1000-line ceiling;
see also ``test_dashboard_keeper_bridge.py`` and
``test_dashboard_budget.py``.
"""

from __future__ import annotations

from draftbot.sleeper_client import SleeperUnavailableError
from draftbot.tracker import DraftTracker

from .conftest import raw_auction_pick
from .helpers_dashboard import ENTERED_BUDGETS, make_poller, make_tick, team_by_slot
from .helpers_engine import sheet_row


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
