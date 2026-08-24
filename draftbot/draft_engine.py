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

import math
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from draftbot.config import LeagueConfig
from draftbot.tracker import FLEX_ELIGIBLE, BoardState, TeamState
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

#: A player with zero lineup-marginal worth still keeps this fraction of
#: his above-floor worth: bench depth covers byes and injuries even when
#: it never starts. The keeper premium is bench value by nature and is
#: never need-discounted at all.
BENCH_RETENTION = 0.25

#: The spend-down margin at money parity: max bids deliberately start
#: this fraction of the way to value (bargain early)...
MARGIN_BASE = 0.93

#: ...and rise this much for every unit my money-per-open-slot climbs
#: past the room's (spend down late). No upper clamp: late-draft riches
#: must be spendable, and the team max bid already caps every number.
#: Calibrated against the simulated-draft spend-down test: shallower
#: slopes strand $10-16 of a $200 budget there.
MARGIN_SLOPE = 1.1

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
    pool = sorted(
        (row for row in rows if row.player_id not in _keeper_ids(keepers_by_slot)),
        key=lambda row: row.player_id,
    )
    sold = {sale.player_id for sale in board.sales}
    initial, remaining = _pool_values(pool, sold)
    total_initial = sum(initial[position] for position in sorted(initial))

    # Initial discretionary money: each team's actual budget minus the $1
    # its starting open slots pin. open_slots + purchase_count restores
    # the pre-sale slot count, so the figure is constant through the draft
    # and never moves when a sale (off-model included) debits a budget.
    money = sum(
        team.budget - (team.open_slots + team.purchase_count) for team in board.teams
    )
    spent = _on_model_spend(board, pool)

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


def _pool_values(
    pool: Sequence[SheetRow], sold: AbstractSet[str]
) -> tuple[dict[str, float], dict[str, float]]:
    """Per-position taper-weighted discretionary value of the whole
    initial pool and of its still-unsold remainder."""
    initial: dict[str, float] = {}
    remaining: dict[str, float] = {}
    for row in pool:
        disc = _discretionary(row)
        initial[row.position] = initial.get(row.position, 0.0) + disc
        if row.player_id not in sold:
            remaining[row.position] = remaining.get(row.position, 0.0) + disc
    return initial, remaining


def _on_model_spend(board: BoardState, pool: Sequence[SheetRow]) -> dict[str, float]:
    """Above-floor dollars spent per position on ON-model sales only: a
    sale flagged off-model by the board, or simply absent from the pool,
    never debits any position's money."""
    rows_by_id = {row.player_id: row for row in pool}
    off_model = set(board.off_model_player_ids)
    spent: dict[str, float] = {}
    for sale in board.sales:
        row = rows_by_id.get(sale.player_id)
        if row is None or sale.player_id in off_model:
            continue
        spent[row.position] = spent.get(row.position, 0.0) + max(
            0.0, (sale.amount or 0) - FLOOR_PRICE
        )
    return spent


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
    the rest of the room's, floored at ``MARGIN_MIN``. The boost is my
    pace deficit: remaining dollars beyond my open-slot share of the
    remaining pool's inflation-adjusted value, spread over my open slots
    and added to every bid — zero while I am on pace, growing as value
    leaves the board without my money following, so a surplus burns into
    real lots instead of stranding. A full roster returns (0, 0).
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
    boost = _pace_boost(board, me, rows, inflation, keepers_by_slot)
    return (_quantize(margin), _quantize(boost))


def _pace_boost(
    board: BoardState,
    me: TeamState,
    rows: Sequence[SheetRow],
    inflation: Mapping[str, float],
    keepers_by_slot: Mapping[int, Sequence[str]] | None,
) -> float:
    """My pace deficit per open slot: remaining dollars beyond my
    open-slot share of the remaining pool's inflation-adjusted value."""
    unavailable = {sale.player_id for sale in board.sales} | _keeper_ids(
        keepers_by_slot
    )
    pool_value = sum(
        inflation_adjusted_price(row, inflation.get(row.position, 1.0))
        for row in sorted(rows, key=lambda row: row.player_id)
        if row.player_id not in unavailable
    )
    room_open = sum(team.open_slots for team in board.teams)
    fair_share = pool_value * me.open_slots / room_open if room_open else 0.0
    return max(0.0, (me.remaining - fair_share) / me.open_slots)


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
    rows: Sequence[SheetRow],
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


@dataclass(frozen=True)
class PlayerAnalysis:
    """One nominated player, fully priced: every number the dashboard
    (issue #7) renders, so that slice only formats what is already here.

    The price layers reconcile by construction:
    ``inflation_adjusted`` is the sheet value under the position's
    tapered inflation; ``need_adjusted`` adds ``need_bump`` (zero for a
    player my lineup fully uses, negative toward bench retention for a
    redundant one); ``spend_adjusted`` applies the margin and boost; and
    ``max_bid`` is that number floored to whole dollars and capped by my
    team's max bid.
    """

    # pylint: disable=too-many-instance-attributes  # derived record type:
    # one field per number the dashboard shows for the nominated player.

    player_id: str
    name: str
    position: str | None
    rank: int | None
    worth: float
    keeper_premium: float
    value: float
    inflation: float
    inflation_adjusted: float
    marginal_worth: float
    need_bump: float
    need_adjusted: float
    spend_margin: float
    spend_boost: float
    spend_adjusted: float
    tier: TierStatus | None
    my_cap: int
    max_bid: int


