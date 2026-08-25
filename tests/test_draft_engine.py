"""The dynamic repricing engine: tiers, inflation, need, spend-down.

Every test drives the engine through its public functions over hand
fixtures built with the ``helpers_engine`` builders; the anti-cheat
fixtures are inputs where a trivially-wrong implementation (pooled
inflation, missing taper, quota-count need, off-by-one tier flag)
returns a detectably wrong value.
"""

from __future__ import annotations

import math

import pytest

from draftbot.draft_engine import (
    BENCH_RETENTION,
    INFLATION_MIN,
    MARGIN_BASE,
    MARGIN_MIN,
    MARGIN_SLOPE,
    TierStatus,
    analyze_player,
    build_tiers,
    inflation_adjusted_price,
    marginal_lineup_worth,
    positional_inflation,
    spend_schedule,
    taper_weight,
    tier_status,
)
from draftbot.tracker import Sale

from .helpers_engine import make_board, sheet_row


def _inflation_rows():
    """Two positions with identical $87 discretionary pools above the
    taper, plus two tail rows whose weight is zero."""
    return [
        sheet_row(1, "rb1", "RB", 40.0),
        sheet_row(2, "rb2", "RB", 30.0),
        sheet_row(3, "rb3", "RB", 20.0),
        sheet_row(4, "wr1", "WR", 40.0),
        sheet_row(5, "wr2", "WR", 30.0),
        sheet_row(6, "wr3", "WR", 20.0),
        sheet_row(160, "rbtail", "RB", 4.0),
        sheet_row(161, "wrtail", "WR", 4.0),
    ]


def _par_board(sales=(), off_model=()):
    """Two teams, six drafted slots, $93 budgets: initial discretionary
    money 2 x (93 - 6) = $174 exactly matches the $174 sheet pool, so
    every position's inflation starts at exactly 1.0."""
    return make_board(
        {1: 93, 2: 93},
        sales,
        drafted_slots=6,
        off_model=off_model,
    )


