"""The verified spot checks and sanity bands, on the league's real history.

``tests/fixtures/league_history.json`` is a small committed excerpt of the
cached Sleeper data (picks feeds 2023-2025 + projections 2023-2026, snapshot
2026-08-23) that reproduces the full-cache pipeline exactly (verified at
fixture-build time: identical bids, identical 359 transition samples,
identical prices and option values). Exact counts are pins against THIS
snapshot and move if the fixture is regenerated; the dollar assertions are
loose bands meant to survive a refit on fresh data.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from draftbot.models import parse_picks
from draftbot.valuation import (
    KeeperModel,
    PriceModel,
    build_bids,
    build_transition_samples,
    parse_projections,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "league_history.json"

#: 2026 player ids used as spot-check anchors (from the fixture snapshot).
STAFFORD = "421"  # Matthew Stafford, the deep-QB anchor from GAMEPLAN.md
JAMES_COOK = "8138"  # top-RB anchor whose ADP band holds 6+ real bids


def _rebuild_seasons(fixture):
    """Reconstruct raw projections rows so the REAL parsers run end to end.

    Every season file repeats the same current-snapshot years_exp, exactly
    as the live API serves it — as-of-season experience must come from the
    pipeline, never from the fixture.
    """
    seasons = {}
    for year in (2023, 2024, 2025, 2026):
        rows = []
        for player_id, entry in fixture["players"].items():
            adp = entry["adp"].get(str(year))
            if adp is None:
                continue
            rows.append(
                {
                    "player_id": player_id,
                    "stats": {"adp_half_ppr": adp, "pts_half_ppr": None},
                    "player": {
                        "position": entry["pos"],
                        "years_exp": entry["exp"],
                    },
                }
            )
        seasons[year] = parse_projections(rows)
    return seasons


def _rebuild_picks(fixture):
    return {
        int(year): parse_picks(
            [
                {
                    "player_id": player_id,
                    "draft_slot": 1,
                    "metadata": {"amount": str(amount), "position": position},
                }
                for player_id, position, amount in feed
            ]
        )
        for year, feed in fixture["picks"].items()
    }


@pytest.fixture(scope="module", name="history")
def history_fixture():
    """The full valuation pipeline run once over the committed excerpt."""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    seasons = _rebuild_seasons(fixture)
    picks = _rebuild_picks(fixture)
    bids = build_bids(picks, seasons)
    prices = PriceModel(bids)
    samples = build_transition_samples(
        seasons, prices, current_season=fixture["current_season"]
    )
    return SimpleNamespace(
        seasons=seasons,
        bids=bids,
        prices=prices,
        samples=samples,
        keeper=KeeperModel(samples),
    )


class TestBidHistory:
    """The bid extraction over the real three-year history."""

    def test_qualifying_bid_count(self, history):
        """504 picks minus K/DEF and no-ADP players leaves 434 bids."""
        assert len(history.bids) == 434

    def test_position_mix(self, history):
        """Per-position counts pin the join against the right season files."""
        by_position = {}
        for bid in history.bids:
            by_position[bid.position] = by_position.get(bid.position, 0) + 1
        assert by_position == {"QB": 59, "RB": 151, "WR": 175, "TE": 49}


class TestVerifiedSpotChecks:
    """The two extremes where the raw curve is known wrong (GAMEPLAN.md)."""

    def test_deep_qb_prices_at_the_band_median_not_the_curve(self, history):
        """The room pays $2-3 for a deep QB; the curve says ~$6.8. The
        band (30+ real bids) must win."""
        adp = history.seasons[2026][STAFFORD].adp
        assert 100 <= adp <= 130  # anchor guard: Stafford is a deep QB
        room = history.prices.room_price("QB", adp)
        curve = history.prices.curve_price("QB", adp)
        assert 2.0 <= room <= 3.0
        assert 6.0 <= curve <= 8.0
        assert len(history.prices.band_amounts("QB", adp)) >= 20

    def test_top_rb_prices_off_the_leagues_actual_top_bids(self, history):
        """The top-RB band holds the room's real top prices ($55 and $57
        McCaffrey/Henry 2025); the band median prices a top RB in that
        cluster's range, off the bids rather than the curve."""
        band = history.prices.band_amounts("RB", 7.5)
        assert len(band) >= 6
        assert {55, 57} <= set(band)
        adp = history.seasons[2026][JAMES_COOK].adp
        assert 7.0 <= adp <= 10.0  # anchor guard: a top-5 RB by 2026 ADP
        room = history.prices.room_price("RB", adp)
        curve = history.prices.curve_price("RB", adp)
        assert 47.0 <= room <= 57.0
        assert len(history.prices.band_amounts("RB", adp)) >= 6
        assert abs(room - curve) > 1.0  # the curve did not sneak through


