"""Static pre-draft valuation: what the room pays and what a player is worth.

Room price is the empirical median of the league's own 2023-25 winning bids
within a position/ADP band; the per-position log curve ``a + b*ln(adp)`` fit
on the same bids prices a player ONLY when the band holds fewer than six
samples (verification showed the raw curve overprices top RBs and deep QBs;
band medians are authoritative wherever they exist).

This module is pure data-in, dollars-out: no network, no draft state.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from draftbot.models import Pick

#: Positions with an auction market worth modeling; K/DEF go for $1 here.
VALUED_POSITIONS = ("QB", "RB", "WR", "TE")

#: Sleeper serves this ADP for unranked players; it means "no ADP".
NO_ADP_SENTINEL = 999.0

#: An ADP band spans adp/BAND_RATIO .. adp*BAND_RATIO, inclusive.
BAND_RATIO = 1.6

#: Bands with fewer samples than this fall back to the fitted log curve.
MIN_BAND_SAMPLES = 6

#: Nobody sells below the $1 minimum bid.
FLOOR_PRICE = 1.0


def _valid_adp(adp: float | None) -> bool:
    return adp is not None and adp != NO_ADP_SENTINEL and adp > 0


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
            adp=stats.get("adp_half_ppr"),
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
) -> list[Bid]:
    """Winning bids from the historical picks feeds, each tagged with the
    DRAFT season's ADP (never a merged or current ADP map).

    Bids are droppable only for the reference model's reasons: K/DEF picks,
    picks with no parsed amount, and players with no ADP that season.
    """
    bids = []
    for year in sorted(picks_by_year):
        season = seasons.get(year, {})
        for pick in picks_by_year[year]:
            position = (pick.metadata or {}).get("position")
            if position not in VALUED_POSITIONS or pick.amount is None:
                continue
            row = season.get(pick.player_id)
            if row is None or not _valid_adp(row.adp):
                continue
            bids.append(Bid(position=position, adp=row.adp, amount=pick.amount))
    return bids


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
    median when the band has enough samples, fitted log curve otherwise,
    $1 when the player has no ADP or the position has no history.
    """

    def __init__(self, bids: Sequence[Bid]):
        valid = [bid for bid in bids if _valid_adp(bid.adp)]
        # Sorted storage keeps every downstream median independent of the
        # caller's bid ordering (determinism rule).
        self._bids = sorted(valid, key=lambda b: (b.position, b.adp, b.amount))
        self._curves = {
            position: _fit_log_curve(
                [bid for bid in self._bids if bid.position == position]
            )
            for position in sorted({bid.position for bid in self._bids})
        }

    def band_amounts(self, position: str, adp: float | None) -> list[float]:
        """Winning bids for ``position`` with ADP in adp/1.6 .. adp*1.6."""
        if not _valid_adp(adp):
            return []
        low, high = adp / BAND_RATIO, adp * BAND_RATIO
        return [
            bid.amount
            for bid in self._bids
            if bid.position == position and low <= bid.adp <= high
        ]

    def curve_price(self, position: str, adp: float | None) -> float:
        """The fitted log-curve price, floored at $1."""
        curve = self._curves.get(position)
        if not _valid_adp(adp) or curve is None:
            return FLOOR_PRICE
        intercept, slope = curve
        return max(FLOOR_PRICE, intercept + slope * math.log(adp))

    def room_price(self, position: str, adp: float | None) -> float:
        """What this room pays: band median, curve fallback, $1 floor."""
        if not _valid_adp(adp):
            return FLOOR_PRICE
        band = self.band_amounts(position, adp)
        if len(band) >= MIN_BAND_SAMPLES:
            return float(statistics.median(band))
        return self.curve_price(position, adp)