class TestPositionalInflation:
    """Remaining money over remaining value, per position, tapered."""

    def test_par_room_starts_at_one(self):
        """Money exactly matching the pool prices every position at par."""
        inflation = positional_inflation(_inflation_rows(), _par_board())
        assert inflation["RB"] == pytest.approx(1.0)
        assert inflation["WR"] == pytest.approx(1.0)

    def test_bargain_inflates_that_position_and_only_that_position(self):
        """Anti-cheat (two mutants): rb1 (worth \\$40) sells for \\$20.
        The RB ratio must move — only $19 of discretionary money left the
        room while $39 of RB value left the board, so the survivors have
        more money chasing them — and the WR ratio must not move at all.
        A pooled implementation moves both; a static one moves neither.

        The isolation check lives on the INFLATION side because the
        deflation side no longer varies: ``INFLATION_MIN`` is 1.0, so
        every below-par ratio reads 1.0 and a below-par fixture can no
        longer tell a per-position implementation from a pooled one."""
        board = _par_board([Sale("rb1", 20, 2)])
        inflation = positional_inflation(_inflation_rows(), board)
        assert inflation["RB"] == pytest.approx((87 - 19) / 48)
        assert inflation["RB"] > 1.0
        assert inflation["WR"] == pytest.approx(1.0)

    def test_overpay_deflation_is_held_at_the_floor(self):
        """The floor's whole job, and the modelling cost it buys.

        rb1 (worth \\$40) sells for \\$60: the raw ratio is 28/48 = 0.583,
        and before the floor moved that is exactly what the engine
        emitted — a mid-draft nominee priced at 58% of his sheet value.
        ``INFLATION_MIN = 1.0`` holds it at par instead. The cost is that
        the deflation signal is gone, not merely damped: a \\$60 overpay
        and a \\$87 overpay (raw ratio 0.0) now read identically. Both
        directions asserted, so a floor quietly lowered again fails here
        rather than silently reviving the collapse."""
        rows = _inflation_rows()
        mild = positional_inflation(rows, _par_board([Sale("rb1", 60, 2)]))
        extreme = positional_inflation(rows, _par_board([Sale("rb1", 87, 2)]))
        assert INFLATION_MIN == 1.0
        assert mild["RB"] == INFLATION_MIN
        assert mild["RB"] > (87 - 59) / 48  # the raw ratio, not emitted
        assert extreme["RB"] == INFLATION_MIN
        assert mild["WR"] == pytest.approx(1.0)

    def test_off_model_sale_moves_no_ratio(self):
        """Anti-cheat: a \\$40 sale of a player the sheet does not price
        debits the buying team's budget (the board builder derives spend
        from sales) but must leave every ratio exactly where it was. An
        implementation whose money side reads remaining budgets — or one
        that forgets the off-model filter on the spent side — moves RB."""
        clean = positional_inflation(_inflation_rows(), _par_board())
        with_off_model = positional_inflation(
            _inflation_rows(),
            _par_board([Sale("mystery", 40, 1)], off_model=("mystery",)),
        )
        assert with_off_model == clean

    def test_board_flagged_off_model_sale_is_excluded_even_if_on_sheet(self):
        """The board's off-model flag is authoritative: a sale it flags
        stays out of the spent side even when the engine's own sheet
        happens to price the player."""
        flagged = positional_inflation(
            _inflation_rows(),
            _par_board([Sale("rb1", 60, 2)], off_model=("rb1",)),
        )
        # The flagged sale spends no modeled money, but rb1's value still
        # leaves the remaining pool: 87 / 48 at RB, untouched WRs.
        assert flagged["RB"] == pytest.approx(87 / 48)
        assert flagged["WR"] == pytest.approx(1.0)

    def test_tail_rows_stay_out_of_the_ratio(self):
        """The taper works both sides: dropping the zero-weight tail rows
        from the sheet changes no ratio, so tail value can never dilute
        the top of the board's inflation."""
        top_only = [row for row in _inflation_rows() if row.rank < 100]
        board = _par_board([Sale("rb1", 60, 2)])
        assert positional_inflation(top_only, board) == positional_inflation(
            _inflation_rows(), board
        )

    def test_inflation_rescales_to_the_actual_room_money(self):
        """Anti-cheat for the \\$2400 assumption: identical sheet, but the
        room's (keeper-reduced) budgets are roughly double the sheet's
        normalization. The ratio must track the actual dollars — a mutant
        that trusts the sheet's own normalization stays at 1.0.

        The fixture is a RICH room rather than the poor one it used to
        be: a poor room now clamps at ``INFLATION_MIN`` and the mutant
        would pass, since 1.0 is what the floor emits anyway."""
        rich_room = make_board({1: 174, 2: 174}, drafted_slots=6)
        inflation = positional_inflation(_inflation_rows(), rich_room)
        assert inflation["RB"] == pytest.approx(336 * 0.5 / 87)
        assert inflation["RB"] > 1.9

    def test_kept_players_never_enter_any_pool(self):
        """A kept rb1 was never buyable: the RB pool starts at \\$48 and
        the room's money share rebalances toward the untouched WR pool."""
        inflation = positional_inflation(
            _inflation_rows(),
            _par_board(),
            keepers_by_slot={2: ("rb1",)},
        )
        assert inflation["RB"] == pytest.approx((174 * 48 / 135) / 48)
        assert inflation["WR"] == pytest.approx((174 * 87 / 135) / 87)

    def test_keeper_premium_stays_out_of_the_ratio(self):
        """The ratio prices this season's dollars: adding keeper premium
        to a row (raising its NPV value) must not move any ratio."""
        with_premium = [
            (
                sheet_row(row.rank, row.player_id, row.position, row.worth, 6.0)
                if row.player_id == "rb2"
                else row
            )
            for row in _inflation_rows()
        ]
        board = _par_board([Sale("rb1", 60, 2)])
        assert positional_inflation(with_premium, board) == positional_inflation(
            _inflation_rows(), board
        )


@pytest.fixture(name="slots")
def slots_fixture(config):
    """The real league's drafted roster shape, straight from the config
    (1QB/2RB/2WR/1TE/FLEX/K/DEF plus bench; IR is extra, not drafted)."""
    return config.roster_slots


