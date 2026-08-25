"""Shared builder for override-sheet fixtures.

Plain builder imported explicitly (the ``helpers_engine`` pattern). One
place for the row's defaults so the engine tests and the dashboard tests
cannot drift into disagreeing about what "no opinion" means -- which is
the one shape both suites assert must leave every number alone.
"""

from __future__ import annotations

from draftbot.overrides import PlayerOverride

#: Every column blank: a row that exists and says nothing.
BLANK_ROW = {
    "name": None,
    "tier": None,
    "target": False,
    "avoid": False,
    "delta": 0,
    "note": None,
}


def override_book(player_id: str, **fields) -> dict[str, PlayerOverride]:
    """A one-row book for ``player_id``, blank in every column the caller
    does not name."""
    return {player_id: PlayerOverride(player_id=player_id, **{**BLANK_ROW, **fields})}
