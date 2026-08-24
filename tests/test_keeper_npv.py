"""Keeper NPV: as-of-season transitions, comparable pools, option values.

The central anti-cheat here is the verified pitfall: the projections API
embeds the CURRENT years_exp snapshot even in historical season files, so
an implementation that buckets transition samples by snapshot experience
produces a detectably different distribution and fails these tests.
"""

from __future__ import annotations

import pytest

from draftbot.valuation import (
    Bid,
    KeeperModel,
    KeeperRules,
    PriceModel,
    TransitionSample,
    build_transition_samples,
    experience_as_of,
    parse_projections,
)


def _row(player_id, position, adp, years_exp):
    """A raw projections row trimmed to what transitions consume."""
    return {
        "player_id": player_id,
        "stats": {"adp_half_ppr": adp, "pts_half_ppr": None},
        "player": {"position": position, "years_exp": years_exp},
    }


def _season(rows):
    return parse_projections(rows)


def _flat_price_bids():
    """Bids pinning exact room prices: RB adp 10 -> $10, adp 20 -> $5,
    adp 5 -> $30 (each band has six samples and no cross-contamination)."""
    return (
        [Bid("RB", 10.0, 10)] * 6 + [Bid("RB", 20.0, 5)] * 6 + [Bid("RB", 5.0, 30)] * 6
    )


class TestExperienceAsOf:
    """exp_t = snapshot_exp - (current_season - t), floored at 0."""

    def test_historical_season_subtracts_the_year_gap(self):
        """A 2023 sample of a 3-year snapshot player has 0 years then."""
        assert experience_as_of(3, season=2023, current_season=2026) == 0
        assert experience_as_of(9, season=2024, current_season=2026) == 7

    def test_current_season_is_the_snapshot_itself(self):
        """No gap to subtract for the season being drafted."""
        assert experience_as_of(3, season=2026, current_season=2026) == 3

    def test_never_negative(self):
        """Pre-rookie seasons clamp to 0, never -2."""
        assert experience_as_of(1, season=2023, current_season=2026) == 0

    def test_unknown_experience_stays_unknown(self):
        """No snapshot means no bucket, not a fake rookie."""
        assert experience_as_of(None, season=2023, current_season=2026) is None


class TestBuildTransitionSamples:
    """Year-over-year price ratios with as-of-season experience buckets."""

    def test_samples_record_as_of_season_experience_not_snapshot(self):
        """Player x carries snapshot years_exp=3 in EVERY season file; his
        2023 sample must say experience 0 and his 2024 sample 1. Recording
        the snapshot value (3) is exactly the verified API pitfall."""
        model = PriceModel(_flat_price_bids())
        seasons = {
            2023: _season([_row("x", "RB", 10.0, 3)]),
            2024: _season([_row("x", "RB", 20.0, 3)]),
            2025: _season([_row("x", "RB", 20.0, 3)]),
        }
        samples = build_transition_samples(seasons, model, current_season=2026)
        assert samples == [
            TransitionSample(
                position="RB", experience=0, price=10.0, ratio_one=0.5, ratio_two=0.5
            ),
            TransitionSample(
                position="RB", experience=1, price=5.0, ratio_one=1.0, ratio_two=None
            ),
        ]

    def test_cheap_lots_are_not_transition_samples(self):
        """Prices under $3 are noise (the reference model's filter)."""
        bids = [Bid("RB", 40.0, 2)] * 6 + [Bid("RB", 10.0, 10)] * 6
        model = PriceModel(bids)
        seasons = {
            2023: _season([_row("cheap", "RB", 40.0, 1)]),
            2024: _season([]),
        }
        assert not build_transition_samples(seasons, model, current_season=2026)

    def test_vanishing_from_next_season_means_a_one_dollar_price(self):
        """A player missing from t+1 fell off the board: his next price is
        the $1 floor, not a dropped sample (survivorship trap)."""
        model = PriceModel(_flat_price_bids())
        seasons = {
            2023: _season([_row("x", "RB", 10.0, 3)]),
            2024: _season([]),
        }
        samples = build_transition_samples(seasons, model, current_season=2026)
        assert samples == [
            TransitionSample(
                position="RB", experience=0, price=10.0, ratio_one=0.1, ratio_two=None
            )
        ]