class TestMarginalNeed:
    """Roster need is a lineup-value difference, never a quota count."""

    @staticmethod
    def _rows():
        return [
            sheet_row(1, "wr40", "WR", 40.0),
            sheet_row(2, "wr35", "WR", 35.0),
            sheet_row(3, "rb30", "RB", 30.0),
            sheet_row(4, "wr30", "WR", 30.0),
            sheet_row(5, "rb25", "RB", 25.0),
            sheet_row(6, "rb20", "RB", 20.0),
            sheet_row(7, "qb15", "QB", 15.0),
            sheet_row(8, "qb12", "QB", 12.0),
            sheet_row(9, "qb9", "QB", 9.0),
            sheet_row(10, "qb8", "QB", 8.0),
            sheet_row(11, "rb15", "RB", 15.0),
            sheet_row(12, "def2", "DEF", 2.0),
        ]

    def test_scarce_starting_slot_is_fully_marginal(self, slots):
        """With no QB owned, a QB's whole worth is lineup-marginal."""
        assert marginal_lineup_worth("qb15", [], self._rows(), slots) == 15.0

    def test_third_qb_marginal_value_is_zero(self, slots):
        """Acceptance: two better QBs already fill the only QB slot, and a
        QB cannot ride the FLEX, so a third adds exactly nothing."""
        owned = ["qb12", "qb9"]
        assert marginal_lineup_worth("qb8", owned, self._rows(), slots) == 0.0

    def test_upgrade_is_partially_marginal(self, slots):
        """Anti-cheat for the quota-count mutant: my QB slot is filled,
        but the candidate beats the incumbent by \\$3 — the marginal value
        is exactly that \\$3, not zero (quota says the slot is full) and
        not \\$15 (a slot-open check says the position is scarce)."""
        assert marginal_lineup_worth("qb15", ["qb12"], self._rows(), slots) == 3.0

    def test_flex_keeps_a_third_rb_fully_marginal(self, slots):
        """Two RBs fill the dedicated slots; the third slides into the
        open FLEX at his full worth."""
        owned = ["rb30", "rb20"]
        assert marginal_lineup_worth("rb25", owned, self._rows(), slots) == 25.0

    def test_flex_competition_prices_the_displacement(self, slots):
        """Anti-cheat for FLEX handling: three good WRs already own the
        WR slots plus the FLEX. Adding rb25 upgrades the flex from wr30
        to... nothing: rb25 takes a dedicated RB slot over rb20, and the
        best leftover for FLEX is still wr30 vs the displaced rb20 —
        lineup gains exactly \\$5 (30+25+40+35+30 = 160 over 155)."""
        owned = ["rb30", "rb20", "wr40", "wr35", "wr30"]
        assert marginal_lineup_worth("rb25", owned, self._rows(), slots) == 5.0

    def test_kicker_and_def_slots_count_too(self, slots):
        """K/DEF slots are starting slots like any other."""
        assert marginal_lineup_worth("def2", [], self._rows(), slots) == 2.0

    def test_off_sheet_players_carry_no_lineup_worth(self, slots):
        """Both as candidate and as owned filler: a player the sheet does
        not price contributes zero, never a crash."""
        assert marginal_lineup_worth("ghost", [], self._rows(), slots) == 0.0
        assert marginal_lineup_worth("qb15", ["ghost"], self._rows(), slots) == 15.0

    def test_lineup_ranking_and_totals_use_worth_not_value(self, slots):
        """Anti-cheat for the worth/value swap: qbB's keeper premium makes
        his VALUE the position's best (\\$15) while his WORTH stays worst
        (\\$9). The lineup solver prices this season only — qbB alone is
        marginal for his \\$9 worth, and qb15 over the worth-best incumbent
        qbA is a \\$3 upgrade. A value-ranked solver reports \\$15 for the
        first (next season's premium leaking into this season's lineup)
        and \\$0 for the second (it already seated qbB as the starter)."""
        rows = [
            sheet_row(1, "qb15", "QB", 15.0),
            sheet_row(2, "qbA", "QB", 12.0),
            sheet_row(3, "qbB", "QB", 9.0, premium=6.0),
        ]
        assert marginal_lineup_worth("qbB", [], rows, slots) == 9.0
        assert marginal_lineup_worth("qb15", ["qbA", "qbB"], rows, slots) == 3.0


class TestInflationAdjustedPrice:
    """The multiplier's application, floor-safe and tapered."""

    def test_top_of_board_inflates_above_the_floor(self):
        """A full-weight rank scales its above-floor worth by the ratio."""
        row = sheet_row(2, "rb2", "RB", 30.0)
        assert inflation_adjusted_price(row, 1.5) == pytest.approx(1 + 29 * 1.5)

    def test_tail_price_does_not_inflate_with_the_top(self):
        """Acceptance: a \\$4 player at rank 160 keeps his \\$4 price no
        matter how hot his position's top of the board runs."""
        tail = sheet_row(160, "rbtail", "RB", 4.0)
        assert inflation_adjusted_price(tail, 1.5) == 4.0
        assert inflation_adjusted_price(tail, 0.5) == 4.0

    def test_keeper_premium_rides_through_uninflated(self):
        """The premium is next season's money: it neither inflates nor
        deflates with this room's remaining dollars."""
        row = sheet_row(2, "rb2", "RB", 30.0, premium=6.0)
        assert inflation_adjusted_price(row, 1.5) == pytest.approx(1 + 29 * 1.5 + 6)
        assert inflation_adjusted_price(row, 0.5) == pytest.approx(1 + 29 * 0.5 + 6)


