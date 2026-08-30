"""Par at open: the engine's precondition, at production sheet depth.

Its own module because it is the only engine test that builds its sheet
from REAL ``compute_worths`` output rather than from hand-written worths.
That is the whole point of it: ``compute_worths`` normalizes the
above-floor total to ``teams * (budget - drafted slots)``, which is
exactly the money an untouched room holds, so opening inflation has to be
par — and has to stay par however deep the priced tail runs.

Two layers, because the precondition spans two functions.
``TestParAtOpenOnADeepSheet`` drives ``compute_worths`` at depth, which is
where the arithmetic lives. ``TestParAtOpenThroughTheProductionSheet``
drives ``build_value_sheet``, which is what actually ships rows: par is
the room's money over the EMITTED sheet's above-floor total, so a sheet
that prices a player above the floor and then declines to emit his row
breaks par no matter how right the arithmetic upstream was.
"""

from __future__ import annotations

import pytest

from draftbot import draft_engine
from draftbot.draft_engine import TAPER_ZERO_RANK, positional_inflation
from draftbot.models import parse_picks
from draftbot.valuation import (
    FLOOR_PRICE,
    SeasonRow,
    build_value_sheet,
    compute_worths,
    parse_projections,
)

from .helpers_engine import make_board, sheet_row
from .helpers_valuation import league_config, projection_row


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


def _qb_picks(year):
    """One season's picks feed: four QBs at $5, so the derived positional
    mix is 100% QB and the bench baseline lands where the slot target
    says rather than where a mixed history would put it."""
    return parse_picks(
        [
            {
                "player_id": f"{year}-qb-{index}",
                "draft_slot": 1,
                "is_keeper": None,
                "metadata": {"amount": "5", "position": "QB"},
            }
            for index in range(4)
        ]
    )


#: Two teams, one QB and one bench slot, $10 each: $16 of discretionary
#: money, which is what ``compute_worths`` normalizes the sheet against.
#: ``bench_skill_slots`` pins the derived bench baseline at QB5.
_TINY_LEAGUE = {"auction_budget": 10, "bench_skill_slots": 5}

#: Five QBs, none with a usable ADP, on a curve that goes NEGATIVE past
#: the third. Starter replacement is QB3 (20 pts) and the bench baseline
#: is QB5 (-60 pts), so "d" carries 50 points of bench VORP and real
#: dollars while his projection is -10 — neither a valid ADP nor a
#: positive projection, which are the only two things the draftability
#: filter used to accept.
_NEGATIVE_TAIL_POINTS = {"a": 100.0, "b": 60.0, "c": 20.0, "d": -10.0, "e": -60.0}


class TestParAtOpenThroughTheProductionSheet:
    """Par at open, driven through ``build_value_sheet`` rather than
    stopping at ``compute_worths``.

    ``compute_worths`` spreading the room's money over VORP is only half
    the precondition. The other half is that every above-floor dollar it
    priced actually reaches the emitted sheet, because par at open is the
    room's money over the EMITTED above-floor total. ``build_value_sheet``
    drops rows that are neither ADP-listed nor positively projected, and
    ``_replacement_points`` falls back to the worst player in the pool, so
    a player with points between a negative bench replacement and zero can
    hold real dollars and still be dropped — which silently shrinks the
    denominator and opens the whole position above par.

    Unreachable on the live feed (the real 2026 sheet opens at exactly
    1.0), so this fixture manufactures the shape on purpose.
    """

    @staticmethod
    def _world():
        """Seasons and picks for ``build_value_sheet``: two empty prior
        seasons carrying the QB-only picks history, and the 2026 board."""
        seasons = {
            2024: parse_projections([]),
            2025: parse_projections([]),
            2026: parse_projections(
                [
                    projection_row(pid, "QB", None, pts=points)
                    for pid, points in sorted(_NEGATIVE_TAIL_POINTS.items())
                ]
            ),
        }
        return seasons, {year: _qb_picks(year) for year in (2024, 2025)}

    @classmethod
    def _rows(cls, **overrides):
        config = league_config(**{**_TINY_LEAGUE, **overrides})
        return build_value_sheet(*cls._world(), config), config

    def test_the_fixture_really_prices_a_row_the_old_filter_would_drop(self):
        """Fixture guard, stated against ``compute_worths`` directly so it
        cannot be satisfied by the sheet it is guarding: "d" is worth more
        than the floor, and he has neither a usable ADP nor a positive
        projection. Without him the assertion below would pass on any
        filter at all."""
        seasons, picks = self._world()
        config = league_config(**_TINY_LEAGUE)
        worths = compute_worths(
            seasons[2026],
            config.roster_slots,
            config.teams,
            config.auction_budget,
            bench_ranks={"QB": 5, "RB": 1, "WR": 1, "TE": 1},
            starter_pct=config.starter_pct,
        )
        assert worths["d"] > FLOOR_PRICE
        assert seasons[2026]["d"].adp is None
        assert seasons[2026]["d"].points < 0
        assert picks[2024]  # the bench baseline is derived, not defaulted

    def test_every_above_floor_dollar_reaches_the_emitted_sheet(self):
        """Conservation on the SHEET, which is the quantity par divides
        by: the emitted above-floor total has to be the whole $16 the
        room holds, not $15.02 with "d" quietly missing."""
        rows, config = self._rows()
        emitted = sum(
            row.worth - FLOOR_PRICE for row in rows if row.worth > FLOOR_PRICE
        )
        money = config.teams * (config.auction_budget - config.drafted_slots)
        assert emitted == pytest.approx(float(money))
        assert "d" in {row.player_id for row in rows}

    def test_an_untouched_board_opens_at_exactly_par(self, monkeypatch):
        """The invariant itself, end to end and with both clamps opened so
        a below-par reading cannot hide behind ``INFLATION_MIN``. Dropping
        "d" reads 1.0649 here."""
        monkeypatch.setattr(draft_engine, "INFLATION_MIN", -1e9)
        monkeypatch.setattr(draft_engine, "INFLATION_MAX", 1e9)
        rows, config = self._rows()
        board = make_board(
            {slot: config.auction_budget for slot in range(1, config.teams + 1)},
            drafted_slots=config.drafted_slots,
        )
        assert positional_inflation(rows, board)["QB"] == pytest.approx(1.0, abs=1e-9)
