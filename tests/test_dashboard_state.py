"""The dashboard poller: one poll cycle in, one JSON-ready snapshot out.

Every test drives the public seam (``DashboardPoller.step`` /
``.snapshot``) with scripted sources — no network, no wall clock, no
FastAPI. This module owns the snapshot PAYLOAD: the money the board and
my panel render, the buyable table and the sale ledger, the pinned
key-set contract the static page binds against, what still serves when
the draft metadata is blank, and the pricing seam that decides which
board a lot is priced on. The fixtures are anti-cheat by construction:
budgets where spent and remaining differ, and boards that a fail-open
renderer would misread.

Three sibling modules hold the rest of the poller's surface. This module
reached pylint's 1000-line ceiling, and splitting it beat adding a
disable:

* ``test_dashboard_verdict.py`` — the verdict chain: when the tool
  advises, at what number, and every condition that withholds the call.
* ``test_dashboard_keeper_bridge.py`` — the keeper bridge and the
  draft order moving out from under it.
* ``test_dashboard_budget.py`` — the budget-provenance family (the
  ``--budget`` override, its precedence over Sleeper's keys, the
  fail-closed verdict and the page's marks).
"""

from __future__ import annotations

from .conftest import raw_auction_pick
from .helpers_dashboard import ENTERED_BUDGETS, make_poller, make_tick, team_by_slot
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
        # The third page-mark key, and the only one of the three that is a
        # BOARD-wide fact rather than a set of slots. The page has no other
        # way to know the figures it is about to paint were computed on a
        # bridge the draft order has moved past: the banner that says so
        # arrives as free text in settings_warnings, which nothing on the
        # page parses.
        "keeper_map_stale",
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
