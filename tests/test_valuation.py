"""Static valuation core: band-median room prices with log-curve fallback.

Every fixture is hand-built and anti-cheat: each test fails under a named
plausible-lazy implementation (mean/sum instead of median, curve used when
the band has enough samples, wrong band boundaries).
"""

from __future__ import annotations

import math

import pytest

from draftbot.models import parse_picks
from draftbot.valuation import (
    Bid,
    PriceModel,
    build_bids,
    compute_worths,
    detect_keeper_picks,
    parse_projections,
    replacement_ranks,
)


def _bids(position, pairs):
    """Bids for one position from (adp, amount) pairs."""
    return [Bid(position=position, adp=adp, amount=amount) for adp, amount in pairs]


def _projection_row(player_id, position, adp, pts=100.0, years_exp=3, name="A Player"):
    # pylint: disable=too-many-arguments,too-many-positional-arguments  # fixture
    # builder mirroring the live API row's independent fields one-to-one.
    """One raw projections-endpoint row, shaped like the live API."""
    first, _, last = name.partition(" ")
    return {
        "player_id": player_id,
        "stats": {"adp_half_ppr": adp, "pts_half_ppr": pts},
        "player": {
            "position": position,
            "years_exp": years_exp,
            "first_name": first,
            "last_name": last,
        },
    }


def _raw_pick(player_id, position, amount, slot=1, is_keeper=None):
    """One raw picks-feed entry (bid is a STRING in metadata.amount)."""
    return {
        "player_id": player_id,
        "draft_slot": slot,
        "is_keeper": is_keeper,
        "metadata": {"amount": str(amount), "position": position},
    }


class TestParseProjections:
    """Raw projections rows become per-player SeasonRow records."""

    def test_pulls_adp_points_position_and_experience(self):
        """The four fields the model consumes come from stats/player dicts."""
        rows = [_projection_row("101", "RB", 7.5, pts=210.4, years_exp=2)]
        season = parse_projections(rows)
        row = season["101"]
        assert row.adp == 7.5
        assert row.points == 210.4
        assert row.position == "RB"
        assert row.years_exp_snapshot == 2

    def test_missing_stats_and_player_dicts_do_not_crash(self):
        """Sleeper serves null stats/player sub-objects for fringe players."""
        season = parse_projections([{"player_id": "9", "stats": None, "player": None}])
        row = season["9"]
        assert row.adp is None
        assert row.points is None
        assert row.position is None

    def test_name_joins_first_and_last(self):
        """Display names come straight from the embedded player object."""
        rows = [_projection_row("101", "RB", 7.5, name="Jahmyr Gibbs")]
        assert parse_projections(rows)["101"].name == "Jahmyr Gibbs"


class TestBuildBids:
    """Historical winning bids get tagged with THAT season's ADP."""

    def test_bid_uses_the_draft_seasons_adp_not_another_years(self):
        """Player 101's ADP was 20 in 2023 and 5 in 2024; the 2023 bid must
        carry 20 — an implementation reading one merged ADP map fails."""
        seasons = {
            2023: parse_projections([_projection_row("101", "RB", 20.0)]),
            2024: parse_projections([_projection_row("101", "RB", 5.0)]),
        }
        picks = {2023: parse_picks([_raw_pick("101", "RB", 31)])}
        bids = build_bids(picks, seasons)
        assert bids == [Bid(position="RB", adp=20.0, amount=31)]

    def test_kicker_and_defense_picks_are_excluded(self):
        """Only QB/RB/WR/TE bids form the market model."""
        seasons = {
            2023: parse_projections(
                [
                    _projection_row("k1", "K", 90.0),
                    _projection_row("d1", "DEF", 95.0),
                    _projection_row("101", "WR", 10.0),
                ]
            )
        }
        raw = [
            _raw_pick("k1", "K", 2),
            _raw_pick("d1", "DEF", 1),
            _raw_pick("101", "WR", 25),
        ]
        bids = build_bids({2023: parse_picks(raw)}, seasons)
        assert [bid.position for bid in bids] == ["WR"]

    def test_picks_without_that_seasons_adp_are_excluded(self):
        """No ADP (missing player, null, or the 999 sentinel) means the bid
        cannot be banded and is dropped, exactly like the reference model."""
        seasons = {
            2023: parse_projections(
                [
                    _projection_row("101", "RB", None),
                    _projection_row("102", "RB", 999.0),
                ]
            )
        }
        raw = [
            _raw_pick("101", "RB", 10),
            _raw_pick("102", "RB", 11),
            _raw_pick("103", "RB", 12),  # absent from the projections file
        ]
        assert not build_bids({2023: parse_picks(raw)}, seasons)

    def test_position_comes_from_the_pick_metadata(self):
        """Attribution uses the pick's own position tag (what the room drafted
        him as), matching the reference model."""
        seasons = {2023: parse_projections([_projection_row("101", "WR", 10.0)])}
        picks = {2023: parse_picks([_raw_pick("101", "TE", 9)])}
        assert build_bids(picks, seasons) == [Bid(position="TE", adp=10.0, amount=9)]


