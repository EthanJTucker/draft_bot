"""Static valuation core: band-median room prices with log-curve fallback.

Every fixture is hand-built and anti-cheat: each test fails under a named
plausible-lazy implementation (mean/sum instead of median, curve used when
the band has enough samples, wrong band boundaries).
"""

from __future__ import annotations

import math

import pytest

from draftbot.config import DEFAULT_STARTER_PCT, LeagueConfig
from draftbot.models import parse_picks
from draftbot.valuation import (
    Bid,
    OptionValues,
    PriceModel,
    SheetRow,
    _fit_models,
    bench_replacement_ranks,
    build_bids,
    build_value_sheet,
    compute_worths,
    detect_keeper_picks,
    parse_projections,
    replacement_ranks,
    skill_slot_target,
    value_map,
)
from tests.helpers_valuation import projection_row


def _bids(position, pairs):
    """Bids for one position from (adp, amount) pairs."""
    return [Bid(position=position, adp=adp, amount=amount) for adp, amount in pairs]


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
        rows = [projection_row("101", "RB", 7.5, pts=210.4, years_exp=2)]
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
        rows = [projection_row("101", "RB", 7.5, name="Jahmyr Gibbs")]
        assert parse_projections(rows)["101"].name == "Jahmyr Gibbs"

    def test_no_adp_sentinel_normalizes_to_none(self):
        """Sleeper's 999.0 means "no ADP": the sentinel becomes None at
        parse time, so no downstream consumer (the emitted CSV included)
        ever sees the magic number. A real ADP passes through untouched."""
        season = parse_projections(
            [projection_row("101", "RB", 999.0), projection_row("102", "RB", 7.5)]
        )
        assert season["101"].adp is None
        assert season["102"].adp == 7.5