class TestSpendSchedule:
    """Bargain early, spend down late, burn what the pool cannot absorb."""

    def test_par_money_bargains_early(self):
        """At money parity the margin sits below 1 (the deliberate early
        bargain stance) and a deep pool leaves no surplus to burn."""
        margin, boost = spend_schedule(
            _par_board(), 1, _inflation_rows(), {"RB": 1.0, "WR": 1.0}
        )
        assert margin == pytest.approx(MARGIN_BASE)
        assert margin < 1.0
        assert boost == 0.0

    def test_relative_riches_raise_the_margin_past_value(self):
        """Anti-cheat for the timid mutant: my \\$150 against the room's
        \\$30 must push the margin well above 1, uncapped by anything but
        my own max bid downstream."""
        board = make_board({1: 150, 2: 30}, drafted_slots=6)
        margin, _ = spend_schedule(board, 1, _inflation_rows(), {"RB": 1.0, "WR": 1.0})
        assert margin == pytest.approx(MARGIN_BASE + MARGIN_SLOPE * (25 / 5 - 1))
        assert margin > 2.5

    def test_pace_deficit_becomes_a_per_slot_boost(self):
        """Two open slots, \\$50 left, and my half of the remaining
        \\$13 pool is \\$6.50: the \\$43.50 my slots cannot absorb at value
        gets spread over both open slots and added to every bid."""
        rows = [
            sheet_row(1, "a", "RB", 6.0),
            sheet_row(2, "b", "RB", 5.0),
            sheet_row(3, "c", "RB", 2.0),
        ]
        board = make_board({1: 50, 2: 10}, drafted_slots=2)
        _, boost = spend_schedule(board, 1, rows, {"RB": 1.0})
        assert boost == pytest.approx((50 - 13 * 2 / 4) / 2)

    def test_sold_players_leave_the_pace_pool(self):
        """The pace check prices only what is still buyable: the sold $6
        lot is out of the pool, and the buyer's filled slot is out of the
        room's open-slot count."""
        rows = [
            sheet_row(1, "a", "RB", 6.0),
            sheet_row(2, "b", "RB", 5.0),
            sheet_row(3, "c", "RB", 2.0),
        ]
        board = make_board({1: 50, 2: 10}, [Sale("a", 6, 2)], drafted_slots=2)
        _, boost = spend_schedule(board, 1, rows, {"RB": 1.0})
        # Remaining pool $7 over 3 open slots; my share is two of them.
        assert boost == pytest.approx((50 - 7 * 2 / 3) / 2)

    def test_full_roster_has_no_schedule(self):
        """Nothing left to buy means nothing to schedule."""
        board = make_board({1: 5, 2: 30}, drafted_slots=0)
        assert spend_schedule(board, 1, _inflation_rows(), {}) == (0.0, 0.0)

    def test_broke_stack_margin_floors_at_margin_min(self):
        """Anti-cheat for a dropped ``MARGIN_MIN``: my \\$10 per open slot
        against the room's \\$50 puts the raw schedule at 0.05 — a
        5%-of-value bid on players a broke stack still needs. The floor
        must hold the margin at 0.85 instead."""
        board = make_board({1: 20, 2: 100}, drafted_slots=2)
        margin, _ = spend_schedule(board, 1, _inflation_rows(), {"RB": 1.0, "WR": 1.0})
        assert margin == MARGIN_MIN
        assert margin == pytest.approx(0.85)


def _league_rows():
    """A small but league-shaped sheet: every drafted position present,
    plus zero-weight tail rows past the taper."""
    return [
        sheet_row(1, "rb40", "RB", 40.0),
        sheet_row(2, "wr40", "WR", 40.0),
        sheet_row(3, "wr35", "WR", 35.0),
        sheet_row(4, "rb30", "RB", 30.0),
        sheet_row(5, "wr30", "WR", 30.0),
        sheet_row(6, "rb25", "RB", 25.0),
        sheet_row(7, "rb20", "RB", 20.0),
        sheet_row(8, "wr20", "WR", 20.0),
        sheet_row(9, "te18", "TE", 18.0),
        sheet_row(10, "qb15", "QB", 15.0),
        sheet_row(11, "rb15", "RB", 15.0),
        sheet_row(12, "qb12", "QB", 12.0),
        sheet_row(13, "qb9", "QB", 9.0),
        sheet_row(14, "qb8", "QB", 8.0),
        sheet_row(15, "te6", "TE", 6.0),
        sheet_row(16, "k2", "K", 2.0),
        sheet_row(17, "def2", "DEF", 2.0),
        sheet_row(160, "rbtail", "RB", 4.0),
        sheet_row(161, "wrtail", "WR", 4.0),
    ]


def _premium_rows():
    """A sheet where the nominee's keeper premium is the interesting
    number: qbP is worth $10 THIS season carrying a $6 premium (value 16),
    behind qb12 and ahead of qb9. The whole pool's discretionary worth is
    $96, which the premium tests' two $54 budgets match exactly, so every
    inflation ratio sits at 1.0 and the layered prices stay hand-checkable."""
    return [
        sheet_row(1, "wr40", "WR", 40.0),
        sheet_row(2, "wr30", "WR", 30.0),
        sheet_row(3, "qb12", "QB", 12.0),
        sheet_row(4, "qbP", "QB", 10.0, premium=6.0),
        sheet_row(5, "qb9", "QB", 9.0),
    ]


def _league_board(sales=(), off_model=()):
    """Three teams, eight drafted slots, $111 budgets: the room's money
    matches the sheet's $335 pool (no pace deficit on an empty board), so
    the spend layers start from their neutral stance."""
    return make_board(
        {1: 111, 3: 111, 7: 111},
        sales,
        drafted_slots=8,
        off_model=off_model,
    )