class TestKeeperCostChain:
    """cost = max(price + $2, $5); chained for year two; capped at 2 keeps."""

    def test_cost_is_price_plus_two_with_a_five_dollar_floor(self):
        """max(price+2, 5): cheap players still cost $5 to keep."""
        model = KeeperModel([])
        assert model.keeper_cost(1) == 5  # 1+2=3 would drop the floor
        assert model.keeper_cost(3) == 5
        assert model.keeper_cost(4) == 6
        assert model.keeper_cost(10) == 12

    def test_year_two_cost_chains_through_the_floor(self):
        """A $1 player costs $5 to keep, then $7 — not $3 then $5."""
        model = KeeperModel([])
        assert model.keeper_cost(model.keeper_cost(1)) == 7

    def test_rules_are_configurable(self):
        """Increment and floor come from KeeperRules, not literals."""
        model = KeeperModel([], rules=KeeperRules(cost_increment=3, cost_floor=10))
        assert model.keeper_cost(1) == 10
        assert model.keeper_cost(9) == 12


def _young_pool_samples():
    """24 young RBs whose price collapses (r1=0.1) plus one young breakout
    (r1=3.0) whose snapshot years_exp would misfile him as a veteran."""
    fades = [
        TransitionSample("RB", experience=0, price=10.0, ratio_one=0.1, ratio_two=None)
    ] * 24
    breakout = TransitionSample(
        "RB", experience=0, price=10.0, ratio_one=3.0, ratio_two=0.1
    )
    return fades + [breakout]


