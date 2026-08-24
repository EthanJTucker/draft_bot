"""The dynamic repricing engine: tiers, inflation, need, spend-down.

Every test drives the engine through its public functions over hand
fixtures built with the ``helpers_engine`` builders; the anti-cheat
fixtures are inputs where a trivially-wrong implementation (pooled
inflation, missing taper, quota-count need, off-by-one tier flag)
returns a detectably wrong value.
"""

from __future__ import annotations

from draftbot.draft_engine import build_tiers, tier_status

from .helpers_engine import sheet_row


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