class TestTransitionPopulation:
    """The real transition-sample population, as-of-season bucketed."""

    def test_sample_and_two_year_counts(self, history):
        """359 samples, 239 with a year-two ratio (this snapshot's pins)."""
        assert len(history.samples) == 359
        assert sum(1 for s in history.samples if s.ratio_two is not None) == 239

    def test_rookie_season_samples_exist_only_under_as_of_bucketing(self, history):
        """39 samples come from seasons when the player was a rookie.
        Snapshot bucketing produces ZERO experience-0 samples on this data
        (2026 rookies have no historical seasons), so a healthy rookie
        bucket is direct evidence the as-of-season fix is live."""
        rookie_samples = [s for s in history.samples if s.experience == 0]
        assert len(rookie_samples) >= 30

    def test_no_cheap_lots(self, history):
        """Every sample cleared the $3 noise filter."""
        assert all(s.price >= 3.0 for s in history.samples)


class TestOptionValueSanityBands:
    """Measured one-year option values (GAMEPLAN.md): cheap young ~$5-7,
    veterans ~$1-3, market-priced stars ~$0. Bands are loose to survive
    refits on fresh snapshots."""

    def test_cheap_young_player_option_is_worth_several_dollars(self, history):
        """A $5 young RB with an $8 room price: ov1 lands ~$5-7."""
        options = history.keeper.option_values(
            "RB", experience=1, room_price=8.0, price=5.0
        )
        assert 4.0 <= options.one_year <= 8.0

    def test_young_qb_at_market_still_carries_upside(self, history):
        """The Jayden Daniels archetype (QB, 2 years, ~$8)."""
        options = history.keeper.option_values(
            "QB", experience=2, room_price=8.0, price=8.0
        )
        assert 3.0 <= options.one_year <= 8.0

    def test_veteran_option_is_worth_a_dollar_or_three(self, history):
        """Jalen Hurts / Tony Pollard archetypes: vets keep a sliver."""
        for position, experience, price in (("QB", 6, 10.0), ("RB", 7, 12.0)):
            options = history.keeper.option_values(
                position, experience, room_price=price, price=price
            )
            assert 0.5 <= options.one_year <= 3.5, (position, experience)

    def test_market_priced_star_option_is_near_zero(self, history):
        """A $45 veteran WR bought at market: the keep-again option adds
        almost nothing relative to his price (~$0 in GAMEPLAN's terms)."""
        options = history.keeper.option_values(
            "WR", experience=6, room_price=45.3, price=45.3
        )
        premium = history.keeper.premium(options)
        assert options.one_year <= 3.5
        assert premium / 45.3 <= 0.08

    def test_option_value_falls_from_young_to_vet_to_star(self, history):
        """The economics in one line: youth upside > veteran sliver >
        star-at-market."""
        young = history.keeper.option_values(
            "RB", experience=1, room_price=8.0, price=5.0
        )
        vet = history.keeper.option_values(
            "RB", experience=7, room_price=12.0, price=12.0
        )
        star = history.keeper.option_values(
            "WR", experience=6, room_price=45.3, price=45.3
        )
        assert young.one_year > vet.one_year > 0.0
        assert vet.one_year > star.one_year / 2  # star premium stays small
        assert star.one_year / 45.3 < vet.one_year / 12.0

    def test_old_rb_prices_off_a_small_age_matched_pool(self, history):
        """A 9-year RB (the McCaffrey keeper case) draws a small 6+ year
        pool at level 0 instead of the broad veteran bucket."""
        options = history.keeper.option_values(
            "RB", experience=9, room_price=59.9, price=59.9
        )
        assert options.widen_level == 0
        assert 8 <= options.pool_size <= 40
        assert 0.0 <= options.one_year <= 5.0