class TestAnalyzePlayer:
    """The pure entry point: one renderable record per nominated player."""

    def test_record_composes_the_layers_consistently(self, config):
        """Every number a dashboard shows is on the record, and the
        layered prices reconcile with the public component functions."""
        rows = _league_rows()
        board = _league_board([Sale("wr40", 43, 3)])
        analysis = analyze_player("rb40", rows, board, config, my_slot=7)

        assert analysis.player_id == "rb40"
        assert analysis.position == "RB"
        assert analysis.rank == 1
        assert analysis.worth == 40.0
        assert analysis.value == 40.0
        inflation = positional_inflation(rows, board)
        assert analysis.inflation == inflation["RB"]
        assert analysis.inflation_adjusted == inflation_adjusted_price(
            rows[0], inflation["RB"]
        )
        # Empty roster: rb40 is fully marginal, so need changes nothing.
        assert analysis.marginal_worth == 40.0
        assert analysis.need_bump == 0.0
        assert analysis.need_adjusted == analysis.inflation_adjusted
        margin, boost = spend_schedule(board, 7, rows, inflation)
        assert analysis.spend_margin == margin
        assert analysis.spend_boost == boost
        assert analysis.spend_adjusted == pytest.approx(
            1.0 + (analysis.need_adjusted - 1.0) * margin + boost
        )
        assert analysis.tier == tier_status(
            "rb40", build_tiers(rows), frozenset({"wr40"})
        )
        assert analysis.my_cap == board.team(7).max_bid
        assert analysis.max_bid == int(analysis.spend_adjusted)

    def test_third_qb_prices_at_bench_not_starter(self, config):
        """Acceptance: with two better QBs already mine, the third QB's
        marginal value is zero and his price collapses to bench retention
        — a fraction of what the same player costs while my QB slot is
        open, whatever the room's inflation level happens to be."""
        rows = _league_rows()
        board = _league_board([Sale("qb12", 12, 7), Sale("qb9", 9, 7)])
        third = analyze_player("qb8", rows, board, config, my_slot=7)
        starter = analyze_player("qb8", rows, _league_board(), config, my_slot=7)

        assert third.marginal_worth == 0.0
        assert third.need_bump < 0.0
        assert third.need_adjusted < starter.need_adjusted / 2
        assert third.max_bid < starter.max_bid

    def test_scarce_starting_slot_beats_the_redundant_case(self, config):
        """The need bump in one comparison: the same QB is priced with my
        QB slot open (fully marginal, no discount) and with it filled
        twice over (bench retention). Open must cost more."""
        rows = _league_rows()
        scarce = analyze_player("qb8", rows, _league_board(), config, my_slot=7)
        stacked_board = _league_board([Sale("qb12", 12, 7), Sale("qb9", 9, 7)])
        redundant = analyze_player("qb8", rows, stacked_board, config, my_slot=7)

        assert scarce.marginal_worth == 8.0
        assert scarce.need_bump == 0.0
        assert redundant.need_bump < 0.0
        assert scarce.need_adjusted > redundant.need_adjusted

    def test_max_bid_never_exceeds_my_team_cap(self, config):
        """A rich stack against a broke room drives the schedule way up;
        the emitted bid still respects max-bid ($1 reserved per other
        open slot), which is the tracker's number, not re-derived here."""
        budgets = {slot: 20 for slot in range(1, 13)} | {7: 200}
        board = make_board(budgets, drafted_slots=3)
        analysis = analyze_player("rb40", _league_rows(), board, config, my_slot=7)

        assert board.team(7).max_bid == 198
        assert analysis.spend_adjusted > 198
        assert analysis.max_bid == 198

    def test_full_roster_bids_zero(self, config):
        """No open slots: the record still renders, the bid is $0."""
        board = make_board(
            {slot: 200 for slot in range(1, 13)},
            [Sale(f"p{n}", 1, 7) for n in range(3)],
            drafted_slots=3,
        )
        analysis = analyze_player("rb40", _league_rows(), board, config, my_slot=7)
        assert analysis.max_bid == 0
        assert analysis.spend_adjusted == 0.0

    def test_off_sheet_nominee_gets_a_floor_record(self, config):
        """A nominated player the sheet does not price still renders: $1
        floor economics, no tier, no crash."""
        analysis = analyze_player(
            "ghost", _league_rows(), _league_board(), config, my_slot=7
        )
        assert analysis.rank is None
        assert analysis.worth == 1.0
        assert analysis.tier is None
        assert analysis.max_bid == 1

    def test_off_sheet_nominee_never_carries_the_pace_boost(self, config):
        """Degenerate-state hardening: a failed or empty sheet load must
        not render confident bids on unknown players. With no rows at
        all, the pace boost sees pool value 0 and would otherwise treat
        the entire \\$200 budget as burnable surplus (\\$13.33 a slot, max
        bid \\$14 on every nominee); with a one-row sheet a ghost nominee
        would carry nearly as much. The boost only ever burns surplus
        into players the model actually prices: an off-sheet nominee
        stays at the \\$1 floor."""
        board = make_board({slot: 200 for slot in range(1, 13)}, drafted_slots=15)

        empty_sheet = analyze_player("ghost", [], board, config, my_slot=7)
        assert empty_sheet.spend_boost == 0.0
        assert empty_sheet.max_bid == 1

        one_row = [sheet_row(1, "rb40", "RB", 40.0)]
        ghost = analyze_player("ghost", one_row, board, config, my_slot=7)
        assert ghost.spend_boost == 0.0
        assert ghost.max_bid == 1

    def test_my_slot_resolves_from_the_config_roster_id(self, config):
        """Without an explicit slot the engine finds my team by the
        config's roster id (the builder maps roster_id == slot, and the
        checked-in config says roster 7)."""
        explicit = analyze_player(
            "rb40", _league_rows(), _league_board(), config, my_slot=7
        )
        resolved = analyze_player("rb40", _league_rows(), _league_board(), config)
        assert resolved == explicit

    def test_unresolvable_slot_raises_instead_of_guessing(self, config):
        """A board without my roster id is an error, never team number 1."""
        board = make_board({1: 200}, drafted_slots=15)
        with pytest.raises(ValueError, match="roster"):
            analyze_player("rb40", _league_rows(), board, config)

    def test_max_bid_floors_the_fraction_never_rounds(self, config):
        """Anti-cheat for the rounding mutant: this fixture's
        ``spend_adjusted`` lands at 27.97 — fractional part above one
        half — so flooring gives 27 while rounding would ship a
        $1-too-high live bid of 28. The guard assert keeps the fixture
        honest: if it ever drifts below .5, floor and round agree and
        the test stops distinguishing them."""
        analysis = analyze_player(
            "rb2", _inflation_rows(), _par_board(), config, my_slot=1
        )
        assert analysis.spend_adjusted == pytest.approx(27.97)
        assert analysis.spend_adjusted - math.floor(analysis.spend_adjusted) >= 0.5
        assert analysis.max_bid == 27
        assert analysis.max_bid == math.floor(analysis.spend_adjusted)

    def test_scarce_premium_player_keeps_the_whole_premium_once(self, config):
        """Anti-cheat for the premium double-count: with my QB slot open,
        qbP is fully marginal (multiplier 1) and the need layer must hand
        back exactly the inflation-adjusted price — floor + (16 - 1 - 6)
        * 1 + premium = \\$16 — never the premium added on top again
        (\\$22). The board's \\$96 of money matches the \\$96 pool, so every
        ratio sits at exactly 1.0 and the numbers stay hand-checkable."""
        board = make_board({1: 54, 2: 54}, drafted_slots=6)
        analysis = analyze_player("qbP", _premium_rows(), board, config, my_slot=1)
        assert analysis.inflation == pytest.approx(1.0)
        assert analysis.inflation_adjusted == pytest.approx(16.0)
        assert analysis.marginal_worth == 10.0
        assert analysis.need_adjusted == pytest.approx(16.0)
        assert analysis.need_bump == 0.0

    def test_redundant_premium_player_discounts_worth_but_not_premium(self, config):
        """The bench-retention discount applies to this season's inflated
        worth only: with the better qb12 already mine, qbP's marginal
        worth is 0 and his price is floor + (16 - 1 - 6) * 0.25 + 6 =
        \\$9.25. The double-count mutant says \\$10.75; the worth/value
        swap calls qbP a \\$4 upgrade over qb12 and says \\$11.95."""
        board = make_board({1: 54, 2: 54}, [Sale("qb12", 12, 1)], drafted_slots=6)
        analysis = analyze_player("qbP", _premium_rows(), board, config, my_slot=1)
        assert analysis.inflation == pytest.approx(1.0)
        assert analysis.marginal_worth == 0.0
        assert analysis.need_adjusted == pytest.approx(
            1.0 + (16.0 - 1.0 - 6.0) * BENCH_RETENTION + 6.0
        )
        assert analysis.need_adjusted == pytest.approx(9.25)
        assert analysis.need_bump == pytest.approx(-6.75)

    def test_golden_quantized_values_pin_the_ten_decimal_grid(self, config):
        """Determinism beyond a self-comparison: these literals are the
        ten-decimal quantized values for a board whose RB ratio repeats
        in decimal (68/48). Removing or loosening ``_quantize`` changes
        the reprs — a drift the double-run tests can never see, because
        both runs drift together.

        The fixture is the BARGAIN board: the overpay board it used to
        use now clamps at ``INFLATION_MIN``, and an exactly-1.0 ratio
        pins nothing about the decimal grid."""
        board = _par_board([Sale("rb1", 20, 2)])
        analysis = analyze_player("rb2", _inflation_rows(), board, config, my_slot=1)
        assert repr(analysis.inflation) == "1.4166666667"
        assert repr(analysis.inflation_adjusted) == "42.0833333343"
        assert repr(analysis.need_adjusted) == "42.0833333343"
        assert repr(analysis.spend_margin) == "0.9978082192"
        assert repr(analysis.spend_boost) == "0.2272727271"
        assert repr(analysis.spend_adjusted) == "42.2205604002"
        assert analysis.max_bid == 42

    def test_double_run_is_byte_identical(self, config):
        """Determinism: the same inputs analyzed twice produce equal
        records with identical reprs, for every player on the sheet."""
        rows = _league_rows()
        board = _league_board([Sale("wr40", 43, 3), Sale("rb30", 22, 7)])

        def sweep():
            return [
                analyze_player(row.player_id, rows, board, config, my_slot=7)
                for row in rows
            ]

        first, second = sweep(), sweep()
        assert first == second
        assert [repr(item) for item in first] == [repr(item) for item in second]


