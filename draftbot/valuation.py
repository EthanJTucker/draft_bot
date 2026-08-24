"""Static pre-draft valuation: what the room pays and what a player is worth.

Room price is the empirical median of the league's own 2023-25 winning bids
within a position/ADP band; the per-position log curve ``a + b*ln(adp)`` fit
on the same bids prices a player ONLY when the band holds fewer than six
samples (verification showed the raw curve overprices top RBs and deep QBs;
band medians are authoritative wherever they exist). Curve fallbacks are
capped at the position's max observed bid — the room has never paid more
than its own record, and an extrapolated curve must not claim it will.

This module is pure data-in, dollars-out: no network, no draft state.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from draftbot.config import LeagueConfig
from draftbot.models import Pick

#: Positions with an auction market worth modeling; K/DEF go for $1 here.
VALUED_POSITIONS = ("QB", "RB", "WR", "TE")

#: Sleeper serves this ADP for unranked players; it means "no ADP".
NO_ADP_SENTINEL = 999.0

#: An ADP band spans adp/BAND_RATIO .. adp*BAND_RATIO, inclusive.
BAND_RATIO = 1.6

#: Bands with fewer samples than this fall back to the fitted log curve.
MIN_BAND_SAMPLES = 6

#: Cap curve-fallback prices at the position's max observed bid (decided
#: 2026-08-23: the room has never paid more than its own record at a
#: position, and an extrapolated curve must not claim it will).
CURVE_CAP = True

#: Nobody sells below the $1 minimum bid.
FLOOR_PRICE = 1.0


def _valid_adp(adp: float | None) -> bool:
    return adp is not None and adp != NO_ADP_SENTINEL and adp > 0


def _normalize_adp(adp: float | None) -> float | None:
    """Sleeper's 999.0 sentinel means "no ADP": normalize it to None at
    parse time so no downstream consumer (the emitted CSV included) ever
    sees the magic number. ``_valid_adp`` still rejects the sentinel as
    defense in depth for callers that bypass the parser."""
    return None if adp == NO_ADP_SENTINEL else adp


@dataclass(frozen=True)
class SeasonRow:
    """One player's row from one season's projections file.

    ``years_exp_snapshot`` is named for the verified pitfall: the projections
    API embeds the CURRENT years_exp even in historical season files, so this
    value is a today-snapshot, never as-of-season experience.
    """

    player_id: str
    position: str | None
    adp: float | None
    points: float | None
    years_exp_snapshot: int | None
    name: str


def parse_projections(rows: Sequence[dict]) -> dict[str, SeasonRow]:
    """Index raw projections-endpoint rows by player id."""
    season: dict[str, SeasonRow] = {}
    for raw in rows:
        stats = raw.get("stats") or {}
        player = raw.get("player") or {}
        player_id = str(raw.get("player_id", ""))
        name = " ".join(
            part for part in (player.get("first_name"), player.get("last_name")) if part
        )
        season[player_id] = SeasonRow(
            player_id=player_id,
            position=player.get("position"),
            adp=_normalize_adp(stats.get("adp_half_ppr")),
            points=stats.get("pts_half_ppr"),
            years_exp_snapshot=player.get("years_exp"),
            name=name,
        )
    return season


@dataclass(frozen=True)
class Bid:
    """One historical winning bid, tagged with that season's ADP."""

    position: str
    adp: float
    amount: float


def build_bids(
    picks_by_year: Mapping[int, Sequence[Pick]],
    seasons: Mapping[int, Mapping[str, SeasonRow]],
    exclude: AbstractSet[tuple[int, str]] = frozenset(),
) -> list[Bid]:
    """Winning bids from the historical picks feeds, each tagged with the
    DRAFT season's ADP (never a merged or current ADP map).

    Bids are droppable only for the reference model's reasons — K/DEF
    picks, picks with no parsed amount, players with no ADP that season —
    plus keeper rows: picks flagged ``is_keeper`` and any ``(year,
    player_id)`` in ``exclude`` (see :func:`detect_keeper_picks`). A
    keeper's cost is last season's price plus the increment, not a
    market-clearing bid, so it must never shape the price fit.
    """
    bids = []
    for year in sorted(picks_by_year):
        season = seasons.get(year, {})
        for pick in picks_by_year[year]:
            position = (pick.metadata or {}).get("position")
            if position not in VALUED_POSITIONS or pick.amount is None:
                continue
            if pick.is_keeper or (year, pick.player_id) in exclude:
                continue
            row = season.get(pick.player_id)
            if row is None or not _valid_adp(row.adp):
                continue
            bids.append(Bid(position=position, adp=row.adp, amount=pick.amount))
    return bids


