"""Shared hand-fixture builders for the valuation test modules.

Lives outside conftest.py on purpose: these are plain builders imported
explicitly by the tests that use them, not fixtures injected everywhere.
"""

from __future__ import annotations

from draftbot.config import LeagueConfig


def projection_row(player_id, position, adp, pts=100.0, years_exp=3, name="A Player"):
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


def league_config(**overrides):
    """A minimal LeagueConfig for model-fit tests (tiny two-team league);
    valuation knobs default to the decided values unless overridden.

    Shared rather than copied: two test modules need the same tiny league,
    and a second copy is a second thing to forget when a required field is
    added to LeagueConfig."""
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