class TestKeeperRowExclusion:
    """Keeper rows ride the historical feeds as priced picks (verified in
    the league's 2025 feed, unflagged). A keeper's cost is last year's
    price plus $2, not a market-clearing bid — it must never enter the
    price fit."""

    # Draft order shuffles between seasons: roster 9 drafts from slot 1 in
    # 2023 but slot 2 in 2024, so slot equality is NOT owner equality.
    SLOT_MAPS = {2023: {1: 9, 2: 8}, 2024: {1: 8, 2: 9}}

    def test_flagged_keeper_picks_never_become_bids(self):
        """A pick with is_keeper=true is excluded even with a valid ADP."""
        seasons = {2023: parse_projections([_projection_row("101", "RB", 10.0)])}
        picks = {2023: parse_picks([_raw_pick("101", "RB", 10, is_keeper=True)])}
        assert not build_bids(picks, seasons)

    def test_same_roster_cost_chain_repeat_is_detected(self):
        """Roster 9 pays $1 in 2023 and holds the player at exactly
        max(1+2, 5)=$5 in 2024: that is the keeper signature — including
        the $5 floor (a floorless chain would look for $3)."""
        picks = {
            2023: parse_picks([_raw_pick("k1", "WR", 1, slot=1)]),
            2024: parse_picks([_raw_pick("k1", "WR", 5, slot=2)]),
        }
        assert detect_keeper_picks(picks, self.SLOT_MAPS) == {(2024, "k1")}

    def test_different_roster_price_coincidence_is_not_a_keeper(self):
        """A player sold to a DIFFERENT roster at the chain price is a real
        auction sale (the league's 2025 feed has several: $2 -> $5 across
        rosters). Slot-number equality must not fool the detector."""
        picks = {
            2023: parse_picks([_raw_pick("p1", "WR", 3, slot=1)]),
            # Same SLOT as 2023, but slot 1 belongs to roster 8 in 2024.
            2024: parse_picks([_raw_pick("p1", "WR", 5, slot=1)]),
        }
        assert not detect_keeper_picks(picks, self.SLOT_MAPS)

    def test_same_roster_market_reauction_is_not_a_keeper(self):
        """An owner re-buying his own player at a market price ($25 then
        $38) kept nothing; only the exact cost chain is a keeper."""
        picks = {
            2023: parse_picks([_raw_pick("p1", "WR", 25, slot=1)]),
            2024: parse_picks([_raw_pick("p1", "WR", 38, slot=2)]),
        }
        assert not detect_keeper_picks(picks, self.SLOT_MAPS)

    def test_detected_keepers_are_excluded_from_the_bid_pool(self):
        """End to end: the detected 2024 keeper row vanishes from the fit
        while the genuine 2023 sale of the same player survives."""
        seasons = {
            2023: parse_projections([_projection_row("k1", "WR", 30.0)]),
            2024: parse_projections([_projection_row("k1", "WR", 25.0)]),
        }
        picks = {
            2023: parse_picks([_raw_pick("k1", "WR", 1, slot=1)]),
            2024: parse_picks([_raw_pick("k1", "WR", 5, slot=2)]),
        }
        exclude = detect_keeper_picks(picks, self.SLOT_MAPS)
        assert build_bids(picks, seasons, exclude=exclude) == [
            Bid(position="WR", adp=30.0, amount=1)
        ]


