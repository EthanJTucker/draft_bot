"""Clean parsed types over raw Sleeper payloads.

Parsing only — no valuation and no live draft-state interpretation (those
are later slices). Covers Sleeper's known gotchas: the winning bid arrives
as a STRING in ``metadata.amount``, purchases attribute by ``draft_slot``
(never ``picked_by``), and a paused draft must not read as an expired timer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

# Sleeper's status for a draft that has not started yet.
PRE_DRAFT_STATUS = "pre_draft"


def slot_map_is_provisional(
    status: str, slot_pairs: Iterable[tuple[int, int | None]]
) -> bool:
    """Whether a draft's slot-to-roster map is still the placeholder.

    Sleeper seats a draft that has not been ordered yet at the identity
    (slot N holds roster N) and permutes it when the commissioner assigns
    the order — at or near draft start, and after the operator has
    already launched the dashboard. Reading "my slot" off that map gives
    a number that is about to change, so anything concluded from it is
    provisional too.

    Both halves are required. A pre_draft draft whose order HAS been
    dealt carries a real permutation and is checkable; keying on the
    status alone would go quiet on exactly the board the check works on.
    An unseated slot (no roster id at all) reads as provisional, matching
    the empty map: nobody has been dealt a seat there either.

    Takes the two facts rather than a draft object because the two
    surfaces that ask this question hold different types — the CLI a
    parsed :class:`DraftState`, the poller a tracked board. Writing the
    rule twice is exactly how the startup check and the standing page
    banner came to give opposite answers on the same board.
    """
    return status == PRE_DRAFT_STATUS and all(
        roster_id is None or slot == roster_id for slot, roster_id in slot_pairs
    )


@dataclass(frozen=True)
class Pick:
    """One completed auction purchase from the draft's picks feed."""

    # pylint: disable=too-many-instance-attributes  # parsed record type that
    # mirrors the API pick object's fields one-to-one.

    player_id: str
    amount: int | None
    draft_slot: int
    round: int
    pick_no: int
    is_keeper: bool
    picked_by: str
    metadata: Mapping = field(default_factory=dict)

    def __post_init__(self):
        # frozen=True alone leaves the metadata dict mutable, and sources
        # share Pick objects across ticks; a read-only proxy over a private
        # copy makes mutation raise instead of poisoning later ticks.
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def parse_pick(raw: dict) -> Pick:
    """Parse one raw pick. The winning bid lives ONLY in ``metadata.amount``
    (a string); any top-level ``amount`` field is not the bid."""
    metadata = raw.get("metadata") or {}
    amount = _parse_bid(metadata.get("amount"))
    return Pick(
        player_id=str(raw.get("player_id", "")),
        amount=amount,
        draft_slot=int(raw["draft_slot"]),
        round=int(raw.get("round", 0)),
        pick_no=int(raw.get("pick_no", 0)),
        is_keeper=bool(raw.get("is_keeper")),
        picked_by=str(raw.get("picked_by", "")),
        metadata=metadata,
    )