def detect_keeper_picks(
    picks_by_year: Mapping[int, Sequence[Pick]],
    slot_to_roster_by_year: Mapping[int, Mapping[int, int]],
    rules: KeeperRules | None = None,
) -> set[tuple[int, str]]:
    """Unflagged keeper rows hiding in the picks feeds, as (year, player_id).

    The league's feeds can carry a keeper entered as an ordinary priced
    pick with ``is_keeper`` unset (verified in the real 2025 feed). The
    keeper signature is exact: the SAME roster holds the player in
    consecutive seasons at precisely ``max(prior price + increment,
    floor)``. Both conditions are required — draft slots shuffle between
    years (slot equality is not owner equality), and the league's feeds
    contain both same-slot chain-price sales to different owners and
    same-owner re-auctions at market prices, none of which are keepers.
    """
    rules = rules if rules is not None else KeeperRules()
    detected = set()
    for year in sorted(picks_by_year):
        if year + 1 not in picks_by_year:
            continue
        prior = {
            pick.player_id: (
                pick.amount,
                slot_to_roster_by_year.get(year, {}).get(pick.draft_slot),
            )
            for pick in picks_by_year[year]
            if pick.amount is not None
        }
        rosters = slot_to_roster_by_year.get(year + 1, {})
        for pick in picks_by_year[year + 1]:
            if pick.amount is None or pick.player_id not in prior:
                continue
            prior_amount, prior_roster = prior[pick.player_id]
            roster = rosters.get(pick.draft_slot)
            chain_cost = max(prior_amount + rules.cost_increment, rules.cost_floor)
            if (
                prior_roster is not None
                and prior_roster == roster
                and pick.amount == chain_cost
            ):
                detected.add((year + 1, pick.player_id))
    return detected


def _fit_log_curve(bids: Sequence[Bid]) -> tuple[float, float] | None:
    """Least-squares (intercept, slope) of amount on ln(adp), or None when
    the bids cannot pin a line (fewer than two distinct ADP values)."""
    points = sorted((math.log(bid.adp), bid.amount) for bid in bids)
    if len({x for x, _ in points}) < 2:
        return None
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sum(
        (x - mean_x) ** 2 for x, _ in points
    )
    return (mean_y - slope * mean_x, slope)


