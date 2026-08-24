"""The dynamic repricing engine: tiers, inflation, need, spend-down.

Every test drives the engine through its public functions over hand
fixtures built with the ``helpers_engine`` builders; the anti-cheat
fixtures are inputs where a trivially-wrong implementation (pooled
inflation, missing taper, quota-count need, off-by-one tier flag)
returns a detectably wrong value.
"""

from __future__ import annotations

import pytest

from draftbot.draft_engine import (
    build_tiers,
    positional_inflation,
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
        inflation = positional_inflation(_inflation_rows(), _par_board())
        assert inflation["RB"] == pytest.approx(1.0)
        assert inflation["WR"] == pytest.approx(1.0)

    def test_overpay_deflates_that_position_and_only_that_position(self):
        """Anti-cheat (two mutants): rb1 (worth \\$40) sells for \\$60.
        The RB ratio must move — $59 of discretionary money left the room
        while only $39 of RB value left the board — and the WR ratio must
        not move at all. A pooled implementation moves both; a static one
        moves neither."""
        board = _par_board([Sale("rb1", 60, 2)])
        inflation = positional_inflation(_inflation_rows(), board)
        assert inflation["RB"] == pytest.approx((87 - 59) / 48)
        assert inflation["RB"] < 1.0
        assert inflation["WR"] == pytest.approx(1.0)

    def test_bargain_inflates_the_remaining_pool(self):
        """The same sale at \\$20 leaves money chasing the survivors."""
        board = _par_board([Sale("rb1", 20, 2)])
        inflation = positional_inflation(_inflation_rows(), board)
        assert inflation["RB"] == pytest.approx((87 - 19) / 48)
        assert inflation["RB"] > 1.0

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
        room's (keeper-reduced) budgets are roughly half the sheet's
        normalization. The ratio must track the actual dollars — a mutant
        that trusts the sheet's own normalization stays at 1.0."""
        poor_room = make_board({1: 48, 2: 48}, drafted_slots=6)
        inflation = positional_inflation(_inflation_rows(), poor_room)
        assert inflation["RB"] == pytest.approx(84 * 0.5 / 87)
        assert inflation["RB"] < 0.55

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


class TestTaperWeight:
    """The taper's shape is part of the contract the tests above rely on."""

    def test_full_fade_and_endpoints(self):
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