class TestBandMedianRoomPrice:
    """Room price = empirical median of same-position bids in the ADP band."""

    def test_room_price_is_band_median_not_mean_or_sum(self):
        """Median 7; a mean gives 13.33, a sum 80 — both must fail."""
        pairs = [(9.0, 2), (9.5, 3), (10.0, 5), (10.5, 9), (11.0, 21), (11.5, 40)]
        model = PriceModel(_bids("RB", pairs))
        assert model.room_price("RB", 10.0) == 7.0

    def test_band_boundaries_are_adp_over_and_times_ratio_inclusive(self):
        """Band is adp/1.6 .. adp*1.6 inclusive; a $99 bid just outside
        either edge must not drag the median."""
        inside = [(6.25, 4), (7.0, 5), (8.0, 6), (9.0, 7), (12.0, 8), (16.0, 9)]
        outside = [(6.24, 99), (16.01, 99)]
        model = PriceModel(_bids("WR", inside + outside))
        assert model.room_price("WR", 10.0) == 6.5

    def test_band_only_uses_same_position_bids(self):
        """Six RB bids in the ADP range must not price a WR."""
        rb_pairs = [(10.0, 30)] * 6
        wr_pairs = [(9.0, 4), (9.5, 5), (10.0, 6), (10.5, 7), (11.0, 8), (11.5, 9)]
        model = PriceModel(_bids("RB", rb_pairs) + _bids("WR", wr_pairs))
        assert model.room_price("WR", 10.0) == 6.5

    def test_no_adp_prices_at_the_one_dollar_floor(self):
        """None, the 999.0 sentinel, and nonpositive ADP all mean unranked."""
        model = PriceModel(_bids("RB", [(10.0, 30)] * 6))
        for adp in (None, 999.0, 0.0, -3.0):
            assert model.room_price("RB", adp) == 1.0


class TestCurveFallback:
    """The log curve a + b*ln(adp) prices only bands with under 6 samples."""

    # Fit bids sit exactly on amount = 50 - 10*ln(adp), so the recovered
    # curve is known in closed form.
    LINE_PAIRS = [
        (1.0, 50.0),
        (math.e, 40.0),
        (math.e**2, 30.0),
        (math.e**3, 20.0),
    ]

    def test_five_sample_band_falls_back_to_fitted_curve(self):
        """adp 40's band (25..64) holds no fit bids: price comes off the
        fitted line, not the band."""
        model = PriceModel(_bids("QB", self.LINE_PAIRS))
        assert model.room_price("QB", 40.0) == pytest.approx(
            50.0 - 10.0 * math.log(40.0)
        )

    def test_six_sample_band_beats_the_curve(self):
        """With 6 in-band samples the median (7) is authoritative even though
        the fitted curve says ~41 at adp 2.5 — a curve-first implementation
        fails here."""
        extra = [(2.0, 7.0)] * 5 + [(2.5, 40.0)]
        model = PriceModel(_bids("QB", self.LINE_PAIRS + extra))
        band_price = model.room_price("QB", 2.5)
        assert band_price == 7.0
        assert band_price != pytest.approx(model.curve_price("QB", 2.5))

    def test_exactly_five_samples_still_uses_the_curve(self):
        """The band threshold is six: five samples (the fit bid at adp e
        plus these four) are not enough."""
        extra = [(2.0, 7.0)] * 3 + [(2.5, 40.0)]
        model = PriceModel(_bids("QB", self.LINE_PAIRS + extra))
        assert model.room_price("QB", 2.5) == pytest.approx(
            model.curve_price("QB", 2.5)
        )

    def test_curve_never_prices_below_one_dollar(self):
        """Deep ADP would extrapolate negative; $1 is the floor."""
        model = PriceModel(_bids("QB", self.LINE_PAIRS))
        assert model.room_price("QB", 5000.0) == 1.0

    def test_unfittable_position_prices_at_the_floor(self):
        """A position with no bid history (K, DEF) has no curve: $1."""
        model = PriceModel(_bids("QB", self.LINE_PAIRS))
        assert model.room_price("K", 30.0) == 1.0


