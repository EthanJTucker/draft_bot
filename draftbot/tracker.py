"""Live draft-state tracking: team budgets, open slots, max bids, needs.

The tracker consumes :class:`~draftbot.sources.SourceTick` objects and is
deliberately blind to whether a live poll or a historical replay produced
them. Valuation and inflation math live elsewhere: a value sheet enters
only as an optional plain mapping, used to FLAG off-model sales.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from draftbot.config import LeagueConfig
from draftbot.models import DraftState, to_int
from draftbot.sources import SourceTick

# The nomination pointer legitimately keeps naming the just-sold winner for
# about one nomination timer after a sale; longer than this and the feed is
# stuck, not merely between lots.
DEFAULT_GRACE_SECONDS = 10.0

NOMINATION_LIVE = "live"
NOMINATION_NONE = "none"
NOMINATION_SOLD_GRACE = "sold_between_lots"
NOMINATION_SOLD_STALE = "sold_stale"

FLEX_ELIGIBLE = ("RB", "WR", "TE")


@dataclass(frozen=True)
class TeamState:
    """One team's standing: what it can still spend and still needs."""

    # pylint: disable=too-many-instance-attributes  # derived record type:
    # one field per number the dashboard shows for a team.

    slot: int
    roster_id: int | None
    budget: int
    budget_is_default: bool
    spent: int
    keeper_count: int
    purchase_count: int
    open_slots: int
    needs: Mapping[str, int]

    @property
    def remaining(self) -> int:
        """Auction dollars left."""
        return self.budget - self.spent

    @property
    def max_bid(self) -> int:
        """The most this team can bid on the NEXT player: it must retain
        $1 for every OTHER unfilled slot (not $1 for all of them, and not
        zero — either lazy version strands the roster)."""
        if self.open_slots <= 0:
            return 0
        return max(0, self.remaining - (self.open_slots - 1))


@dataclass(frozen=True)
class NominationView:
    """The current lot, AFTER the stale-pointer guard has ruled on it.

    ``is_live`` is true only for a nominee absent from the sold set; a sold
    nominee is ``sold_between_lots`` within the post-sale grace window and
    ``sold_stale`` beyond it — in no case does it render as live.
    """

    player_id: str | None
    is_live: bool
    status: str
    highest_offer: int | None = None
    nominating_slot: int | None = None


@dataclass(frozen=True)
class SettingsMismatch:
    """One live draft setting that differs from a valuation assumption."""

    field: str
    expected: object
    actual: object


def default_expected_settings(config: LeagueConfig) -> dict[str, object]:
    """The assumptions the config encodes, in ``diff_settings`` form.

    Timer expectations are league facts, not config keys, so callers add
    them as data (e.g. ``{"nomination_timer": 10, "pick_timer": 10}``).
    """
    return {
        "type": "auction",
        "teams": config.teams,
        "budget": config.auction_budget,
    }


def keepers_by_slot_from_rosters(
    draft: DraftState, raw_rosters: Sequence[Mapping]
) -> dict[int, tuple[str, ...]]:
    """Keeper player ids per DRAFT SLOT.

    Rosters key their ``keepers`` lists by roster_id; the draft object's
    ``slot_to_roster_id`` bridges the two (slot and roster id genuinely
    differ in this league). A missing or null keepers list reads as none.
    """
    keepers_by_roster = {
        roster.get("roster_id"): tuple(str(pid) for pid in roster.get("keepers") or ())
        for roster in raw_rosters
    }
    return {
        slot: keepers_by_roster.get(roster_id, ())
        for slot, roster_id in draft.slot_to_roster_id.items()
    }


def diff_settings(
    draft: DraftState, expected: Mapping[str, object]
) -> tuple[SettingsMismatch, ...]:
    """Every expected key the live draft contradicts (missing counts too).

    ``type`` compares against the draft object's type; every other key
    compares against ``draft.settings``, so timers, budget, teams, and any
    future assumption all ride the same check.
    """
    mismatches = []
    for field, want in expected.items():
        got = draft.draft_type if field == "type" else draft.settings.get(field)
        if to_int(want) is not None and to_int(got) is not None:
            equal = to_int(want) == to_int(got)
        else:
            equal = want == got
        if not equal:
            mismatches.append(SettingsMismatch(field, want, got))
    return tuple(mismatches)


@dataclass(frozen=True)
class BoardState:
    """Everything the tracker knows after one tick."""

    status: str
    paused: bool
    teams: tuple[TeamState, ...]
    nomination: NominationView = NominationView(None, False, NOMINATION_NONE)
    settings_warnings: tuple[SettingsMismatch, ...] = ()
    # Sold players with no value in the injected sheet: their dollars DID
    # leave the buying team's budget, but the inflation math (issue #5)
    # must leave them out of its ratio.
    off_model_player_ids: tuple[str, ...] = ()
    stale_endpoints: frozenset[str] = frozenset()

    def team(self, slot: int) -> TeamState:
        """The team drafting from ``slot``."""
        for team in self.teams:
            if team.slot == slot:
                return team
        raise KeyError(f"no team in draft slot {slot}")