class PriceModel:
    """Room prices from the league's own bid history.

    ``room_price`` is the one number the rest of the bot consumes: band
    median when the band has enough samples; otherwise the fitted log
    curve, capped at the position's max observed bid; $1 when the player
    has no ADP or the position has no history.
    """

    def __init__(
        self,
        bids: Sequence[Bid],
        band_ratio: float = BAND_RATIO,
        min_band_samples: int = MIN_BAND_SAMPLES,
        curve_cap: bool = CURVE_CAP,
    ):
        valid = [bid for bid in bids if _valid_adp(bid.adp)]
        self._band_ratio = band_ratio
        self._min_band_samples = min_band_samples
        self._curve_cap = curve_cap
        # Sorted storage keeps every downstream median independent of the
        # caller's bid ordering (determinism rule).
        self._bids = sorted(valid, key=lambda b: (b.position, b.adp, b.amount))
        self._curves = {
            position: _fit_log_curve(
                [bid for bid in self._bids if bid.position == position]
            )
            for position in sorted({bid.position for bid in self._bids})
        }
        # PER-POSITION max over the SAME fit population as the bands
        # (keeper rows are already excluded by build_bids): the cap on
        # curve extrapolations.
        self._max_bids = {
            position: max(bid.amount for bid in self._bids if bid.position == position)
            for position in sorted({bid.position for bid in self._bids})
        }

    def band_amounts(self, position: str, adp: float | None) -> list[float]:
        """Winning bids for ``position`` with ADP in adp/ratio .. adp*ratio
        (ratio 1.6 unless configured otherwise), inclusive."""
        if not _valid_adp(adp):
            return []
        low, high = adp / self._band_ratio, adp * self._band_ratio
        return [
            bid.amount
            for bid in self._bids
            if bid.position == position and low <= bid.adp <= high
        ]

    def curve_price(self, position: str, adp: float | None) -> float:
        """The RAW fitted log-curve price, floored at $1 — diagnostic only:
        never capped, so ``room_price`` may sit below it."""
        curve = self._curves.get(position)
        if not _valid_adp(adp) or curve is None:
            return FLOOR_PRICE
        intercept, slope = curve
        return max(FLOOR_PRICE, intercept + slope * math.log(adp))

    def _curve_fallback(self, position: str, adp: float) -> tuple[float, str]:
        """Curve-fallback price with its honest label: "curve" straight off
        the fitted line, "curve_capped" when the position's max observed
        bid binds, "curve_floor" when the extrapolation fell below $1, and
        "floor" when the position has no fittable curve at all."""
        curve = self._curves.get(position)
        if curve is None:
            return FLOOR_PRICE, "floor"
        intercept, slope = curve
        price = _quantize(intercept + slope * math.log(adp))
        cap = _quantize(float(self._max_bids[position]))
        if self._curve_cap and price > cap:
            return cap, "curve_capped"
        if price < FLOOR_PRICE:
            return FLOOR_PRICE, "curve_floor"
        return price, "curve"

    def room_price(self, position: str, adp: float | None) -> float:
        """What this room pays: band median, capped-curve fallback, $1
        floor. Band medians are never capped (they cannot exceed the
        position max by construction)."""
        if not _valid_adp(adp):
            return FLOOR_PRICE
        band = self.band_amounts(position, adp)
        if len(band) >= self._min_band_samples:
            return float(statistics.median(band))
        return self._curve_fallback(position, adp)[0]

    def price_source(self, position: str, adp: float | None) -> str:
        """Which rule priced this player: "band", "curve", "curve_capped",
        "curve_floor", or "floor".

        Mirrors :meth:`room_price` exactly — both delegate the fallback to
        the same helper, so price and label cannot drift apart.
        """
        if not _valid_adp(adp):
            return "floor"
        if len(self.band_amounts(position, adp)) >= self._min_band_samples:
            return "band"
        return self._curve_fallback(position, adp)[1]


#: Share of each FLEX slot assumed to go to RB/WR/TE when computing how
#: many players at a position start league-wide (half-PPR convention).
FLEX_SPLIT = {"RB": 0.5, "WR": 0.4, "TE": 0.1}

#: Round to this many decimals before ranking/tie-breaking (determinism).
_QUANTIZE_DECIMALS = 10


def _quantize(value: float) -> float:
    return round(value, _QUANTIZE_DECIMALS)


def replacement_ranks(roster_slots: Mapping[str, int], teams: int) -> dict[str, int]:
    """Positional rank of the replacement-level player: the first player
    past the league's last starter, counting each position's share of the
    FLEX slots."""
    flex_slots = roster_slots.get("FLEX", 0)
    ranks = {}
    for position in VALUED_POSITIONS:
        starters = teams * (
            roster_slots.get(position, 0) + FLEX_SPLIT.get(position, 0.0) * flex_slots
        )
        ranks[position] = math.floor(starters) + 1
    return ranks


def _replacement_points(
    season: Mapping[str, SeasonRow], ranks: Mapping[str, int]
) -> dict[str, float]:
    """Projected points of the replacement-level player per position (0.0
    when the pool is shallower than the replacement rank)."""
    replacement = {}
    for position, rank in sorted(ranks.items()):
        pool = sorted(
            (
                (-_quantize(row.points), row.player_id)
                for row in season.values()
                if row.position == position and row.points is not None
            ),
        )
        replacement[position] = -pool[rank - 1][0] if len(pool) >= rank else 0.0
    return replacement


