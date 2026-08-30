"""Par at open: the engine's precondition, at production sheet depth.

Its own module because it is the only engine test that builds its sheet
from REAL ``compute_worths`` output rather than from hand-written worths.
That is the whole point of it: ``compute_worths`` normalizes the
above-floor total to ``teams * (budget - drafted slots)``, which is
exactly the money an untouched room holds, so opening inflation has to be
par — and has to stay par however deep the priced tail runs.
"""

from __future__ import annotations

import pytest

from draftbot import draft_engine
from draftbot.draft_engine import TAPER_ZERO_RANK, positional_inflation
from draftbot.valuation import FLOOR_PRICE, SeasonRow, compute_worths

from .helpers_engine import make_board, sheet_row


def _deep_season(depths):
    """A synthetic projections season: ``depths`` players per position on
    a strictly decreasing points curve, so every position has real value
    above any replacement rank shallower than its pool."""
    season = {}
    for position, depth in sorted(depths.items()):
        for index in range(1, depth + 1):
            player_id = f"{position.lower()}{index:03d}"
            season[player_id] = SeasonRow(
                player_id=player_id,
                position=position,
                adp=float(index),
                points=300.0 - 2.0 * index,
                years_exp_snapshot=3,
                name=player_id,
            )
    return season


def _production_sheet(config, bench_ranks, depths):
    """Sheet rows carrying REAL ``compute_worths`` output, ranked by value
    the way ``build_value_sheet`` ranks it.

    The point of going through ``compute_worths`` rather than hand-writing
    worths is that its normalization is the engine's stated precondition:
    the above-floor total IS ``teams * (budget - drafted slots)``. A hand
    fixture can be built to open at par by accident; this one opens at par
    only if the engine's arithmetic actually honours the precondition."""
    season = _deep_season(depths)
    league = (config.roster_slots, config.teams, config.auction_budget)
    worths = compute_worths(
        season, *league, bench_ranks=bench_ranks, starter_pct=config.starter_pct
    )
    ordered = sorted(season, key=lambda pid: (-worths[pid], pid))
    return [
        sheet_row(rank, pid, season[pid].position, worths[pid])
        for rank, pid in enumerate(ordered, start=1)
    ]


class TestParAtOpenOnADeepSheet:
    """The engine's stated precondition, exercised at production depth.

    ``compute_worths`` spreads exactly ``teams * (budget - drafted slots)``
    over VORP, so a full room facing an untouched board has exactly as many
    dollars as the sheet has above-floor value. Opening inflation must
    therefore be par — NOT because the priced tail happens to stop inside
    the taper window, but because the money side and the value side are the
    same quantity. Both depths below run the priced tail past
    ``TAPER_ZERO_RANK``, which is where a taper-weighted denominator (money
    untapered, value tapered) opens above par and marks every displayed
    dollar up for no market reason.
    """

    #: Position depths and bench baselines: the first pair is the shape the
    #: league's own draft history derives, the second is deliberately deeper
    #: so "par at open holds at any depth" is tested, not asserted.
    DEPTHS = {"QB": 60, "RB": 120, "WR": 140, "TE": 50}
    BENCH_CASES = (
        {"QB": 21, "RB": 55, "WR": 63, "TE": 18},
        {"QB": 35, "RB": 95, "WR": 110, "TE": 40},
    )

    @staticmethod
    def _full_room(config):
        """Twelve untouched full budgets: money = 12 x (200 - 15) = $2220,
        which is exactly what ``compute_worths`` normalized against."""
        return make_board(
            {slot: config.auction_budget for slot in range(1, config.teams + 1)},
            drafted_slots=config.drafted_slots,
        )

    @pytest.mark.parametrize("bench_ranks", BENCH_CASES)
    def test_the_priced_tail_really_runs_past_the_taper_window(
        self, config, bench_ranks
    ):
        """Fixture guard: without above-floor rows at or past
        ``TAPER_ZERO_RANK`` the par assertion below would pass on the
        defect too, so the depth is pinned here rather than assumed."""
        rows = _production_sheet(config, bench_ranks, self.DEPTHS)
        above = [row for row in rows if row.worth > FLOOR_PRICE]
        assert len(above) == sum(rank - 1 for rank in bench_ranks.values())
        assert max(row.rank for row in above) > TAPER_ZERO_RANK
        assert sum(1 for row in above if row.rank >= TAPER_ZERO_RANK) > 0

    @pytest.mark.parametrize("bench_ranks", BENCH_CASES)
    def test_an_untouched_board_opens_at_exactly_par(self, config, bench_ranks):
        """No sales, no keepers, full budgets: every valued position reads
        exactly 1.0."""
        rows = _production_sheet(config, bench_ranks, self.DEPTHS)
        inflation = positional_inflation(rows, self._full_room(config))
        for position in sorted(self.DEPTHS):
            assert inflation[position] == 1.0

    @pytest.mark.parametrize("bench_ranks", BENCH_CASES)
    def test_par_at_open_survives_the_clamps_being_opened(
        self, config, bench_ranks, monkeypatch
    ):
        """Anti-cheat for the floor: ``INFLATION_MIN`` is 1.0, so a ratio
        that came out BELOW par would also display as 1.0 and the assertion
        above would pass on a denominator that is merely too big. Re-drive
        the same board with both clamps opened and the raw ratio has to be
        par from both sides."""
        monkeypatch.setattr(draft_engine, "INFLATION_MIN", -1e9)
        monkeypatch.setattr(draft_engine, "INFLATION_MAX", 1e9)
        rows = _production_sheet(config, bench_ranks, self.DEPTHS)
        inflation = positional_inflation(rows, self._full_room(config))
        for position in sorted(self.DEPTHS):
            assert inflation[position] == pytest.approx(1.0, abs=1e-9)

    def test_a_tapered_denominator_would_open_above_par(self, config, monkeypatch):
        """The defect, stated as a property rather than a digit: taper the
        POOL while leaving the room's money untapered and the same board
        opens above par with no keepers and no sales. This is the mutant
        the two tests above kill, pinned here so a future edit that quietly
        reinstates it fails with a message about the taper rather than
        about a number."""
        monkeypatch.setattr(draft_engine, "INFLATION_MIN", -1e9)
        monkeypatch.setattr(
            draft_engine,
            "_discretionary",
            lambda row: draft_engine.taper_weight(row.rank)
            * max(0.0, row.worth - FLOOR_PRICE),
        )
        rows = _production_sheet(config, self.BENCH_CASES[0], self.DEPTHS)
        inflation = positional_inflation(rows, self._full_room(config))
        assert inflation["RB"] > 1.0