@dataclass(frozen=True)
class DraftState:
    """Parsed draft object: status, timers, budgets, and raw live-auction
    metadata (interpreting the live fields is a later slice's job)."""

    # pylint: disable=too-many-instance-attributes  # parsed record type that
    # mirrors the API draft object's fields one-to-one.

    draft_id: str
    status: str
    draft_type: str
    start_time: int | None
    paused: bool
    timer_end_at: int | None
    nominated_player_id: str | None
    highest_offer: str | None
    offering_user_id: str | None
    # Two different teams on the wire: ``nominating_slot`` is the slot that
    # NOMINATED the player; ``offering_slot`` is the current HIGH BIDDER's.
    nominating_slot: str | None
    offering_slot: str | None
    budget_by_slot: Mapping[int, int]
    slot_to_roster_id: Mapping[int, int]
    settings: Mapping = field(default_factory=dict)
    metadata: Mapping = field(default_factory=dict)

    def __post_init__(self):
        # Same reasoning as Pick: sources share DraftState objects across
        # ticks, so the mapping members must refuse writes.
        for name in ("budget_by_slot", "slot_to_roster_id", "settings", "metadata"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def is_timer_expired(self, now_ms: int) -> bool:
        """Whether the bid/nomination timer has run out.

        A paused draft freezes ``timer_end_at`` in the past (Sleeper's
        overnight auto-pause), so paused NEVER reads as expired.
        """
        if self.paused:
            return False
        if self.timer_end_at is None:
            return False
        return now_ms >= self.timer_end_at


_TRUTHY_STRINGS = {"true", "1", "yes"}


def _is_truthy_flag(value) -> bool:
    """Sleeper metadata flags arrive as strings ("true") or booleans."""
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return bool(value)


def to_int(value) -> int | None:
    """Best-effort int conversion for values Sleeper serves as strings.

    Public: the tracker uses it to normalize numeric metadata and settings
    values before comparing them.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_optional_str(value) -> str | None:
    """Coerce an id-ish metadata value to str; None stays None.

    Sleeper serves these fields as strings, but a non-string id slipping
    through would silently defeat sold-set membership (``4034 != "4034"``),
    so the coercion is defensive, mirroring ``Pick.player_id``.
    """
    return None if value is None else str(value)


def _parse_bid(value) -> int | None:
    """Parse a winning bid that may arrive malformed mid-auction.

    Integer-valued strings parse ("43", and "43.0" only because it is
    exactly integral); empty or garbage values become None rather than
    crashing the poll loop. Negative values are garbage too: "-5" flowing
    into a team's spend would silently credit its budget.
    """
    amount = to_int(value)
    if amount is None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        amount = int(number) if number.is_integer() else None
    if amount is not None and amount < 0:
        return None
    return amount


def parse_draft(raw: dict) -> DraftState:
    """Parse the draft object, honoring the pause flag and budget_<slot>."""
    metadata = raw.get("metadata") or {}
    settings = raw.get("settings") or {}
    paused = raw.get("status") == "paused" or _is_truthy_flag(metadata.get("paused"))
    budget_by_slot = {}
    for key, value in settings.items():
        if key.startswith("budget_"):
            slot = to_int(key.removeprefix("budget_"))
            budget = to_int(value)
            if slot is not None and budget is not None:
                budget_by_slot[slot] = budget
    slot_to_roster_id = {
        int(slot): int(roster_id)
        for slot, roster_id in (raw.get("slot_to_roster_id") or {}).items()
        if roster_id is not None
    }
    return DraftState(
        draft_id=str(raw.get("draft_id", "")),
        status=str(raw.get("status", "")),
        draft_type=str(raw.get("type", "")),
        start_time=to_int(raw.get("start_time")),
        paused=paused,
        timer_end_at=to_int(metadata.get("timer_end_at")),
        nominated_player_id=_to_optional_str(metadata.get("nominated_player_id")),
        highest_offer=_to_optional_str(metadata.get("highest_offer")),
        offering_user_id=_to_optional_str(metadata.get("offering_user_id")),
        nominating_slot=_to_optional_str(metadata.get("nominating_slot")),
        offering_slot=_to_optional_str(metadata.get("offering_slot")),
        budget_by_slot=budget_by_slot,
        slot_to_roster_id=slot_to_roster_id,
        settings=settings,
        metadata=metadata,
    )


def parse_picks(raw_picks: list[dict]) -> list[Pick]:
    """Parse the whole picks feed."""
    return [parse_pick(raw) for raw in raw_picks]


def spent_by_slot(picks: list[Pick]) -> dict[int, int]:
    """Total auction dollars spent per draft slot.

    Purchases attribute by ``draft_slot``: ``picked_by`` can be empty or
    reflect whoever clicked the winning bid, so it is never used here.
    """
    totals: dict[int, int] = {}
    for pick in picks:
        totals[pick.draft_slot] = totals.get(pick.draft_slot, 0) + (pick.amount or 0)
    return totals
