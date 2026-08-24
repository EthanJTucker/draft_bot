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

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from draftbot.tracker import FLEX_ELIGIBLE, BoardState
from draftbot.valuation import FLOOR_PRICE, SheetRow

#: Inflation applies in full down to this sheet rank...
TAPER_FULL_RANK = 110

#: ...and not at all from this rank on, fading linearly between (measured
#: keeper-league inflation concentrates above roughly the 130th player;
#: the $1-5 tail trades at floor prices whatever the room's money does).
TAPER_ZERO_RANK = 150

#: Sanity clamp on the per-position ratio: a depleted denominator late in
#: the draft must not emit an absurd multiplier.
INFLATION_MIN = 0.25
INFLATION_MAX = 3.0

#: The spend-down margin at money parity: max bids deliberately start
#: this fraction of the way to value (bargain early)...
MARGIN_BASE = 0.93

#: ...and rise this much for every unit my money-per-open-slot climbs
#: past the room's (spend down late). No upper clamp: late-draft riches
#: must be spendable, and the team max bid already caps every number.
MARGIN_SLOPE = 0.5

#: The margin never drops below this: a broke stack still bids near value
#: on the players it actually needs.
MARGIN_MIN = 0.85

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


def taper_weight(rank: int | None) -> float:
    """How much of the inflation multiplier reaches this sheet rank:
    1.0 through ``TAPER_FULL_RANK``, 0.0 from ``TAPER_ZERO_RANK`` on,
    linear between. An unranked (off-sheet) player never inflates."""
    if rank is None:
        return 0.0
    if rank <= TAPER_FULL_RANK:
        return 1.0
    if rank >= TAPER_ZERO_RANK:
        return 0.0
    return _quantize((TAPER_ZERO_RANK - rank) / (TAPER_ZERO_RANK - TAPER_FULL_RANK))


def _keeper_ids(keepers_by_slot: Mapping[int, Sequence[str]] | None) -> frozenset[str]:
    return frozenset(
        player_id
        for slot in sorted(keepers_by_slot or {})
        for player_id in (keepers_by_slot or {})[slot]
    )


def _discretionary(row: SheetRow) -> float:
    """The taper-weighted dollars above the $1 floor that room money can
    actually move. Worth basis: the sheet's worths are what the room's
    dollars were normalized against; the keeper premium is next season's
    money and stays out of the ratio."""
    return _quantize(taper_weight(row.rank) * max(0.0, row.worth - FLOOR_PRICE))


def positional_inflation(
    rows: Sequence[SheetRow],
    board: BoardState,
    keepers_by_slot: Mapping[int, Sequence[str]] | None = None,
) -> dict[str, float]:
    """Remaining room money over remaining sheet value, per position.

    Each position is budgeted its share of the room's initial
    discretionary money (actual keeper-reduced budgets, never the sheet's
    normalization room) in proportion to its share of the initial
    taper-weighted pool; every on-model sale then debits that position's
    money by the dollars paid above the floor while its pool loses the
    player's sheet value. Off-model sales — flagged by the board or
    simply absent from the sheet — touch neither side of any ratio, and
    kept players were never part of the buyable pool at all.
    """
    keepers = _keeper_ids(keepers_by_slot)
    pool = sorted(
        (row for row in rows if row.player_id not in keepers),
        key=lambda row: row.player_id,
    )
    sold = {sale.player_id for sale in board.sales}
    off_model = set(board.off_model_player_ids)
    rows_by_id = {row.player_id: row for row in pool}

    initial: dict[str, float] = {}
    remaining: dict[str, float] = {}
    for row in pool:
        disc = _discretionary(row)
        initial[row.position] = initial.get(row.position, 0.0) + disc
        if row.player_id not in sold:
            remaining[row.position] = remaining.get(row.position, 0.0) + disc
    total_initial = sum(initial[position] for position in sorted(initial))

    # Initial discretionary money: each team's actual budget minus the $1
    # its starting open slots pin. open_slots + purchase_count restores
    # the pre-sale slot count, so the figure is constant through the draft
    # and never moves when a sale (off-model included) debits a budget.
    money = sum(
        team.budget - (team.open_slots + team.purchase_count) for team in board.teams
    )

    spent: dict[str, float] = {}
    for sale in board.sales:
        row = rows_by_id.get(sale.player_id)
        if row is None or sale.player_id in off_model:
            continue
        spent[row.position] = spent.get(row.position, 0.0) + max(
            0.0, (sale.amount or 0) - FLOOR_PRICE
        )

    inflation: dict[str, float] = {}
    for position in sorted(initial):
        left = remaining.get(position, 0.0)
        if left <= 0 or total_initial <= 0:
            inflation[position] = 1.0
            continue
        budgeted = money * initial[position] / total_initial
        ratio = (budgeted - spent.get(position, 0.0)) / left
        inflation[position] = _quantize(max(INFLATION_MIN, min(INFLATION_MAX, ratio)))
    return inflation