def compute_worths(
    season: Mapping[str, SeasonRow],
    roster_slots: Mapping[str, int],
    teams: int,
    budget: int,
) -> dict[str, float]:
    """Roster-independent worth in auction dollars for every player.

    Each drafted slot costs a $1 minimum bid; the rest of the league's money
    (teams x (budget - drafted slots), IR excluded because IR spots are not
    drafted) is spread over projected points above replacement. K/DEF and
    everyone at or below replacement are $1 roster fillers. Marginal-roster
    and inflation adjustments belong to later slices, not here.
    """
    ranks = replacement_ranks(roster_slots, teams)
    replacement = _replacement_points(season, ranks)
    vorp = {
        row.player_id: _quantize(max(0.0, row.points - replacement[row.position]))
        for row in season.values()
        if row.position in VALUED_POSITIONS and row.points is not None
    }
    total_vorp = sum(vorp[player_id] for player_id in sorted(vorp))
    drafted_slots = sum(count for slot, count in roster_slots.items() if slot != "IR")
    discretionary = teams * (budget - drafted_slots)
    dollars_per_point = discretionary / total_vorp if total_vorp > 0 else 0.0
    return {
        player_id: _quantize(FLOOR_PRICE + vorp.get(player_id, 0.0) * dollars_per_point)
        for player_id in sorted(season)
    }


#: Multi-year keeper discount per season of delay (decided in GAMEPLAN.md).
GAMMA = 0.8

#: Room prices under $3 are auction noise; they never form transitions.
MIN_TRANSITION_PRICE = 3.0

#: A comparable pool must reach this size before the search stops widening.
MIN_POOL_SIZE = 25

#: The age-matched old-RB pool is scarce; it may stop at this size instead.
MIN_OLD_RB_POOL_SIZE = 8

#: Price-band ratios for pool widening levels 0, 1, 2.
POOL_PRICE_BANDS = (1.6, 2.2, 3.0)

#: Under this many two-year samples, OV2 falls back to half of OV1.
MIN_TWO_YEAR_SAMPLES = 10

#: An RB with this much experience prices off the age-matched pool.
OLD_RB_EXPERIENCE = 8

#: Positions whose transitions are comparable to each candidate position
#: before the pool widens to every position (TEs price like thin WRs).
POSITION_GROUPS = {
    "QB": frozenset({"QB"}),
    "RB": frozenset({"RB"}),
    "WR": frozenset({"WR"}),
    "TE": frozenset({"TE", "WR"}),
}


def experience_as_of(
    exp_now: int | None, season: int, current_season: int
) -> int | None:
    """As-of-season experience from a current-snapshot ``years_exp``.

    The projections API embeds the CURRENT years_exp even in historical
    season files (verified live 2026-08-23), so historical buckets must be
    recomputed: ``exp_t = exp_now - (current_season - t)``, floored at 0.
    """
    if exp_now is None:
        return None
    return max(0, exp_now - (current_season - season))


@dataclass(frozen=True)
class TransitionSample:
    """One player's year-over-year price transition.

    ``ratio_one``/``ratio_two`` are next-price and price-after-next over
    ``price``; ``experience`` is as-of the sample's season, never the
    snapshot value.
    """

    position: str
    experience: int | None
    price: float
    ratio_one: float
    ratio_two: float | None


def build_transition_samples(
    seasons: Mapping[int, Mapping[str, SeasonRow]],
    price_model: PriceModel,
    current_season: int,
) -> list[TransitionSample]:
    """Price transitions for every player priced at $3+ in a season with a
    following season on file (2023->24->25, 2024->25->26, 2025->26).

    A player missing from a later file fell off the board and transitions
    to the $1 floor — dropping him instead would survivorship-bias every
    ratio upward.
    """

    def price_in(year: int, player_id: str, position: str) -> float:
        row = seasons.get(year, {}).get(player_id)
        return price_model.room_price(position, row.adp if row else None)

    samples = []
    for year in sorted(seasons):
        if year + 1 not in seasons:
            continue
        has_year_two = year + 2 in seasons
        for player_id in sorted(seasons[year]):
            row = seasons[year][player_id]
            if row.position not in VALUED_POSITIONS:
                continue
            price = price_model.room_price(row.position, row.adp)
            if price < MIN_TRANSITION_PRICE:
                continue
            ratio_one = _quantize(price_in(year + 1, player_id, row.position) / price)
            ratio_two = (
                _quantize(price_in(year + 2, player_id, row.position) / price)
                if has_year_two
                else None
            )
            samples.append(
                TransitionSample(
                    position=row.position,
                    experience=experience_as_of(
                        row.years_exp_snapshot, year, current_season
                    ),
                    price=_quantize(price),
                    ratio_one=ratio_one,
                    ratio_two=ratio_two,
                )
            )
    return samples


@dataclass(frozen=True)
class KeeperRules:
    """The league's keeper cost rule: cost = max(price + increment, floor),
    with at most ``max_consecutive_years`` keeps in a row."""

    cost_increment: int = 2
    cost_floor: int = 5
    max_consecutive_years: int = 2


