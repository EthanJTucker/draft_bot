"""Clean parsed types over raw Sleeper payloads.

Parsing only — no valuation and no live draft-state interpretation (those
are later slices). Covers Sleeper's known gotchas: the winning bid arrives
as a STRING in ``metadata.amount``, purchases attribute by ``draft_slot``
(never ``picked_by``), and a paused draft must not read as an expired timer.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    metadata: dict = field(default_factory=dict)


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
    nominating_slot: str | None
    budget_by_slot: dict[int, int]
    slot_to_roster_id: dict[int, int]
    settings: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

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


def _to_int(value) -> int | None:
    """Best-effort int conversion for values Sleeper serves as strings."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_bid(value) -> int | None:
    """Parse a winning bid that may arrive malformed mid-auction.

    Integer-valued strings parse ("43", and "43.0" only because it is
    exactly integral); empty or garbage values become None rather than
    crashing the poll loop.
    """
    amount = _to_int(value)
    if amount is not None:
        return amount
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def parse_draft(raw: dict) -> DraftState:
    """Parse the draft object, honoring the pause flag and budget_<slot>."""
    metadata = raw.get("metadata") or {}
    settings = raw.get("settings") or {}
    paused = raw.get("status") == "paused" or _is_truthy_flag(metadata.get("paused"))
    budget_by_slot = {}
    for key, value in settings.items():
        if key.startswith("budget_"):
            slot = _to_int(key.removeprefix("budget_"))
            budget = _to_int(value)
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
        start_time=_to_int(raw.get("start_time")),
        paused=paused,
        timer_end_at=_to_int(metadata.get("timer_end_at")),
        nominated_player_id=metadata.get("nominated_player_id"),
        highest_offer=metadata.get("highest_offer"),
        offering_user_id=metadata.get("offering_user_id"),
        nominating_slot=metadata.get("nominating_slot"),
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