def inflation_adjusted_price(row: SheetRow, inflation: float) -> float:
    """One row's price under its position's inflation ratio: the $1 floor
    never scales, the worth above it scales by the taper-weighted ratio
    (so the $1-5 tail holds its price whatever the top of the board
    does), and the keeper premium — next season's money — rides through
    untouched in both directions."""
    discretionary = max(0.0, row.worth - FLOOR_PRICE)
    scaled = discretionary * (1.0 + taper_weight(row.rank) * (inflation - 1.0))
    return _quantize(FLOOR_PRICE + scaled + row.keeper_premium)


def spend_schedule(
    board: BoardState,
    my_slot: int,
    rows: Sequence[SheetRow],
    inflation: Mapping[str, float],
    keepers_by_slot: Mapping[int, Sequence[str]] | None = None,
) -> tuple[float, float]:
    """The (margin, boost) pair of the bargain-early/spend-down-late
    schedule.

    The margin multiplies every bid: ``MARGIN_BASE`` at money parity,
    rising by ``MARGIN_SLOPE`` per unit of my money-per-open-slot over
    the rest of the room's, floored at ``MARGIN_MIN``. The boost is the
    money the remaining pool can no longer absorb — my remaining dollars
    minus the top ``open_slots`` remaining inflation-adjusted prices —
    spread over my open slots and added to every bid, so a surplus gets
    burned instead of stranded. A full roster returns (0, 0).
    """
    me = board.team(my_slot)
    if me.open_slots <= 0:
        return (0.0, 0.0)
    others = [team for team in board.teams if team.slot != my_slot]
    others_open = sum(team.open_slots for team in others)
    my_rate = me.remaining / me.open_slots
    pool_rate = 1.0
    if others_open > 0:
        pool_rate = max(1.0, sum(team.remaining for team in others) / others_open)
    margin = max(MARGIN_MIN, MARGIN_BASE + MARGIN_SLOPE * (my_rate / pool_rate - 1.0))

    unavailable = {sale.player_id for sale in board.sales} | _keeper_ids(
        keepers_by_slot
    )
    prices = sorted(
        (
            -inflation_adjusted_price(row, inflation.get(row.position, 1.0)),
            row.player_id,
        )
        for row in rows
        if row.player_id not in unavailable
    )
    opportunity = sum(-price for price, _ in prices[: me.open_slots])
    boost = max(0.0, (me.remaining - opportunity) / me.open_slots)
    return (_quantize(margin), _quantize(boost))


def best_lineup_worth(
    owned: Sequence[str],
    rows: Sequence[SheetRow],
    roster_slots: Mapping[str, int],
) -> float:
    """The summed worth of the best legal STARTING lineup from ``owned``.

    Dedicated slots first (each position's best players fill its own
    slots), then the best leftover RB/WR/TE fill the FLEX slots — optimal
    for this roster shape, because a dedicated slot always prefers its
    own position's best and FLEX accepts any leftover. Bench never
    scores: that is exactly what makes a redundant player worth ~0 here.
    Players the sheet does not price contribute nothing.
    """
    rows_by_id = {row.player_id: row for row in rows}
    by_position: dict[str, list[tuple[float, str]]] = {}
    for player_id in sorted(set(owned)):
        row = rows_by_id.get(player_id)
        if row is not None:
            by_position.setdefault(row.position, []).append(
                (-_quantize(row.worth), player_id)
            )
    total = 0.0
    flex_pool: list[tuple[float, str]] = []
    for position in sorted(by_position):
        players = sorted(by_position[position])
        starters = roster_slots.get(position, 0)
        total += sum(-worth for worth, _ in players[:starters])
        if position in FLEX_ELIGIBLE:
            flex_pool.extend(players[starters:])
    flex_slots = roster_slots.get("FLEX", 0)
    total += sum(-worth for worth, _ in sorted(flex_pool)[:flex_slots])
    return _quantize(total)


def marginal_lineup_worth(
    player_id: str,
    owned: Sequence[str],
    rows: Sequence[SheetRow],
    roster_slots: Mapping[str, int],
) -> float:
    """Best lineup with the player minus best lineup without him — the
    decided need measure. A third QB adds ~0; a player filling an open
    starting slot adds his full worth; an upgrade adds the difference."""
    with_player = best_lineup_worth([*owned, player_id], rows, roster_slots)
    without = best_lineup_worth(owned, rows, roster_slots)
    return _quantize(with_player - without)


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
