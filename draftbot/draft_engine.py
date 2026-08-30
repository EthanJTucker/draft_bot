"""Dynamic repricing: a value sheet plus draft state in, a max bid out.

The engine layers four adjustments (decided in GAMEPLAN.md and PRD.md)
on top of the static value sheet, recomputed from scratch on every call:

- **Positional inflation** — remaining room money over remaining sheet
  value, per position, with off-model sales excluded and the multiplier
  tapered away from the cheap tail (measured keeper-league inflation
  concentrates above roughly the 130th player). One-directional today:
  ``INFLATION_MIN`` is 1.0, so the layer can raise a price and never
  lower one. See the constant for the measurement and for the reason
  the deflation half is off.
- **Marginal roster need** — best legal lineup with the player minus
  without, honoring FLEX eligibility. A discount schedule, never a
  premium: a scarce starter keeps the full inflation-adjusted price,
  a redundant player is discounted toward bench retention, and the
  bump is zero or negative by construction. Scarcity pressure itself
  comes from inflation, the spend schedule, and the last-of-tier flag —
  a positive roster-side bump on top would double-count what worth and
  inflation already price.
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

#: Clamp on the per-position ratio. The ceiling is the original sanity
#: bound: a depleted denominator late in the draft must not emit an
#: absurd multiplier. It is reachable and pinned by
#: ``test_a_depleted_pool_is_held_at_the_ceiling`` (raw ratio 4.47),
#: though the 2025 replay never comes near it.
#:
#: The FLOOR is a measured value, and it is doing more than sanity work.
#: It was 0.25, and on the 2025 replay 84 of the 159 scored lots priced
#: at exactly that clamp — an expensive mid-draft nominee displaying at
#: roughly a quarter of his real price. Sweeping the replay across
#: candidate floors (re-run ``python -m draftbot.backtest`` with this
#: constant overridden to reproduce any row; the committed report is a
#: single-floor render and does not carry the sweep):
#:
#:     floor   running MAE   running bias   segment bias spread
#:     0.25         5.5376        -5.3095               11.7962
#:     0.50         4.8277        -4.5368               10.5453
#:     0.85         2.6981        -1.8074                4.5583
#:     0.95         2.3370        -0.9706                2.6970
#:     1.00         2.2388        -0.5522                1.9351
#:
#: All three fall monotonically across that grid, so 1.00 scoring best
#: within it is true by construction and is not itself the reason for
#: the choice. The grid stops at par because par is the semantic
#: boundary: above 1.0 the layer can only ever inflate, which is a flat
#: markup rather than a model of an auction. The metrics do keep
#: improving past par on this fixture, each bottoming out at a DIFFERENT
#: floor — running MAE near 1.03 (2.2233), absolute bias near 1.066
#: (0.0000), segment spread near 1.10 (0.5596) — because a constant
#: markup absorbs the sheet's own -0.55 static bias. That is the sheet's
#: calibration showing through, not a better floor, and the post-#17
#: re-measurement should not chase it.
#:
#: Within the swept grid 1.00 is also where the monotone early-to-late
#: bias trend stops existing rather than merely shrinking (1.00: -1.71 /
#: +0.23 / +0.06, mid above late; 0.95: -2.64 / +0.03 / +0.06, still
#: monotone). On a denser grid the crossing sits near 0.96, so that
#: separates 1.00 from the rows above, not from every sub-par floor.
#:
#: The cost is real and deliberate: at 1.0 the engine can no longer say
#: "this position is getting cheaper, bid less" — the deflation half of
#: the model is discarded, not damped. The argument for paying it is
#: scoped to the sheet the measurement came from.
#: ``build_history_price_sheet`` sets worth = fitted room price with no
#: normalization, so the backtest sheet's above-floor total (about
#: $2445 taper-weighted) overhangs the room's $1464 of discretionary
#: money and every position opens at 0.599. Sub-1.0 there is the
#: remaining-pool denominator counting value the room never absorbs, and
#: the per-position budget split, talking — not the market.
#:
#: The PRODUCTION sheet has no such overhang. ``compute_worths`` spreads
#: exactly ``teams * (budget - drafted_slots)`` over VORP, so its
#: above-floor total equals the room's discretionary money BY
#: CONSTRUCTION, and the ratio opens at exactly 1.0 with no keepers and
#: above 1.0 with them — at ANY sheet depth, because ``_discretionary``
#: divides by that same untapered above-floor total. It did not always:
#: the pool used to be taper-weighted while the room's money stayed
#: whole, so a sheet whose priced tail ran past ``TAPER_FULL_RANK``
#: opened above par and marked every displayed dollar up for no market
#: reason. ``TestParAtOpenOnADeepSheet`` pins par at open now, so this
#: paragraph is a tested claim rather than a remark. On draft night
#: this layer is therefore actively inflating, not inert, and a
#: genuinely below-par reading there WOULD be a market signal — one this
#: floor discards. Raising the floor did not create that cost (at or
#: above par the raise is a no-op) and does not fix it. When the
#: absorbable-pool fix lands, this floor is the first thing to
#: re-measure: it is a bound on a known defect, not a statement about
#: auctions.
INFLATION_MIN = 1.0
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
    """The dollars above the $1 floor that room money has to cover.

    UNTAPERED on purpose, and the asymmetry that used to live here is the
    reason it is spelled out. The taper has exactly one job — keeping the
    cheap tail from receiving the inflation MULTIPLIER, which it still
    does in :func:`inflation_adjusted_price`. It has no business in the
    pool: a $4 tail player's $3 above the floor is money the room really
    spends, ``_on_model_spend`` really debits it when he sells, and the
    sheet's normalization really counts it. Taper-weighting the pool
    while leaving the room's money whole compared a shrunken value side
    against a full money side, which opened every position above par on
    any sheet deep enough to reach the taper window.

    Worth basis: the sheet's worths are what the room's dollars were
    normalized against; the keeper premium is next season's money and
    stays out of the ratio.
    """
    return _quantize(max(0.0, row.worth - FLOOR_PRICE))


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
    player's sheet value. Off-model sales debit no position's money. A
    sale of a player this sheet does not price touches nothing else
    either, but a sale the BOARD flags off-model while this sheet still
    prices the player — a defensive path, unreachable when tracker and
    engine share one sheet — still removes that player's value from the
    remaining pool (sold is sold, whatever the price meant), so the
    denominator and with it the ratio can move. Kept players were never
    part of the buyable pool at all — however they reached the board. A
    keeper entered as a PRICED PICK is still a kept player: his chain
    price (prior bid plus the increment, floored) is not a
    market-clearing bid, and ``valuation.build_bids`` already refuses the
    same rows for the same reason when fitting historical prices.
    """
    pool = _buyable_pool(rows, board, keepers_by_slot)
    sold = {sale.player_id for sale in board.sales}
    initial, remaining = _pool_values(pool, sold)
    total_initial = sum(initial[position] for position in sorted(initial))

    # Initial discretionary money: each team's actual budget, less any
    # keeper dollars the feed priced into its spend, minus the $1 its
    # starting open slots pin. open_slots + purchase_count restores the
    # pre-sale slot count, so the figure is constant through the draft and
    # never moves when a sale (off-model included) debits a budget. The
    # keeper term is what makes the two entry shapes agree: a keeper
    # netted out of a budget_<slot> and the same keeper priced in the feed
    # leave the room identical money to chase the pool. Without it a $200
    # budget with $104 of keepers already gone reads as $200 to spend.
    money = sum(
        team.budget - team.keeper_spend - (team.open_slots + team.purchase_count)
        for team in board.teams
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


def _buyable_pool(
    rows: Sequence[SheetRow],
    board: BoardState,
    keepers_by_slot: Mapping[int, Sequence[str]] | None,
) -> list[SheetRow]:
    """The sheet minus every kept player, from BOTH sources that carry
    them: the roster-derived mapping and the picks feed's own keeper
    rows. Sorted by player id so the ratio is order-independent."""
    kept = _keeper_ids(keepers_by_slot) | {
        sale.player_id for sale in board.sales if sale.is_keeper
    }
    return sorted(
        (row for row in rows if row.player_id not in kept),
        key=lambda row: row.player_id,
    )


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
    sale flagged off-model by the board, a KEEPER sale, or one simply
    absent from the pool, never debits any position's money.

    The keeper test is stated here as well as in the pool the caller
    hands over, and deliberately so: the pool exclusion only bites on a
    player this sheet prices, so an off-sheet keeper reaching this loop
    would otherwise debit his chain price to a position whose remaining
    value never held him.
    """
    rows_by_id = {row.player_id: row for row in pool}
    off_model = set(board.off_model_player_ids)
    spent: dict[str, float] = {}
    for sale in board.sales:
        row = rows_by_id.get(sale.player_id)
        if row is None or sale.is_keeper or sale.player_id in off_model:
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
    untouched in both directions.

    Deflation-side shape, and why it is currently unreachable: below par
    (ratio < 1) the taper holds the tail at sticker while the top
    deflates, so inside the 111-149 ramp a worse-ranked player could
    carry a slightly higher ceiling — bounded (at most ~$1 after flooring
    on a realistic decaying worth curve) and visible on any record via
    its inflation and rank fields. ``INFLATION_MIN`` is 1.0, so
    ``positional_inflation`` cannot emit a below-par ratio at all and the
    inversion cannot be reached through the engine's own entry points. A
    below-par ``inflation`` argument passed DIRECTLY to this function
    still produces it; that is the tested contract of this function, not
    a live behaviour of the engine.

    (The claim that stood here before — that no position's inflation ever
    fell below 1.0 across the pinned 2025 replay — was scoped to the
    engine suite's own replay sheet, which prices every player at his
    actual hammer price. It was never true of the backtest's honest
    history-fit replay, where every scored lot's raw ratio runs below
    1.0; see reports/backtest_2025.md.)"""
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

    Raises ``ValueError`` when the board shows kept players whose ids no
    source names (silently mispricing a keeper board is worse than
    stopping), and when the config's roster id is not on the board and
    ``my_slot`` was not passed. An off-sheet nominee prices at floor
    economics and never carries the pace boost.
    """
    slot = _resolve_my_slot(board, config) if my_slot is None else my_slot
    me = board.team(slot)
    sold = frozenset(sale.player_id for sale in board.sales)
    # Both sources, because either alone can carry this season's keepers
    # and the live one carries them in both. Union, never sum: the same
    # player through two doors is one kept player.
    keepers = _keeper_ids(keepers_by_slot) | frozenset(
        sale.player_id for sale in board.sales if sale.is_keeper
    )

    # Fail loud on the silent-keeper seam: a board that itself proves
    # keepers exist, analyzed without the keeper ids, would misprice
    # every layer (kept players wrongly stay in the pool denominators
    # and vanish from the roster) — an error, never a plausible number.
    # Keepers the picks feed PRICES name themselves, so they satisfy this
    # without a keepers_by_slot mapping.
    kept_on_board = sum(team.keeper_count for team in board.teams)
    if kept_on_board and not keepers:
        raise ValueError(
            f"the board shows {kept_on_board} kept player(s) but no keeper "
            "ids were supplied; pass keepers_by_slot so the engine can "
            "exclude them from the buyable pool"
        )

    row = next((r for r in rows if r.player_id == player_id), None)
    off_sheet = row is None
    if off_sheet:
        row = _off_sheet_row(player_id)

    inflation_map = positional_inflation(rows, board, keepers_by_slot)
    inflation = inflation_map.get(row.position, 1.0) if not off_sheet else 1.0
    inflated = inflation_adjusted_price(row, inflation)

    # Deduplicated: a keeper carried by the roster array AND priced in the
    # feed is one player on one roster slot, and counting him twice would
    # consume a second lineup spot and understate the marginal worth of
    # everything the team still needs.
    owned = list(
        dict.fromkeys(
            [
                *(keepers_by_slot or {}).get(slot, ()),
                *(sale.player_id for sale in board.sales if sale.draft_slot == slot),
            ]
        )
    )
    marginal = marginal_lineup_worth(player_id, owned, rows, config.roster_slots)
    need_adjusted = _need_adjusted_price(row, inflated, marginal)

    margin, boost = spend_schedule(board, slot, rows, inflation_map, keepers_by_slot)
    if off_sheet:
        # The boost burns surplus into players the model actually
        # prices, never into unknowns: a degenerate (empty or failed)
        # sheet load would otherwise read the whole pool as absorbed
        # and emit confident double-digit bids on off-sheet nominees.
        boost = 0.0
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