class TestAnalyzePlayerKeepers:
    """Keeper plumbing through the entry point: ``keepers_by_slot`` must
    reach every layer — roster need, tiers, inflation, and the pace
    boost. This is a keeper league, so keepers populated is the actual
    draft-night configuration; each test here fails under a mutant that
    drops the keepers from exactly one of those hand-offs."""

    def test_kept_starters_make_the_same_position_redundant(self, config):
        """Anti-cheat for keepers dropped from the OWNED list: my two
        kept QBs must reach the need layer, so a nominated third QB is
        fully redundant (marginal 0, bench-retention price). A mutant
        that forgets them prices him as a scarce starter instead."""
        board = make_board(
            {1: 111, 3: 111, 7: 111}, drafted_slots=8, keeper_counts={7: 2}
        )
        kept = analyze_player(
            "qb8",
            _league_rows(),
            board,
            config,
            my_slot=7,
            keepers_by_slot={7: ("qb12", "qb9")},
        )
        assert kept.marginal_worth == 0.0
        assert kept.need_bump < 0.0
        assert kept.need_adjusted == pytest.approx(
            1.0 + (kept.inflation_adjusted - 1.0) * BENCH_RETENTION
        )

    def test_kept_tier_mate_shrinks_the_tier_and_fires_the_flag(self, config):
        """Anti-cheat for tiers built over the full sheet: with rb40
        kept, the two-man top RB tier collapses to rb38 alone, so
        nominating rb38 on a fresh board fires ``last_of_tier``. A
        mutant that tiers over all rows still sees two members — the
        unsold-but-unbuyable rb40 suppresses the alarm on the survivor."""
        rows = [
            sheet_row(1, "rb40", "RB", 40.0),
            sheet_row(2, "rb38", "RB", 38.0),
            sheet_row(3, "wr40", "WR", 40.0),
            sheet_row(4, "rb25", "RB", 25.0),
            sheet_row(5, "wr30", "WR", 30.0),
        ]
        board = make_board({1: 60, 3: 60, 7: 60}, drafted_slots=8, keeper_counts={3: 1})
        analysis = analyze_player(
            "rb38", rows, board, config, my_slot=7, keepers_by_slot={3: ("rb40",)}
        )
        assert analysis.tier == TierStatus(
            tier=1, size=1, remaining=1, last_of_tier=True
        )

    def test_keeper_board_without_keeper_ids_refuses_to_price(self, config):
        """Fail loud, never silently wrong: the board itself proves
        keepers exist (keeper_count > 0), so analyzing without
        ``keepers_by_slot`` would misprice every layer all night — the
        kept players wrongly stay in the pool denominators and vanish
        from the roster. Mirrors the ``_resolve_my_slot`` guard: an
        error, never a plausible number."""
        board = make_board(
            {1: 111, 3: 111, 7: 111}, drafted_slots=8, keeper_counts={3: 1}
        )
        with pytest.raises(ValueError, match="keeper"):
            analyze_player("rb40", _league_rows(), board, config, my_slot=7)
        with pytest.raises(ValueError, match="keeper"):
            analyze_player(
                "rb40", _league_rows(), board, config, my_slot=7, keepers_by_slot={}
            )

    def test_inflation_and_pace_boost_price_the_keeper_excluded_pool(self, config):
        """Anti-cheat for keepers dropped from the ratio or the pace
        pool: the kept \\$6 RB is not buyable, so \\$14 of room money over
        the \\$5 keeper-excluded pool inflates RB to 2.8 (1.4 if the kept
        row stays in the denominator), and the boostable pool prices at
        \\$16, leaving my \\$12 a 2/3-dollar per-slot surplus (a mutant
        that leaves the kept row in the pace pool sees \\$31 of pool and
        no surplus at all)."""
        rows = [
            sheet_row(1, "a", "RB", 6.0),
            sheet_row(2, "b", "RB", 5.0),
            sheet_row(3, "c", "RB", 2.0),
        ]
        board = make_board({1: 12, 2: 5}, drafted_slots=2, keeper_counts={2: 1})
        analysis = analyze_player(
            "b", rows, board, config, my_slot=1, keepers_by_slot={2: ("a",)}
        )
        assert analysis.inflation == pytest.approx(2.8)
        assert analysis.spend_boost == pytest.approx(2 / 3)