@dataclass(frozen=True)
class OptionValues:
    """Expected keep-again profits: ``one_year`` is E[max(0, V1 - cost1)],
    ``two_year`` the year-two term conditioned on the year-one keep being
    in the money. ``pool_size``/``widen_level`` document the comparable
    pool that produced them."""

    one_year: float
    two_year: float
    pool_size: int
    widen_level: int


class KeeperModel:
    """Keeper option pricing from the league's own price transitions."""

    def __init__(
        self,
        samples: Sequence[TransitionSample],
        rules: KeeperRules | None = None,
        gamma: float = GAMMA,
    ):
        self._samples = list(samples)
        self._rules = rules if rules is not None else KeeperRules()
        self._gamma = gamma

    def keeper_cost(self, price: float) -> float:
        """Next season's keeper cost for a player bought at ``price``."""
        return max(price + self._rules.cost_increment, self._rules.cost_floor)

    def comparable_pool(
        self, position: str, experience: int | None, price: float
    ) -> tuple[list[TransitionSample], int]:
        """Transition samples comparable to the candidate, widening the
        price band (and position match) until the pool is big enough.

        Old RBs (8+ years as of the current season) price off an
        age-matched pool of 6+ year backs — the raw veteran bucket mixes
        in mid-career backs whose aging curve they no longer share.
        """
        # ``(experience or 0)``: a None-experience candidate deliberately
        # prices off the YOUNG pool, inherited verbatim from the reference
        # model (``exp26 or 0``). Reference conformance is load-bearing;
        # the choice is pinned by test, not changed.
        old_rb = position == "RB" and (experience or 0) >= OLD_RB_EXPERIENCE
        young = (experience or 0) <= 2
        group = POSITION_GROUPS.get(position, frozenset())
        min_pool = MIN_OLD_RB_POOL_SIZE if old_rb else MIN_POOL_SIZE

        def matches(sample: TransitionSample, level: int) -> bool:
            if sample.experience is None:
                return False
            if old_rb:
                floor = 6 if level < 2 else 5
                comparable = sample.position == "RB" and sample.experience >= floor
            elif young:
                comparable = (
                    sample.position in group or level >= 1
                ) and sample.experience <= 2
            else:
                comparable = (
                    sample.position in group or level >= 1
                ) and 3 <= sample.experience <= 8
            band = POOL_PRICE_BANDS[level]
            return comparable and price / band <= sample.price <= price * band

        pool: list[TransitionSample] = []
        for level, _ in enumerate(POOL_PRICE_BANDS):
            pool = [s for s in self._samples if matches(s, level)]
            if len(pool) >= min_pool:
                return pool, level
        return pool, len(POOL_PRICE_BANDS) - 1

    def option_values(
        self, position: str, experience: int | None, room_price: float, price: float
    ) -> OptionValues:
        """Expected keep-again profits for a candidate worth ``room_price``
        on the open market and bought (or kept) at ``price``."""
        cost_one = self.keeper_cost(price)
        cost_two = self.keeper_cost(cost_one)
        pool, level = self.comparable_pool(position, experience, room_price)
        if not pool:
            return OptionValues(0.0, 0.0, 0, level)
        one_year = sum(
            max(0.0, room_price * s.ratio_one - cost_one) for s in pool
        ) / len(pool)
        two_year_pool = [s for s in pool if s.ratio_two is not None]
        if len(two_year_pool) >= MIN_TWO_YEAR_SAMPLES:
            # Year two happens only on paths where year one was worth
            # keeping; dividing by the FULL pool bakes that probability in.
            two_year = sum(
                max(0.0, room_price * s.ratio_two - cost_two)
                for s in two_year_pool
                if room_price * s.ratio_one > cost_one
            ) / len(two_year_pool)
        else:
            two_year = 0.5 * one_year
        return OptionValues(_quantize(one_year), _quantize(two_year), len(pool), level)

    def premium(self, options: OptionValues, years_already_kept: int = 0) -> float:
        """Discounted keeper premium: gamma*OV1 + gamma^2*OV2, truncated by
        the consecutive-keep cap (a player kept twice has no option left)."""
        keeps_left = self._rules.max_consecutive_years - years_already_kept
        value = 0.0
        if keeps_left >= 1:
            value += self._gamma * options.one_year
        if keeps_left >= 2:
            value += self._gamma**2 * options.two_year
        return _quantize(value)


