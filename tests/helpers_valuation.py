"""Shared hand-fixture builders for the valuation test modules.

Lives outside conftest.py on purpose: these are plain builders imported
explicitly by the tests that use them, not fixtures injected everywhere.
"""

from __future__ import annotations


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