class TestBuildBids:
    """Historical winning bids get tagged with THAT season's ADP."""

    def test_bid_uses_the_draft_seasons_adp_not_another_years(self):
        """Player 101's ADP was 20 in 2023 and 5 in 2024; the 2023 bid must
        carry 20 — an implementation reading one merged ADP map fails."""
        seasons = {
            2023: parse_projections([projection_row("101", "RB", 20.0)]),
            2024: parse_projections([projection_row("101", "RB", 5.0)]),
        }
        picks = {2023: parse_picks([_raw_pick("101", "RB", 31)])}
        bids = build_bids(picks, seasons)
        assert bids == [Bid(position="RB", adp=20.0, amount=31)]

    def test_kicker_and_defense_picks_are_excluded(self):
        """Only QB/RB/WR/TE bids form the market model."""
        seasons = {
            2023: parse_projections(
                [
                    projection_row("k1", "K", 90.0),
                    projection_row("d1", "DEF", 95.0),
                    projection_row("101", "WR", 10.0),
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
                    projection_row("101", "RB", None),
                    projection_row("102", "RB", 999.0),
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
        seasons = {2023: parse_projections([projection_row("101", "WR", 10.0)])}
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
        seasons = {2023: parse_projections([projection_row("101", "RB", 10.0)])}
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
            2023: parse_projections([projection_row("k1", "WR", 30.0)]),
            2024: parse_projections([projection_row("k1", "WR", 25.0)]),
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
        """Deep ADP would extrapolate negative; $1 is the floor — and the
        source says so honestly ("curve_floor", not a fitted "curve")."""
        model = PriceModel(_bids("QB", self.LINE_PAIRS))
        assert model.room_price("QB", 5000.0) == 1.0
        assert model.price_source("QB", 5000.0) == "curve_floor"

    def test_unfittable_position_prices_at_the_floor(self):
        """A position with no bid history (K, DEF) has no curve: $1."""
        model = PriceModel(_bids("QB", self.LINE_PAIRS))
        assert model.room_price("K", 30.0) == 1.0
        assert model.price_source("K", 30.0) == "floor"


class TestCurveCap:
    """Curve-fallback prices are capped at the position's max observed bid
    (decided 2026-08-23: the room has never paid more than its own record
    at a position, and an extrapolated curve must not claim it will)."""

    # RB curve through ($20 @ adp 10, $5 @ adp 20) extrapolates to ~$69.8
    # at adp 1 — far past the $20 the room has ever paid for an RB.
    RB_PAIRS = [(10.0, 20.0), (20.0, 5.0)]

    def test_cap_binds_when_the_curve_passes_the_positions_top_bid(self):
        """The wild ~$69.8 extrapolation prices at the $20 record, and the
        source column says the cap (not the curve) set the number."""
        model = PriceModel(_bids("RB", self.RB_PAIRS))
        assert model.curve_price("RB", 1.0) > 20.0  # the raw curve is wild
        assert model.room_price("RB", 1.0) == 20.0
        assert model.price_source("RB", 1.0) == "curve_capped"

    def test_cap_is_per_position_not_global(self):
        """Anti-cheat: WR history holds $100 bids, so a min over ALL
        positions' bids leaves the ~$69.8 RB curve uncapped and fails;
        the cap must be the RB max ($20)."""
        wr = _bids("WR", [(50.0, 100.0)] * 6)
        model = PriceModel(_bids("RB", self.RB_PAIRS) + wr)
        assert 20.0 < model.curve_price("RB", 1.0) < 100.0  # the trap is live
        assert model.room_price("RB", 1.0) == 20.0
        assert model.price_source("RB", 1.0) == "curve_capped"

    def test_curve_below_the_cap_is_untouched_and_labeled_curve(self):
        """adp 15 sits between the fit bids: the curve reads ~$12.4, under
        the $20 cap, and passes through unchanged."""
        model = PriceModel(_bids("RB", self.RB_PAIRS))
        assert model.curve_price("RB", 15.0) < 20.0
        assert model.room_price("RB", 15.0) == pytest.approx(
            model.curve_price("RB", 15.0)
        )
        assert model.price_source("RB", 15.0) == "curve"

    def test_cap_can_be_disabled(self):
        """curve_cap=False restores the raw fitted curve (the pre-decision
        behavior, kept reachable through config)."""
        model = PriceModel(_bids("RB", self.RB_PAIRS), curve_cap=False)
        assert model.room_price("RB", 1.0) == pytest.approx(
            model.curve_price("RB", 1.0)
        )
        assert model.price_source("RB", 1.0) == "curve"

    def test_band_median_is_never_capped(self):
        """A 6-sample band prices at its median with source "band" even
        when the median IS the position's top bid — the cap applies only
        to curve fallbacks (a band median cannot exceed the position max
        by construction; this pins that the plumbing agrees)."""
        pairs = [(9.0, 66.0), (9.5, 66.0), (10.0, 66.0)] * 2
        model = PriceModel(_bids("RB", pairs))
        assert model.room_price("RB", 10.0) == 66.0
        assert model.price_source("RB", 10.0) == "band"


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
            projection_row(pid, position, None, pts=pts)
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
            projection_row("k1", "K", None, pts=999.0),
            projection_row("d1", "DEF", None, pts=999.0),
            projection_row("q1", "QB", None, pts=100.0),
            projection_row("q2", "QB", None, pts=50.0),
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


class TestBlendedWorth:
    """Worth split between the starter baseline and a deeper bench one."""

    #: QB1 + 1 bench slot, 2 teams, $10: $16 discretionary. Starter
    #: replacement is QB3 (60 pts), the bench baseline QB5 (20 pts), so
    #: starter VORP totals 60 and bench VORP totals 200.
    POINTS = {"a": 100.0, "b": 80.0, "c": 60.0, "d": 40.0, "e": 20.0}
    ROSTER = {"QB": 1, "BN": 1}
    BENCH = {"QB": 5, "RB": 1, "WR": 1, "TE": 1}

    @classmethod
    def _worths(cls, **kwargs):
        season = parse_projections(
            [
                projection_row(pid, "QB", None, pts=pts)
                for pid, pts in cls.POINTS.items()
            ]
        )
        return compute_worths(
            season, roster_slots=cls.ROSTER, teams=2, budget=10, **kwargs
        )

    def test_half_the_pool_flows_through_each_baseline(self):
        """worth = 1 + 0.5*16*(vorp_starter/60) + 0.5*16*(vorp_bench/200).

        Anti-cheat: this is the blend itself, not either endpoint. A
        starter-only model gives a=$11.67 and c=$1.00; a bench-only model
        gives a=$7.40 and c=$4.20; blending the two VORPs before scaling
        (rather than the two dollar halves) gives a=$8.38.
        """
        worths = self._worths(bench_ranks=self.BENCH, starter_pct=0.5)
        assert worths["a"] == pytest.approx(1 + 8 * (40 / 60) + 8 * (80 / 200))
        assert worths["b"] == pytest.approx(1 + 8 * (20 / 60) + 8 * (60 / 200))
        assert worths["c"] == pytest.approx(1 + 8 * (40 / 200))
        assert worths["d"] == pytest.approx(1 + 8 * (20 / 200))
        assert worths["e"] == 1.0

    def test_the_blend_sits_strictly_between_the_two_endpoints(self):
        """Pinned against both degenerate implementations at once: the
        blended price of the best player is below starter-only and above
        bench-only, and a replacement-level starter is priced above $1."""
        blended = self._worths(bench_ranks=self.BENCH, starter_pct=0.5)
        starter_only = self._worths(bench_ranks=self.BENCH, starter_pct=1.0)
        bench_only = self._worths(bench_ranks=self.BENCH, starter_pct=0.0)
        assert bench_only["a"] < blended["a"] < starter_only["a"]
        assert starter_only["c"] == 1.0 < blended["c"] < bench_only["c"]

    def test_the_normalization_invariant_survives_the_blend(self):
        """The two halves sum to the whole discretionary pool exactly, at
        every blend weight — the property the live max-bid math rests on."""
        for starter_pct in (0.0, 0.25, 0.5, 0.75, 1.0):
            worths = self._worths(bench_ranks=self.BENCH, starter_pct=starter_pct)
            assert sum(worths.values()) - len(worths) == pytest.approx(16.0)

    def test_omitting_the_bench_baseline_reproduces_the_starter_only_sheet(self):
        """No bench ranks means no second baseline: worths are identical to
        the starter-only model at any blend weight."""
        legacy = self._worths()
        assert legacy == self._worths(starter_pct=0.0)
        assert legacy == self._worths(bench_ranks=None, starter_pct=0.5)
        assert legacy["a"] == pytest.approx(1 + 16 * (40 / 60))

    def test_the_keyword_default_is_the_configured_blend_weight(self):
        """``starter_pct``'s signature default is live API surface: this is
        the only test that omits it while a REAL bench baseline is in play,
        so the default actually decides something. Both directions, so a
        default silently moved to 1.0 (which restores the pre-blend sheet
        and disables the second baseline entirely) fails here."""
        implied = self._worths(bench_ranks=self.BENCH)
        assert implied == self._worths(
            bench_ranks=self.BENCH, starter_pct=DEFAULT_STARTER_PCT
        )
        assert implied != self._worths(bench_ranks=self.BENCH, starter_pct=1.0)

    def test_a_dead_starter_half_hands_its_money_to_the_live_half(self):
        """Conservation when one baseline has nothing to price.

        The top three are tied at the starter baseline (QB3), so starter
        VORP is 0 for every player while the deeper QB5 baseline still
        sees 260 points of value. Rating a zero-total half at $0/point
        silently deletes ``starter_pct`` of the room's money from a sheet
        that still looks completely normal — here half of $16. The whole
        $16 has to reach the board, and nobody may sit under the floor."""
        season = parse_projections(
            [
                projection_row(pid, "QB", None, pts=pts)
                for pid, pts in {
                    "a": 100.0,
                    "b": 100.0,
                    "c": 100.0,
                    "d": 40.0,
                    "e": 20.0,
                }.items()
            ]
        )
        worths = compute_worths(
            season,
            roster_slots=self.ROSTER,
            teams=2,
            budget=10,
            bench_ranks=self.BENCH,
            starter_pct=0.5,
        )
        assert sum(worths.values()) - len(worths) == pytest.approx(16.0)
        assert min(worths.values()) == 1.0
        # Degraded to the single surviving baseline, not to a scaled-down
        # sheet: every dollar is priced off the bench VORP total of 260.
        assert worths["a"] == pytest.approx(1 + 16 * (80 / 260))

    @pytest.mark.parametrize("starter_pct", [1.5, -0.5, 2.0])
    def test_an_out_of_range_blend_weight_is_refused_by_value(self, starter_pct):
        """The $1 FLOOR_PRICE is a guarantee of this function, not of its
        caller. Above 1.0 the bench half goes negative and prices players
        under the floor (measured -$0.60 at 1.5); below 0.0 the starter
        half inverts and worse players outprice better ones. Conservation
        holds in both cases, so no invariant test would notice."""
        with pytest.raises(ValueError, match="starter_pct"):
            self._worths(bench_ranks=self.BENCH, starter_pct=starter_pct)

    def test_a_bench_rank_shallower_than_the_starter_rank_is_refused(self):
        """``bench_replacement_ranks`` clamps this, but ``compute_worths``
        is module-public and the clamp is not its property. A shallower
        "deeper" baseline produces a plausible, conserving, WRONG sheet."""
        shallow = {**self.BENCH, "QB": 2}
        with pytest.raises(ValueError, match="QB"):
            self._worths(bench_ranks=shallow, starter_pct=0.5)

    def test_a_bench_rank_past_the_pool_falls_back_to_the_worst_player(self):
        """A rank deeper than the position's own pool must not collapse
        replacement to ZERO points.

        At zero, that position's bench VORP becomes raw points: every
        player at it prices above $1 — including the worst, who IS
        replacement level by definition — and the dollars come out of
        every other position. With four TEs and a bench rank of 6, TE4
        has to stay a $1 filler."""
        rows = [
            projection_row(f"te{index}", "TE", None, pts=points)
            for index, points in enumerate([80.0, 60.0, 40.0, 20.0], start=1)
        ] + [
            projection_row("q1", "QB", None, pts=100.0),
            projection_row("q2", "QB", None, pts=50.0),
            projection_row("q3", "QB", None, pts=25.0),
        ]
        worths = compute_worths(
            parse_projections(rows),
            roster_slots=self.ROSTER,
            teams=2,
            budget=10,
            bench_ranks={"QB": 3, "RB": 1, "WR": 1, "TE": 6},
            starter_pct=0.5,
        )
        assert worths["te4"] == 1.0
        assert sum(worths.values()) - len(worths) == pytest.approx(16.0)

    def test_kickers_and_defenses_stay_at_one_dollar_under_the_blend(self):
        """K/DEF are outside VORP on both baselines."""
        rows = [
            projection_row("k1", "K", None, pts=999.0),
            projection_row("d1", "DEF", None, pts=999.0),
            projection_row("q1", "QB", None, pts=100.0),
            projection_row("q2", "QB", None, pts=50.0),
            projection_row("q3", "QB", None, pts=25.0),
        ]
        worths = compute_worths(
            parse_projections(rows),
            roster_slots={"QB": 1, "K": 1, "DEF": 1},
            teams=1,
            budget=10,
            bench_ranks={"QB": 3, "RB": 1, "WR": 1, "TE": 1},
            starter_pct=0.5,
        )
        assert worths["k1"] == 1.0
        assert worths["d1"] == 1.0
        assert worths["q2"] > 1.0

    def test_a_flat_pool_divides_by_no_zero_on_either_half(self):
        """Both denominators can be zero at once; neither may raise."""
        season = parse_projections(
            [projection_row(pid, "QB", None, pts=50.0) for pid in ("a", "b", "c")]
        )
        worths = compute_worths(
            season,
            roster_slots={"QB": 1, "BN": 1},
            teams=2,
            budget=10,
            bench_ranks={"QB": 3, "RB": 1, "WR": 1, "TE": 1},
            starter_pct=0.5,
        )
        assert set(worths.values()) == {1.0}


class TestSkillSlotTarget:
    """How many skill-position lots the league actually drafts."""

    def test_target_excludes_ir_and_the_k_def_slots(self):
        """12 teams x (15 drafted slots - 1 K - 1 DEF) = 156. Counting IR
        (17 slots) or forgetting to drop K/DEF (15 slots) both give 180."""
        assert skill_slot_target(TestWorth.ROSTER, teams=12) == 156

    def test_target_reads_the_roster_shape_rather_than_a_constant(self):
        """A different shape moves the target: 3 teams x (7 drafted - 2 K
        - 1 DEF) = 12."""
        roster = {"QB": 1, "RB": 1, "K": 2, "DEF": 1, "BN": 2, "IR": 1}
        assert skill_slot_target(roster, teams=3) == 12


def _season_picks(counts, year):
    """One season's picks feed: ``counts`` players per position, each a
    distinct id, every bid $5."""
    raw = [
        _raw_pick(f"{year}-{position}-{index}", position, 5)
        for position in sorted(counts)
        for index in range(counts[position])
    ]
    return parse_picks(raw)


class TestBenchReplacementRanks:
    """The deeper baseline, read off this league's own draft composition."""

    #: Three seasons of very different SIZE but stated positional mix.
    #: Shares are QB/RB/WR/TE = .1/.4/.4/.1, .1/.2/.6/.1, .2/.4/.2/.2.
    #: Each season also drafts K and DEF, which are not skill lots.
    HISTORY = {
        2023: {"QB": 10, "RB": 40, "WR": 40, "TE": 10, "K": 20, "DEF": 20},
        2024: {"QB": 5, "RB": 10, "WR": 30, "TE": 5, "K": 6, "DEF": 6},
        2025: {"QB": 4, "RB": 8, "WR": 4, "TE": 4, "K": 2, "DEF": 2},
    }

    STARTERS = {"QB": 3, "RB": 5, "WR": 5, "TE": 3}

    @classmethod
    def _picks(cls):
        return {
            year: _season_picks(counts, year) for year, counts in cls.HISTORY.items()
        }

    def test_ranks_are_the_median_share_of_the_skill_pool(self):
        """MEDIAN share x 100 skill lots = QB10 / RB40 / WR40 / TE10.

        Anti-cheat: the mean share gives QB13/RB33/TE13, pooling every
        season's raw counts gives QB11/RB34/WR44/TE11, using raw counts
        instead of shares gives QB5/RB10, and counting the K/DEF picks in
        the denominator drags every rank down (QB7 in 2023 alone).
        """
        assert bench_replacement_ranks(
            self._picks(), self.STARTERS, skill_slots=100
        ) == {"QB": 10, "RB": 40, "WR": 40, "TE": 10}

    def test_ranks_scale_with_the_league_s_own_slot_target(self):
        """The mix is a share, so a 200-lot league doubles every rank."""
        assert bench_replacement_ranks(
            self._picks(), self.STARTERS, skill_slots=200
        ) == {"QB": 20, "RB": 80, "WR": 80, "TE": 20}

    def test_a_bench_rank_never_lands_shallower_than_the_starter_rank(self):
        """WR's derived rank is 40; a league that starts 60 WRs clamps it
        to 60, because bench VORP below starter VORP is a bug."""
        starters = {**self.STARTERS, "WR": 60}
        assert bench_replacement_ranks(self._picks(), starters, skill_slots=100) == {
            "QB": 10,
            "RB": 40,
            "WR": 60,
            "TE": 10,
        }

    def test_a_thin_position_falls_back_to_its_starter_rank_and_says_so(self):
        """One season of TE data is not a median: TE falls back to its
        starter rank (a no-op blend at TE) and warns by name, while the
        positions with two seasons still derive normally."""
        picks = {
            2024: _season_picks({"QB": 10, "RB": 40, "WR": 40, "TE": 10}, 2024),
            2025: _season_picks({"QB": 20, "RB": 40, "WR": 40, "TE": 0}, 2025),
        }
        with pytest.warns(UserWarning, match="TE") as caught:
            ranks = bench_replacement_ranks(picks, self.STARTERS, skill_slots=100)
        assert ranks == {"QB": 15, "RB": 40, "WR": 40, "TE": 3}
        # The message must not read as "priced as before". A fallback
        # position is still priced off a bench half normalized across all
        # four positions, so its dollars move — measured at a 25% swing on
        # a constructed two-season history. An operator who reads "no bench
        # pricing" and skips the sheet is reading the opposite of the truth.
        message = str(caught[0].message)
        assert "gets no bench pricing" not in message
        assert "no DEEPER baseline" in message
        assert "dollars still move" in message

    #: Every season drafts the same number at each position, so every
    #: median share is EXACTLY 0.25 and the rank is a pure function of the
    #: slot target — which is what makes the rounding rule observable.
    EVEN_HISTORY = {
        year: {"QB": 5, "RB": 5, "WR": 5, "TE": 5, "K": 3, "DEF": 3}
        for year in (2023, 2024, 2025)
    }

    @classmethod
    def _even_picks(cls):
        return {
            year: _season_picks(counts, year)
            for year, counts in cls.EVEN_HISTORY.items()
        }

    @pytest.mark.parametrize(
        "skill_slots,expected",
        [
            (69, 17),  # 17.25 -> 17; math.ceil would say 18
            (54, 14),  # 13.50 -> 14; math.floor and int() would say 13
            (50, 12),  # 12.50 -> 12; round-half-UP would say 13
        ],
    )
    def test_the_rank_rounds_to_nearest_with_ties_to_even(self, skill_slots, expected):
        """The rounding rule itself, on deliberately fractional products.

        Every other bench fixture lands ``skill_slots * median(share)`` on
        an exact integer, where ``round``, ``math.floor``, ``int`` and
        ``math.ceil`` are indistinguishable — and on the real feeds the
        difference moves three of the four ranks that drive prices. These
        three products separate all four: 17.25 kills ``ceil``, 13.5 kills
        ``floor`` and ``int``, and 12.5 pins Python's round-half-to-even
        against a half-up implementation.
        """
        ranks = bench_replacement_ranks(
            self._even_picks(), self.STARTERS, skill_slots=skill_slots
        )
        assert ranks == {position: expected for position in ("QB", "RB", "WR", "TE")}

    def test_an_undrafted_season_counts_as_a_zero_share_not_a_missing_one(self):
        """A season the room drafted no TE in is evidence ABOUT TE, and
        the median has to see it.

        TE shares here are 0.0 / 0.05 / 0.15. Over all three seasons the
        median is 0.05 and the bench rank is 5; dropping the zero takes the
        median over only the seasons the room wanted a TE, which reads 0.10
        and rank 10 — twice as deep a baseline for the position the room
        demonstrably wants LEAST, which raises its VORP and pulls dollars
        toward it. TE was drafted in two seasons, so this is the derived
        path, not the fallback: no warning is expected."""
        picks = {
            2023: _season_picks({"QB": 10, "RB": 45, "WR": 45, "TE": 0}, 2023),
            2024: _season_picks({"QB": 10, "RB": 45, "WR": 40, "TE": 5}, 2024),
            2025: _season_picks({"QB": 10, "RB": 40, "WR": 35, "TE": 15}, 2025),
        }
        ranks = bench_replacement_ranks(picks, self.STARTERS, skill_slots=100)
        assert ranks["TE"] == 5

    def test_a_repeated_player_id_counts_once(self):
        """Distinct PLAYERS, not feed rows. The 2025 feed is known to carry
        unflagged keeper rows entered as ordinary picks, so a duplicated or
        re-listed row is a real shape: counting rows instead of players
        would inflate that position's share and deepen its bench rank.

        Both seasons name the same eight RBs, but 2025 lists four of them
        twice. De-duplicated, every season reads 8 RB of a 16-lot pool for
        a 0.5 share and rank 50; counting rows reads 12 of 20 in 2025, so
        the median share becomes 0.55 and the rank 55."""
        counts = {"QB": 2, "RB": 8, "WR": 4, "TE": 2}
        clean = _season_picks(counts, 2024)
        doubled = _season_picks(counts, 2025)
        repeats = [pick for pick in doubled if pick.metadata["position"] == "RB"][:4]
        ranks = bench_replacement_ranks(
            {2024: clean, 2025: list(doubled) + repeats},
            {"QB": 1, "RB": 1, "WR": 1, "TE": 1},
            skill_slots=100,
        )
        assert ranks["RB"] == 50

    def test_no_usable_history_degrades_loudly_to_the_starter_baseline(self):
        """An empty history must not raise; it warns for every position and
        leaves the sheet exactly where the starter baseline puts it."""
        with pytest.warns(UserWarning):
            ranks = bench_replacement_ranks({}, self.STARTERS, skill_slots=100)
        assert ranks == self.STARTERS


def _league_config(**overrides):
    """A minimal LeagueConfig for model-fit tests (tiny two-team league);
    valuation knobs default to the decided values unless overridden."""
    base = {
        "league_name": "T",
        "season": 2026,
        "league_id": "L",
        "draft_id": "D",
        "teams": 2,
        "auction_budget": 12,
        "roster_slots": {"QB": 1, "BN": 1},
        "keeper_cost_increment": 2,
        "keeper_cost_floor": 5,
        "max_keepers": 3,
        "max_consecutive_keep_years": 2,
        "my_username": "u",
        "my_user_id": "i",
        "my_roster_id": 1,
    }
    base.update(overrides)
    return LeagueConfig(**base)


class TestValuationConfigFlow:
    """The [valuation] knobs flow from LeagueConfig into the fitted models."""

    @staticmethod
    def _world():
        """Two RB bids ($20 @ adp 10, $5 @ adp 20) whose curve extrapolates
        to ~$69.8 at adp 1, far past the $20 position max."""
        seasons = {
            2023: parse_projections(
                [projection_row("a", "RB", 10.0), projection_row("b", "RB", 20.0)]
            ),
            2026: parse_projections([]),
        }
        picks = {
            2023: parse_picks(
                [_raw_pick("a", "RB", 20, slot=1), _raw_pick("b", "RB", 5, slot=2)]
            )
        }
        return seasons, picks

    def test_gamma_flows_to_the_keeper_model(self):
        """premium = g*OV1 + g^2*OV2 at the CONFIG gamma, not the default."""
        seasons, picks = self._world()
        _, keeper = _fit_models(seasons, picks, _league_config(gamma=0.25), None)
        options = OptionValues(one_year=1.0, two_year=1.0, pool_size=1, widen_level=0)
        assert keeper.premium(options) == pytest.approx(0.25 + 0.25**2)

    def test_curve_cap_toggle_flows_to_the_price_model(self):
        """curve_cap=false in config restores the raw fitted curve."""
        seasons, picks = self._world()
        capped, _ = _fit_models(seasons, picks, _league_config(), None)
        assert capped.room_price("RB", 1.0) == 20.0
        assert capped.price_source("RB", 1.0) == "curve_capped"
        uncapped, _ = _fit_models(seasons, picks, _league_config(curve_cap=False), None)
        assert uncapped.room_price("RB", 1.0) > 20.0
        assert uncapped.price_source("RB", 1.0) == "curve"

    def test_band_settings_flow_to_the_price_model(self):
        """min_band_samples=2 alone leaves adp 10 on the curve (its 1.6-ratio
        band holds one bid); widening band_ratio to 5.0 pulls both bids in
        and prices the $12.50 band median — each knob observably flows."""
        seasons, picks = self._world()
        narrow, _ = _fit_models(
            seasons, picks, _league_config(min_band_samples=2), None
        )
        assert narrow.price_source("RB", 10.0) == "curve"
        wide, _ = _fit_models(
            seasons, picks, _league_config(min_band_samples=2, band_ratio=5.0), None
        )
        assert wide.room_price("RB", 10.0) == 12.5
        assert wide.price_source("RB", 10.0) == "band"


class TestSheetBenchBaseline:
    """The sheet derives its bench baseline from the picks feeds it is given."""

    #: 2 teams, QB1 + 1 bench, $10: starter replacement is QB3 (60 pts) and
    #: the derived bench baseline is QB4 (40 pts), over $16 discretionary.
    POINTS = {"a": 100.0, "b": 80.0, "c": 60.0, "d": 40.0, "e": 20.0}

    @classmethod
    def _world(cls):
        """A season of five QBs and two historical drafts of QBs only, so
        the derived mix is 100% QB and the depth target is 2 x (2 - 0) = 4."""
        seasons = {
            2024: parse_projections([]),
            2025: parse_projections([]),
            2026: parse_projections(
                [
                    projection_row(pid, "QB", None, pts=pts)
                    for pid, pts in cls.POINTS.items()
                ]
            ),
        }
        picks = {
            year: _season_picks({"QB": 4, "RB": 0, "WR": 0, "TE": 0}, year)
            for year in (2024, 2025)
        }
        return seasons, picks

    @classmethod
    def _worth_by_id(cls, **overrides):
        seasons, picks = cls._world()
        rows = build_value_sheet(
            seasons, picks, _league_config(auction_budget=10, **overrides)
        )
        return {row.player_id: row.worth for row in rows}

    def test_the_bench_baseline_funds_players_the_starter_cliff_left_at_a_dollar(self):
        """QB3 is exactly replacement level on the starter baseline, so the
        old sheet pinned him at $1. Half the pool now prices off QB4, which
        pays him 8 * 20/120."""
        worths = self._worth_by_id()
        assert worths["c"] == pytest.approx(1 + 8 * (20 / 120))
        assert worths["a"] == pytest.approx(1 + 8 * (40 / 60) + 8 * (60 / 120))

    def test_starter_pct_one_restores_the_old_starter_only_sheet(self):
        """The knob is real: all the money back on the starter baseline
        re-pins QB3 at the $1 floor."""
        worths = self._worth_by_id(starter_pct=1.0)
        assert worths["c"] == 1.0
        assert worths["a"] == pytest.approx(1 + 16 * (40 / 60))

    def test_an_explicit_slot_target_overrides_the_derived_one(self):
        """bench_skill_slots=8 pushes the bench baseline past the whole
        five-player pool, so bench VORP is raw points."""
        worths = self._worth_by_id(bench_skill_slots=8)
        assert worths["c"] == pytest.approx(1 + 8 * (60 / 300))


def _sheet_row(rank, player_id, value):
    """A minimal SheetRow for seam tests (floor player, custom value)."""
    return SheetRow(
        rank=rank,
        player_id=player_id,
        name=f"P {player_id}",
        position="RB",
        adp=None,
        points=None,
        worth=1.0,
        room_price=1.0,
        price_source="floor",
        keeper_premium=0.0,
        value=value,
    )


def test_value_map_keys_player_ids_to_sheet_values():
    """The player_id -> value seam the live tracker injects (issue #4):
    one mapping, straight off the rows."""
    rows = [_sheet_row(1, "a", 42.5), _sheet_row(2, "b", 1.0)]
    assert value_map(rows) == {"a": 42.5, "b": 1.0}
