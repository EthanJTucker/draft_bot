"""Dynamic repricing: a value sheet plus draft state in, a max bid out.

The engine layers four adjustments (decided in GAMEPLAN.md and PRD.md)
on top of the static value sheet, recomputed from scratch on every call:

- **Positional inflation** — remaining room money over remaining sheet
  value, per position, with off-model sales excluded and the multiplier
  tapered away from the cheap tail (measured keeper-league inflation
  concentrates above roughly the 130th player).
- **Marginal roster need** — best legal lineup with the player minus
  without, honoring FLEX eligibility; a redundant player prices at bench
  retention, never at starter value.
- **Spend-down schedule** — a bargain-early margin that rises as my
  money-per-open-slot outpaces the room's, plus a boost that burns money
  the remaining pool can no longer absorb. Never finish with cash.
- **Gap-based tiers** — per position, with remaining-in-tier counts and
  a last-of-tier flag on the final remaining member.

Everything here is a pure function of (value sheet, board state, config):
no stored state, no clock, no network — so the replay backtest (issue #6)
can ask for the engine's number at the moment of any historical sale, and
the dashboard (issue #7) only renders the returned analysis record.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from draftbot.valuation import FLOOR_PRICE, SheetRow

#: A tier break needs a value gap of at least this many dollars...
TIER_ABS_GAP = 2.0

#: ...and at least this fraction of the richer neighbor's value, so the
#: $8 gaps that separate top tiers are required at the top of the board
#: while $2 still splits the mid-board.
TIER_REL_GAP = 0.12

#: Round to this many decimals before comparing/ranking (determinism).
_QUANTIZE_DECIMALS = 10


def _quantize(value: float) -> float:
    return round(value, _QUANTIZE_DECIMALS)


@dataclass(frozen=True)
class TierStatus:
    """Where one player sits in his position's tier structure."""

    tier: int  # 1-based tier index within the position
    size: int  # players the tier started with
    remaining: int  # tier members not yet sold (the player included)
    last_of_tier: bool  # the player is the FINAL remaining member


def tier_status(
    player_id: str,
    tiers: dict[str, tuple[tuple[str, ...], ...]],
    sold: AbstractSet[str],
) -> TierStatus | None:
    """The player's tier, remaining-in-tier count, and last-of-tier flag.

    ``last_of_tier`` fires exactly when the player is available and every
    OTHER member of his tier has sold — never one early (a tier-mate
    still on the board) and never one late (the player himself sold).
    """
    for position in sorted(tiers):
        for index, members in enumerate(tiers[position], start=1):
            if player_id not in members:
                continue
            remaining = sum(1 for member in members if member not in sold)
            return TierStatus(
                tier=index,
                size=len(members),
                remaining=remaining,
                last_of_tier=player_id not in sold and remaining == 1,
            )
    return None


def build_tiers(
    rows: list[SheetRow] | tuple[SheetRow, ...],
) -> dict[str, tuple[tuple[str, ...], ...]]:
    """Gap-based tiers per position: player ids in value order, split
    where the drop to the next player is at least ``TIER_ABS_GAP`` dollars
    AND ``TIER_REL_GAP`` of the richer value."""
    by_position: dict[str, list[SheetRow]] = {}
    for row in rows:
        by_position.setdefault(row.position, []).append(row)
    tiers: dict[str, tuple[tuple[str, ...], ...]] = {}
    for position in sorted(by_position):
        members = sorted(
            by_position[position], key=lambda row: (-row.value, row.player_id)
        )
        grouped: list[list[str]] = [[members[0].player_id]]
        for prev, row in zip(members, members[1:]):
            gap = _quantize(prev.value - row.value)
            threshold = max(TIER_ABS_GAP, TIER_REL_GAP * prev.value)
            if gap >= _quantize(threshold):
                grouped.append([])
            grouped[-1].append(row.player_id)
        tiers[position] = tuple(tuple(group) for group in grouped)
    return tiers