class TestWorth:
    """Projection value over replacement, converted to auction dollars."""

    ROSTER = {
        "QB": 1,
        "RB": 2,
        "WR": 2,
        "TE": 1,
        "FLEX": 1,
        "K": 1,
        "DEF": 1,
        "BN": 6,
        "IR": 2,
    }

    def test_replacement_ranks_follow_roster_shape_and_flex_split(self):
        """12 teams, 1QB/2RB/2WR/1TE + FLEX split 0.5/0.4/0.1: replacement
        is the first player past the last starter at each position."""
        assert replacement_ranks(self.ROSTER, teams=12) == {
            "QB": 13,
            "RB": 31,
            "WR": 29,
            "TE": 14,
        }

    @staticmethod
    def _tiny_season(points_by_id, position="QB"):
        rows = [
            _projection_row(pid, position, None, pts=pts)
            for pid, pts in points_by_id.items()
        ]
        return parse_projections(rows)

    def test_worth_scales_vorp_to_league_discretionary_dollars(self):
        """2 teams x ($10 budget - 2 drafted slots) = $16 discretionary,
        spread over 60 points of value over replacement (QB rank 3).
        Every worth is $1 base plus the player's share — a mean, a sum,
        or a wrong replacement rank all fail the exact numbers."""
        season = self._tiny_season({"a": 100.0, "b": 80.0, "c": 60.0, "d": 40.0})
        worths = compute_worths(
            season, roster_slots={"QB": 1, "BN": 1}, teams=2, budget=10
        )
        assert worths["a"] == pytest.approx(1 + 40 * 16 / 60)
        assert worths["b"] == pytest.approx(1 + 20 * 16 / 60)
        assert worths["c"] == 1.0
        assert worths["d"] == 1.0
        assert sum(worths.values()) - len(worths) == pytest.approx(16.0)

    def test_ir_slots_are_not_drafted_slots(self):
        """IR spots cost no auction dollars; counting them shrinks the
        discretionary pool and fails the exact worth."""
        season = self._tiny_season({"a": 100.0, "b": 80.0, "c": 60.0})
        worths = compute_worths(
            season, roster_slots={"QB": 1, "BN": 1, "IR": 5}, teams=2, budget=10
        )
        assert worths["a"] == pytest.approx(1 + 40 * 16 / 60)

    def test_kickers_and_defenses_are_roster_fillers_at_one_dollar(self):
        """Monster kicker points never earn auction dollars."""
        rows = [
            _projection_row("k1", "K", None, pts=999.0),
            _projection_row("d1", "DEF", None, pts=999.0),
            _projection_row("q1", "QB", None, pts=100.0),
            _projection_row("q2", "QB", None, pts=50.0),
        ]
        worths = compute_worths(
            parse_projections(rows), roster_slots={"QB": 1}, teams=1, budget=10
        )
        assert worths["k1"] == 1.0
        assert worths["d1"] == 1.0
        assert worths["q1"] > 1.0

    def test_flat_pool_prices_everyone_at_the_floor(self):
        """Zero total value over replacement must not divide by zero."""
        season = self._tiny_season({"a": 50.0, "b": 50.0, "c": 50.0})
        worths = compute_worths(
            season, roster_slots={"QB": 1, "BN": 1}, teams=2, budget=10
        )
        assert set(worths.values()) == {1.0}
