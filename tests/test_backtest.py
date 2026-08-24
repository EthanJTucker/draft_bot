"""Unit behavior of the backtest statistics and the history-fit sheet.

Every fixture here is an anti-cheat: inputs chosen so that the trivially
wrong implementation (absolute error where signed belongs, an off-by-one
segment split, an off-model lot silently included, a fit that peeks at
the 2025 amounts) returns a detectably wrong number. The full-replay
integration gates live in ``test_backtest_replay.py``.
"""

from __future__ import annotations

import copy
import json

from draftbot.backtest import (
    BacktestRecord,
    build_history_price_sheet,
    drift_spread,
    error_stats,
    overall_stats,
    position_stats,
    segment_stats,
)

from .conftest import REPO_ROOT

HISTORY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "league_history.json"


def record(
    lot: int,
    running: float,
    actual: int,
    position: str = "WR",
    off_model: bool = False,
) -> BacktestRecord:
    """A minimal backtest record: only the fields under test vary."""
    return BacktestRecord(
        lot=lot,
        player_id=f"p{lot:03d}",
        name=f"Player {lot}",
        position=None if off_model else position,
        actual=actual,
        running=running,
        static=running,
        max_bid=int(running),
        inflation=1.0,
        off_model=off_model,
    )


def test_mae_and_bias_disagree_on_symmetric_misses():
    """A +5 miss and a -5 miss: MAE is 5, bias is 0 (error = estimate -
    actual, bias its mean, MAE the mean absolute). An implementation that
    confuses signed with absolute error collapses the two."""
    records = [record(1, running=25.0, actual=20), record(2, 15.0, 20)]
    stats = error_stats(records, "running")
    assert stats.n == 2
    assert stats.mae == 5.0
    assert stats.bias == 0.0


def test_an_off_model_whale_does_not_move_the_overall_stats():
    """The same two scored lots, with and without an off-model lot
    carrying a $99 error: overall stats must be identical, and n must
    count only the scored lots. Silently including off-model lots moves
    MAE by tens of dollars on this fixture."""
    scored = [record(1, running=25.0, actual=20), record(2, 15.0, 20)]
    with_whale = [*scored, record(3, running=1.0, actual=100, off_model=True)]
    assert overall_stats(with_whale, "running") == overall_stats(scored, "running")
    assert overall_stats(with_whale, "running").n == 2


class TestSegmentSplit:
    """180 lots split into thirds by sale order: 1-60, 61-120, 121-180."""

    def test_lot_60_is_early_and_lot_61_is_mid(self):
        """Errors placed exactly on the boundary lots: +6 on lot 60, -6 on
        lot 61, zero everywhere else. The correct split puts all the
        positive bias early and all the negative bias mid; any off-by-one
        split drags both onto the wrong side."""
        records = [
            record(lot, running=20.0 + {60: 6.0, 61: -6.0}.get(lot, 0.0), actual=20)
            for lot in range(1, 181)
        ]
        early, mid, late = segment_stats(records, "running")
        assert (early.label, early.first_lot, early.last_lot) == ("early", 1, 60)
        assert (mid.label, mid.first_lot, mid.last_lot) == ("mid", 61, 120)
        assert (late.label, late.first_lot, late.last_lot) == ("late", 121, 180)
        assert (early.stats.n, mid.stats.n, late.stats.n) == (60, 60, 60)
        assert early.stats.bias == 0.1
        assert mid.stats.bias == -0.1
        assert late.stats.bias == 0.0

    def test_segments_exclude_off_model_lots_from_their_stats(self):
        """An off-model lot inside a segment counts toward the lot span
        but never toward the segment's n, MAE, or bias."""
        records = [
            record(lot, running=23.0, actual=20, off_model=lot == 2)
            for lot in range(1, 7)
        ]
        early, mid, late = segment_stats(records, "running")
        assert (early.first_lot, early.last_lot) == (1, 2)
        assert (early.stats.n, mid.stats.n, late.stats.n) == (1, 2, 2)
        assert early.stats.bias == 3.0


def test_positions_are_scored_separately():
    """Per-position MAE/bias, keyed and sorted by position label: an RB
    overestimate next to a WR underestimate would cancel to zero bias
    merged; split, each keeps its sign."""
    records = [
        record(1, running=30.0, actual=20, position="RB"),
        record(2, running=10.0, actual=20, position="WR"),
        record(3, running=1.0, actual=50, off_model=True),
    ]
    by_position = position_stats(records, "running")
    assert list(by_position) == ["RB", "WR"]
    assert by_position["RB"] == error_stats([records[0]], "running")
    assert by_position["RB"].bias == 10.0
    assert by_position["WR"].bias == -10.0


def test_drift_spread_is_the_bias_range_across_segments():
    """The drift number the gate bounds: max minus min segment bias."""
    records = [
        record(lot, running=20.0 + {60: 6.0, 61: -6.0}.get(lot, 0.0), actual=20)
        for lot in range(1, 181)
    ]
    assert drift_spread(segment_stats(records, "running")) == 0.2


class TestHistorySheet:
    """The backtest sheet: 2025 ADP priced by the 2023-2024 fit ONLY."""

    def test_2025_amounts_never_reach_the_price_fit(self, config):
        """The leakage anti-cheat: corrupting every 2025 winning bid to
        $99 must leave the sheet byte-identical (the fit uses 2023-2024
        bids only), while corrupting the 2024 bids the same way must move
        it — the replayed season's answers stay out of the estimate."""
        history = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))
        baseline = build_history_price_sheet(history, config, season=2025)

        leaked = copy.deepcopy(history)
        leaked["picks"]["2025"] = [
            [player_id, position, 99, slot]
            for player_id, position, _, slot in leaked["picks"]["2025"]
        ]
        assert build_history_price_sheet(leaked, config, season=2025) == baseline

        refit = copy.deepcopy(history)
        refit["picks"]["2024"] = [
            [player_id, position, 99, slot]
            for player_id, position, _, slot in refit["picks"]["2024"]
        ]
        assert build_history_price_sheet(refit, config, season=2025) != baseline

    def test_sheet_rows_are_ranked_prices_for_the_2025_pool(self, config):
        """Every row is a player with a 2025 ADP; ranks are 1..n in price
        order (ties by player id); worth, room price, and value coincide
        (keeper premium deliberately zero: the empirical fit already
        contains the room's keeper appetite, and doubling it would tilt
        the market estimate)."""
        history = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))
        rows = build_history_price_sheet(history, config, season=2025)
        assert rows, "the committed fixture prices a non-empty 2025 pool"
        for index, row in enumerate(rows):
            assert row.rank == index + 1
            assert history["players"][row.player_id]["adp"].get("2025") == row.adp
            assert row.keeper_premium == 0.0
            assert row.worth == row.room_price == row.value
        ordering = [(-row.worth, row.player_id) for row in rows]
        assert ordering == sorted(ordering)
