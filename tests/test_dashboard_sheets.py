"""Sheet inputs for the dashboard: the replay-derived demo sheet and the
value-sheet CSV reader."""

from __future__ import annotations

from draftbot.dashboard.sheets import read_sheet_csv, replay_sheet
from draftbot.valuesheet import write_csv

from .conftest import raw_auction_pick
from .helpers_engine import sheet_row


def test_replay_sheet_prices_every_sold_player_at_the_hammer_price():
    """The demo sheet derives from the replay itself: each player's worth
    IS the price the room actually paid, ranked richest first with player-id
    tie-breaks, keeper premium zero (the 2025 fixture has no keepers)."""
    picks = [
        raw_auction_pick(1, "A", 1, "51", "WR"),
        raw_auction_pick(2, "B", 2, "7", "RB"),
        raw_auction_pick(3, "C", 3, "51", "QB"),
        raw_auction_pick(4, "D", 4, "1", "TE"),
    ]
    rows = replay_sheet(picks)

    assert [row.player_id for row in rows] == ["A", "C", "B", "D"]
    assert [row.rank for row in rows] == [1, 2, 3, 4]
    by_id = {row.player_id: row for row in rows}
    assert by_id["A"].worth == 51.0
    assert by_id["D"].worth == 1.0
    for row in rows:
        assert row.value == row.worth  # value = worth + a zero premium
        assert row.keeper_premium == 0.0
        assert row.room_price == row.worth
        assert row.price_source == "replay"
    assert by_id["B"].position == "RB"


def test_read_sheet_csv_round_trips_the_valuesheet_writer(tmp_path):
    """The reader consumes exactly what ``python -m draftbot.valuesheet``
    writes: numbers come back as numbers, blank ADP/points come back as
    None, and row order is preserved."""
    rows = [
        sheet_row(1, "A", "WR", 51.0, premium=3.5, name="Amon-Ra St. Brown"),
        sheet_row(2, "B", "RB", 7.0),
    ]
    path = tmp_path / "sheet.csv"
    write_csv(rows, path)

    loaded = read_sheet_csv(path)

    assert [row.player_id for row in loaded] == ["A", "B"]
    first = loaded[0]
    assert first.rank == 1
    assert first.name == "Amon-Ra St. Brown"
    assert first.position == "WR"
    assert first.worth == 51.0
    assert first.keeper_premium == 3.5
    assert first.value == 54.5
    assert first.price_source == "band"
    assert loaded[1].points is None  # blank cell, not 0.0
    assert first.adp == 1.0  # helper mirrors rank into ADP