class DraftTracker:
    """Recomputes the full board from each tick (180 picks is tiny; a
    stateless recompute cannot drift from the feed)."""

    # pylint: disable=too-few-public-methods  # update() is the whole
    # interface: ticks in, BoardState out.
    # pylint: disable=too-many-instance-attributes  # one attribute per
    # injected seam plus the guard's two clock marks; a knobs object would
    # only relocate the count.

    def __init__(  # pylint: disable=too-many-arguments  # all keyword-only
        # injected seams (keepers, positions, sheet, expectations, clock).
        self,
        config: LeagueConfig,
        *,
        keepers_by_slot: Mapping[int, Sequence[str]] | None = None,
        player_positions: Mapping[str, str] | None = None,
        expected_settings: Mapping[str, object] | None = None,
        value_sheet: Mapping[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
    ):
        self._config = config
        self._player_positions = dict(player_positions or {})
        self._expected_settings = dict(expected_settings or {})
        self._value_sheet = None if value_sheet is None else dict(value_sheet)
        self._keepers_by_slot = {
            slot: tuple(players) for slot, players in (keepers_by_slot or {}).items()
        }
        self._clock = clock
        self._grace_seconds = grace_seconds
        self._observed_sales = 0
        self._last_sale_at: float | None = None

    def update(self, tick: SourceTick) -> BoardState:
        """Fold one observation of the draft into a fresh board."""
        if len(tick.picks) > self._observed_sales:
            self._observed_sales = len(tick.picks)
            self._last_sale_at = self._clock()
        sold = {pick.player_id for pick in tick.picks}
        return BoardState(
            status=tick.draft.status,
            paused=tick.draft.paused,
            teams=tuple(self._team_state(slot, tick) for slot in self._slots(tick)),
            nomination=self._resolve_nomination(tick.draft, sold),
            settings_warnings=self._settings_warnings(tick.draft),
            off_model_player_ids=self._off_model(tick),
            stale_endpoints=tick.stale_endpoints,
        )

    def _off_model(self, tick: SourceTick) -> tuple[str, ...]:
        """Sales the value sheet knows nothing about, in sale order. With
        no sheet injected there is no basis for calling anything off-model."""
        if self._value_sheet is None:
            return ()
        return tuple(
            pick.player_id
            for pick in tick.picks
            if pick.player_id not in self._value_sheet
        )

    def _settings_warnings(self, draft: DraftState) -> tuple[SettingsMismatch, ...]:
        """Assumption diffs, plus the pre-entry keeper condition: rosters
        show keepers but no budget_<slot> exists yet, so every default
        budget on the board overstates what a keeper team can spend."""
        warnings = list(diff_settings(draft, self._expected_settings))
        keeper_teams = sum(1 for players in self._keepers_by_slot.values() if players)
        if keeper_teams and not draft.budget_by_slot:
            warnings.append(
                SettingsMismatch(
                    field="keeper_budgets",
                    expected=f"budget_<slot> entered ({keeper_teams} teams "
                    "hold keepers)",
                    actual="not entered; teams shown at the "
                    f"${self._config.auction_budget} default",
                )
            )
        return tuple(warnings)

    def _resolve_nomination(self, draft: DraftState, sold: set[str]) -> NominationView:
        """Rule on the nomination pointer against the FULL sold set — the
        pointer can lag more than one lot, so comparing against only the
        latest pick's winner is exactly the lazy guard this forbids."""
        nominee = draft.nominated_player_id
        if not nominee:
            return NominationView(None, False, NOMINATION_NONE)
        offer = to_int(draft.highest_offer)
        slot = to_int(draft.nominating_slot)
        if nominee in sold:
            in_grace = (
                self._last_sale_at is not None
                and self._clock() - self._last_sale_at <= self._grace_seconds
            )
            status = NOMINATION_SOLD_GRACE if in_grace else NOMINATION_SOLD_STALE
            return NominationView(nominee, False, status, offer, slot)
        return NominationView(nominee, True, NOMINATION_LIVE, offer, slot)

    def _slots(self, tick: SourceTick) -> list[int]:
        if tick.draft.slot_to_roster_id:
            return sorted(tick.draft.slot_to_roster_id)
        return list(range(1, self._config.teams + 1))

    def _team_state(self, slot: int, tick: SourceTick) -> TeamState:
        purchases = [pick for pick in tick.picks if pick.draft_slot == slot]
        keepers = self._keepers_by_slot.get(slot, ())
        budget = tick.draft.budget_by_slot.get(slot)
        open_slots = max(0, self._config.drafted_slots - len(keepers) - len(purchases))
        positions = [self._player_positions.get(player_id) for player_id in keepers]
        positions.extend(
            pick.metadata.get("position") or self._player_positions.get(pick.player_id)
            for pick in purchases
        )
        return TeamState(
            slot=slot,
            roster_id=tick.draft.slot_to_roster_id.get(slot),
            budget=self._config.auction_budget if budget is None else budget,
            budget_is_default=budget is None,
            spent=sum(pick.amount or 0 for pick in purchases),
            keeper_count=len(keepers),
            purchase_count=len(purchases),
            open_slots=open_slots,
            needs=self._compute_needs(positions),
        )

    def _compute_needs(self, positions: Sequence[str | None]) -> dict[str, int]:
        """Unfilled roster slots by label after placing every owned player:
        dedicated position first, FLEX for surplus RB/WR/TE, bench last.
        A player with no known position still occupies a bench spot —
        losing it would overstate what the team has left to buy."""
        needs = {
            label: count
            for label, count in self._config.roster_slots.items()
            if label != "IR"
        }
        for raw in positions:
            position = (raw or "").upper()
            if needs.get(position, 0) > 0:
                needs[position] -= 1
            elif position in FLEX_ELIGIBLE and needs.get("FLEX", 0) > 0:
                needs["FLEX"] -= 1
            elif needs.get("BN", 0) > 0:
                needs["BN"] -= 1
        return needs