def _resolve_my_slot(board: BoardState, config: LeagueConfig) -> int:
    for team in board.teams:
        if team.roster_id == config.my_roster_id:
            return team.slot
    raise ValueError(
        f"no team on the board has roster id {config.my_roster_id}; "
        "pass my_slot explicitly"
    )


def _need_adjusted_price(
    row: SheetRow, inflation_adjusted: float, marginal: float
) -> float:
    """Scale the inflated above-floor worth by how much of the player my
    lineup actually uses; the keeper premium (bench value by nature) and
    the $1 floor ride through whole."""
    fraction = min(1.0, max(0.0, marginal / row.worth)) if row.worth > 0 else 0.0
    multiplier = BENCH_RETENTION + (1.0 - BENCH_RETENTION) * fraction
    inflated_discretionary = inflation_adjusted - FLOOR_PRICE - row.keeper_premium
    return _quantize(
        FLOOR_PRICE + inflated_discretionary * multiplier + row.keeper_premium
    )


def _off_sheet_row(player_id: str) -> SheetRow:
    """Floor economics for a nominated player the sheet does not price:
    $1 worth, no premium, no rank (so the taper zeroes him out)."""
    return SheetRow(
        rank=None,
        player_id=player_id,
        name=player_id,
        position=None,
        adp=None,
        points=None,
        worth=FLOOR_PRICE,
        room_price=FLOOR_PRICE,
        price_source="floor",
        keeper_premium=0.0,
        value=FLOOR_PRICE,
    )


def analyze_player(  # pylint: disable=too-many-arguments  # the public
    # pure-function seam: the four inputs of the contract plus two
    # keyword-only extras the board cannot carry (keeper ids, replay slot).
    player_id: str,
    rows: Sequence[SheetRow],
    board: BoardState,
    config: LeagueConfig,
    *,
    keepers_by_slot: Mapping[int, Sequence[str]] | None = None,
    my_slot: int | None = None,
) -> PlayerAnalysis:
    # pylint: disable=too-many-locals  # the orchestrator: one local per
    # layer of the record it assembles, each computed by a public helper.
    """The engine's entry point: one player priced against one board.

    A pure function of (value sheet, board state, config) — plus the
    keeper lists the picks feed never carries — so the replay backtest
    can call it at the moment of any historical sale and the dashboard
    can call it on every poll. ``my_slot`` overrides the config's roster
    id lookup (replays of drafts I was not in need one).
    """
    slot = _resolve_my_slot(board, config) if my_slot is None else my_slot
    me = board.team(slot)
    sold = frozenset(sale.player_id for sale in board.sales)
    keepers = _keeper_ids(keepers_by_slot)

    row = next((r for r in rows if r.player_id == player_id), None)
    off_sheet = row is None
    if off_sheet:
        row = _off_sheet_row(player_id)

    inflation_map = positional_inflation(rows, board, keepers_by_slot)
    inflation = inflation_map.get(row.position, 1.0) if not off_sheet else 1.0
    inflated = inflation_adjusted_price(row, inflation)

    owned = [
        *(keepers_by_slot or {}).get(slot, ()),
        *(sale.player_id for sale in board.sales if sale.draft_slot == slot),
    ]
    marginal = marginal_lineup_worth(player_id, owned, rows, config.roster_slots)
    need_adjusted = _need_adjusted_price(row, inflated, marginal)

    margin, boost = spend_schedule(board, slot, rows, inflation_map, keepers_by_slot)
    if me.open_slots > 0:
        spend_adjusted = _quantize(
            FLOOR_PRICE + (need_adjusted - FLOOR_PRICE) * margin + boost
        )
        max_bid = min(me.max_bid, max(1, math.floor(_quantize(spend_adjusted))))
    else:
        spend_adjusted, max_bid = 0.0, 0

    tier = None
    if not off_sheet:
        available = [r for r in rows if r.player_id not in keepers]
        tier = tier_status(player_id, build_tiers(available), sold)

    return PlayerAnalysis(
        player_id=player_id,
        name=row.name,
        position=row.position,
        rank=row.rank,
        worth=row.worth,
        keeper_premium=row.keeper_premium,
        value=row.value,
        inflation=inflation,
        inflation_adjusted=inflated,
        marginal_worth=marginal,
        need_bump=_quantize(need_adjusted - inflated),
        need_adjusted=need_adjusted,
        spend_margin=margin,
        spend_boost=boost,
        spend_adjusted=spend_adjusted,
        tier=tier,
        my_cap=me.max_bid,
        max_bid=max_bid,
    )