class TestComparablePoolAndOptionValues:
    """Option value = discounted expected keep-again profit over the pool."""

    def test_young_candidate_prices_off_the_young_pool(self):
        """25 young samples at level 0: ov1 = E[max(0, v0*r1 - cost)] =
        (24*0 + 18)/25 = 0.72; under 10 two-year samples halves it."""
        model = KeeperModel(_young_pool_samples())
        options = model.option_values("RB", experience=1, room_price=10.0, price=10.0)
        assert options.one_year == pytest.approx(0.72)
        assert options.two_year == pytest.approx(0.36)
        assert options.pool_size == 25
        assert options.widen_level == 0

    def test_none_experience_candidate_draws_the_young_pool(self):
        """A candidate with UNKNOWN experience prices off the young pool,
        exactly like a rookie — ``(experience or 0)`` inherited verbatim
        from the reference model (``exp26 or 0``). Reference conformance
        is load-bearing, so this pin makes the implicit choice explicit
        rather than changing it."""
        model = KeeperModel(_young_pool_samples())
        unknown = model.option_values(
            "RB", experience=None, room_price=10.0, price=10.0
        )
        rookie = model.option_values("RB", experience=0, room_price=10.0, price=10.0)
        assert unknown == rookie
        assert unknown.pool_size == 25  # the young pool, not empty or veteran

    def test_end_to_end_snapshot_bucketing_is_detected(self):
        """Full pipeline: player x (snapshot years_exp=3) broke out in 2023
        when his as-of experience was 0. Correct bucketing puts his r1=3.0
        sample in the young pool (ov1=0.72); snapshot bucketing files him
        as a veteran and prices the young candidate's option at $0."""
        model = PriceModel(_flat_price_bids())
        rows_2025 = [_row(f"y{i}", "RB", 10.0, 1) for i in range(24)]
        seasons = {
            2023: _season([_row("x", "RB", 10.0, 3)]),
            2024: _season([_row("x", "RB", 5.0, 3)]),
            2025: _season(rows_2025),
            2026: _season([]),
        }
        samples = build_transition_samples(seasons, model, current_season=2026)
        keeper = KeeperModel(samples)
        options = keeper.option_values("RB", experience=1, room_price=10.0, price=10.0)
        assert options.pool_size == 25
        assert options.one_year == pytest.approx((30.0 - 12.0) / 25)

    def test_old_rb_uses_the_age_matched_pool_not_the_veteran_bucket(self):
        """An 8+ year RB prices off 6+ year backs (who crater, r1=0.2:
        option worthless), never the 3-8 bucket where mid-career backs
        holding value (r1=1.5) would inflate the option to ~$6."""
        mid_career = [
            TransitionSample("RB", 4, price=20.0, ratio_one=1.5, ratio_two=None)
        ] * 30
        old_backs = [
            TransitionSample("RB", 7, price=20.0, ratio_one=0.2, ratio_two=None)
        ] * 8
        model = KeeperModel(mid_career + old_backs)
        options = model.option_values("RB", experience=9, room_price=20.0, price=20.0)
        assert options.pool_size == 8
        assert options.one_year == 0.0

    def test_old_wr_is_not_age_matched(self):
        """The age-matched pool is an RB rule; an old WR stays in the 3-8
        veteran bucket (widened to any position at level 1)."""
        mid_career = [
            TransitionSample("RB", 4, price=20.0, ratio_one=1.5, ratio_two=None)
        ] * 30
        old_backs = [
            TransitionSample("RB", 7, price=20.0, ratio_one=0.2, ratio_two=None)
        ] * 8
        model = KeeperModel(mid_career + old_backs)
        options = model.option_values("WR", experience=9, room_price=20.0, price=20.0)
        assert options.pool_size == 38
        assert options.widen_level == 1

    def test_option_payoff_honors_the_five_dollar_keeper_floor(self):
        """Buying at $1 means keeping at $5, not $3: payoffs are 3.0 and 0,
        never 5.0 and 1.0."""
        pool = [
            TransitionSample("RB", 0, price=10.0, ratio_one=0.4, ratio_two=None)
        ] * 24 + [TransitionSample("RB", 0, price=10.0, ratio_one=0.8, ratio_two=None)]
        model = KeeperModel(pool)
        options = model.option_values("RB", experience=0, room_price=10.0, price=1.0)
        assert options.one_year == pytest.approx(3.0 / 25)

    def test_year_two_counts_only_paths_where_year_one_was_kept(self):
        """ov2 conditions on the year-1 keep being in the money but divides
        by ALL two-year samples: 12 paths pay max(0, 20-14)=6, the 13
        collapse-then-spike paths (r1=0.1, r2=5.0) contribute NOTHING even
        though v0*r2 - cost would be huge. Expected: 12*6/25 = 2.88."""
        keeps = [
            TransitionSample("QB", 1, price=10.0, ratio_one=2.0, ratio_two=2.0)
        ] * 12
        spikes = [
            TransitionSample("QB", 1, price=10.0, ratio_one=0.1, ratio_two=5.0)
        ] * 13
        model = KeeperModel(keeps + spikes)
        options = model.option_values("QB", experience=0, room_price=10.0, price=10.0)
        assert options.one_year == pytest.approx(12 * 8.0 / 25)
        assert options.two_year == pytest.approx(12 * 6.0 / 25)


class TestPremium:
    """premium = gamma*OV1 + gamma^2*OV2, truncated by the 2-keep cap."""

    def test_gamma_discounting(self):
        """premium = 0.8*OV1 + 0.64*OV2 at the decided gamma."""
        model = KeeperModel(_young_pool_samples())
        options = model.option_values("RB", experience=1, room_price=10.0, price=10.0)
        assert model.premium(options) == pytest.approx(0.8 * 0.72 + 0.64 * 0.36)

    def test_one_prior_keep_drops_the_year_two_term(self):
        """One keep left: only the one-year option survives."""
        model = KeeperModel(_young_pool_samples())
        options = model.option_values("RB", experience=1, room_price=10.0, price=10.0)
        assert model.premium(options, years_already_kept=1) == pytest.approx(0.8 * 0.72)

    def test_two_prior_keeps_exhaust_the_option(self):
        """Kept 2024 and 2025 means ineligible: the option is worth $0."""
        model = KeeperModel(_young_pool_samples())
        options = model.option_values("RB", experience=1, room_price=10.0, price=10.0)
        assert model.premium(options, years_already_kept=2) == 0.0