#: Positions that appear on the value sheet (the drafted roster's needs).
SHEET_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


@dataclass(frozen=True)
class SheetRow:
    """One line of the ranked, priced player pool."""

    # pylint: disable=too-many-instance-attributes  # record type mirroring
    # the emitted CSV's columns one-to-one.

    rank: int
    player_id: str
    name: str
    position: str
    adp: float | None
    points: float | None
    worth: float
    room_price: float
    price_source: str
    keeper_premium: float
    value: float


def _keeper_premium(keeper: KeeperModel, row: SeasonRow, room_price: float) -> float:
    """The discounted keep-again premium for a fresh auction buy at the
    room price (zero prior keeps; K/DEF have no keeper market)."""
    if row.position not in VALUED_POSITIONS:
        return 0.0
    options = keeper.option_values(
        row.position, row.years_exp_snapshot, room_price, room_price
    )
    return keeper.premium(options)


def _fit_models(
    seasons: Mapping[int, Mapping[str, SeasonRow]],
    picks_by_year: Mapping[int, Sequence[Pick]],
    config: LeagueConfig,
    slot_to_roster_by_year: Mapping[int, Mapping[int, int]] | None,
) -> tuple[PriceModel, KeeperModel]:
    """The fitted price and keeper models behind one value sheet, with
    keeper rows (flagged, plus detected when slot maps are given) kept out
    of the bid pool."""
    rules = KeeperRules(
        cost_increment=config.keeper_cost_increment,
        cost_floor=config.keeper_cost_floor,
        max_consecutive_years=config.max_consecutive_keep_years,
    )
    keeper_rows: AbstractSet[tuple[int, str]] = frozenset()
    if slot_to_roster_by_year is not None:
        keeper_rows = detect_keeper_picks(picks_by_year, slot_to_roster_by_year, rules)
    prices = PriceModel(build_bids(picks_by_year, seasons, exclude=keeper_rows))
    keeper = KeeperModel(
        build_transition_samples(seasons, prices, config.season),
        rules=rules,
    )
    return prices, keeper


def build_value_sheet(
    seasons: Mapping[int, Mapping[str, SeasonRow]],
    picks_by_year: Mapping[int, Sequence[Pick]],
    config: LeagueConfig,
    slot_to_roster_by_year: Mapping[int, Mapping[int, int]] | None = None,
) -> list[SheetRow]:
    """The full ranked, priced player pool for the config's season.

    Every player with an ADP or positive projection at a rosterable
    position gets worth (VBD dollars), room price (band median with curve
    fallback), and an NPV-adjusted value: worth plus the keeper premium
    evaluated at the room price. Ranked by value, ties broken by player
    id after quantizing — two runs over the same inputs emit identical
    rows.

    ``slot_to_roster_by_year`` (each year's draft ``slot_to_roster_id``)
    lets the fit drop unflagged keeper rows hiding in the feeds; without
    it only ``is_keeper``-flagged picks are dropped.
    """
    season = seasons[config.season]
    prices, keeper = _fit_models(seasons, picks_by_year, config, slot_to_roster_by_year)
    worths = compute_worths(
        season, config.roster_slots, config.teams, config.auction_budget
    )
    unranked = []
    for player_id in sorted(season):
        row = season[player_id]
        draftable = _valid_adp(row.adp) or (row.points or 0) > 0
        if row.position not in SHEET_POSITIONS or not draftable:
            continue
        room = _quantize(prices.room_price(row.position, row.adp))
        premium = _keeper_premium(keeper, row, room)
        unranked.append(
            (
                _quantize(worths[player_id] + premium),
                row,
                room,
                prices.price_source(row.position, row.adp),
                premium,
            )
        )
    unranked.sort(key=lambda entry: (-entry[0], entry[1].player_id))
    return [
        SheetRow(
            rank=rank,
            player_id=row.player_id,
            name=row.name,
            position=row.position,
            adp=row.adp,
            points=row.points,
            worth=worths[row.player_id],
            room_price=room,
            price_source=source,
            keeper_premium=premium,
            value=value,
        )
        for rank, (value, row, room, source, premium) in enumerate(unranked, start=1)
    ]