def test_taper_weight_full_fade_and_endpoints():
    """The taper's shape is part of the contract the tests above rely on:
    full weight through rank 110, linear fade, zero from rank 150 on, and
    zero for the unranked."""
    assert taper_weight(1) == 1.0
    assert taper_weight(110) == 1.0
    assert taper_weight(130) == pytest.approx(0.5)
    assert taper_weight(150) == 0.0
    assert taper_weight(400) == 0.0
    assert taper_weight(None) == 0.0


class TestTiers:
    """Gap-based tiers per position with remaining counts and the flag."""

    @staticmethod
    def _rows():
        """RBs in three visible tiers ([40, 38], [25, 24], [10]) plus WRs
        whose own gaps must never leak into the RB partition."""
        return [
            sheet_row(1, "rb1", "RB", 40.0),
            sheet_row(2, "rb2", "RB", 38.0),
            sheet_row(3, "wr1", "WR", 33.0),
            sheet_row(4, "rb3", "RB", 25.0),
            sheet_row(5, "rb4", "RB", 24.0),
            sheet_row(6, "wr2", "WR", 12.0),
            sheet_row(7, "rb5", "RB", 10.0),
        ]

    def test_gap_breaks_partition_each_position_on_its_own_values(self):
        """A $13 cliff and a $14 cliff split the RBs into three tiers; the
        $2 and $1 in-tier wobbles do not. WRs partition separately (their
        $21 gap makes two singleton tiers) instead of interleaving."""
        tiers = build_tiers(self._rows())
        assert tiers["RB"] == (("rb1", "rb2"), ("rb3", "rb4"), ("rb5",))
        assert tiers["WR"] == (("wr1",), ("wr2",))

    def test_last_of_tier_fires_exactly_on_the_final_remaining_member(self):
        """Anti-cheat for the known off-by-one: in the two-man top RB tier,
        nominating rb2 while rb1 is UNSOLD must not fire (2 remain), and
        nominating rb2 after rb1 sold must fire (exactly 1 remains). A
        flag keyed on ``remaining <= 2``, or one that forgets to count the
        nominee itself, gets one of the two wrong."""
        tiers = build_tiers(self._rows())

        both_left = tier_status("rb2", tiers, sold=frozenset())
        assert both_left.tier == 1
        assert both_left.size == 2
        assert both_left.remaining == 2
        assert both_left.last_of_tier is False

        alone = tier_status("rb2", tiers, sold=frozenset({"rb1"}))
        assert alone.remaining == 1
        assert alone.last_of_tier is True

        # The third direction — one LATE: rb2 himself just sold while
        # rb1 still sits on the board, so exactly one member remains but
        # the flag must stay off for the already-sold nominee. A guard
        # reduced to ``remaining == 1`` fires here and corrupts every
        # just-sold record the backtest and dashboard consume.
        himself_sold = tier_status("rb2", tiers, sold=frozenset({"rb2"}))
        assert himself_sold.remaining == 1
        assert himself_sold.last_of_tier is False

    def test_cheap_board_gaps_need_the_absolute_floor_too(self):
        """Anti-cheat for the relative-only mutant: late-draft values
        5.0 / 4.3 / 3.7 gap by more than 12% of the richer value but
        well under the \\$2 absolute floor — one tier, never three fake
        singletons each flashing a last-of-tier "jump now" signal."""
        rows = [
            sheet_row(1, "te5", "TE", 5.0),
            sheet_row(2, "te43", "TE", 4.3),
            sheet_row(3, "te37", "TE", 3.7),
        ]
        assert build_tiers(rows)["TE"] == (("te5", "te43", "te37"),)

    def test_tier_counts_ignore_other_tiers_and_other_positions(self):
        """Selling out the whole top RB tier and a WR must not flag a
        second-tier RB, whose own tier still holds two players."""
        tiers = build_tiers(self._rows())
        status = tier_status("rb3", tiers, sold=frozenset({"rb1", "rb2", "wr1"}))
        assert status.tier == 2
        assert status.size == 2
        assert status.remaining == 2
        assert status.last_of_tier is False

    def test_off_sheet_player_has_no_tier(self):
        """A player the sheet does not price cannot claim a tier."""
        assert tier_status("ghost", build_tiers(self._rows()), frozenset()) is None
