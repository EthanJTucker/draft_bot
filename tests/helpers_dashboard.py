"""Shared hand-fixture builders for the dashboard test modules.

Plain builders imported explicitly (the ``helpers_engine`` pattern): a
scripted source, a tick built through the real parsers, and a poller
wired with injected clocks — no network, no wall time.
"""

from __future__ import annotations

from draftbot.dashboard.state import DashboardPoller
from draftbot.models import parse_draft, parse_picks
from draftbot.sources import SourceTick
from draftbot.tracker import DraftTracker

# The commissioner HAS entered every slot, at the league budget. Numerically
# identical to leaving the keys out (the fallback is the same $200), but the
# provenance differs, and the poller's budget rule reads the provenance: a
# fixture that wants a BID/PASS verdict must carry real budgets, because a
# figure the tool guessed cannot advise. Fixtures that mean to exercise the
# GUESSED board leave budgets out.
ENTERED_BUDGETS = {slot: 200 for slot in range(1, 13)}

# A slot_to_roster_id map that is NOT the identity. My roster id is 7
# (league_config.toml), and here it drafts from slot 4 while slot 7
# belongs to roster 11 — a 3-cycle, so it stays a real permutation. Every
# completed season in this league carries a permuted map; the identity map
# a draft shows while it sits in pre_draft is a placeholder that changes
# when the draft order is assigned. Any fixture exercising "my slot" uses
# this, because on an identity map a rule reading the wrong identifier
# passes anyway.
PERMUTED_SLOTS = {
    1: 1,
    2: 2,
    3: 3,
    4: 7,
    5: 5,
    6: 6,
    7: 11,
    8: 8,
    9: 9,
    10: 10,
    11: 4,
    12: 12,
}
MY_DRAFT_SLOT = 4

# The PLACEHOLDER map a draft carries before its order is dealt: slot N =
# roster N. Spelled out rather than left to ``make_tick``'s default,
# because the fixtures that pair it with PERMUTED_SLOTS are about the
# moment one becomes the other.
IDENTITY_SLOTS = {slot: slot for slot in range(1, 13)}


class ScriptedSource:
    """poll() serves scripted entries in order (the last one repeats);
    an Exception entry raises instead of returning."""

    # pylint: disable=too-few-public-methods  # poll() IS the source seam.

    def __init__(self, entries):
        self._entries = list(entries)

    def poll(self) -> SourceTick:
        """The next scripted entry."""
        entry = self._entries.pop(0) if len(self._entries) > 1 else self._entries[0]
        if isinstance(entry, Exception):
            raise entry
        return entry


def make_tick(
    picks=(),
    *,
    nominee=None,
    offer=None,
    nominating_slot=None,
    offering_slot=None,
    status="drafting",
    paused=False,
    stale=(),
    teams=12,
    budgets=None,
    slot_to_roster_id=None,
):
    # pylint: disable=too-many-arguments  # tick builder mirroring the draft
    # object's independent live fields one-to-one.
    """One SourceTick built through the real parsers, like a source would.

    ``slot_to_roster_id`` defaults to the identity map, which is what the
    live draft carries while it sits in pre_draft. That is a PLACEHOLDER:
    every completed season in this league carries a permuted map, so slot
    and roster id stop matching the moment the draft order is assigned.
    A fixture that means to exercise "my slot" must pass a non-identity
    map, or a rule reading the wrong identifier passes it anyway.
    """
    metadata = {}
    if paused:
        metadata["paused"] = "true"
    if nominee is not None:
        metadata["nominated_player_id"] = nominee
        if offer is not None:
            metadata["highest_offer"] = str(offer)
        if nominating_slot is not None:
            metadata["nominating_slot"] = str(nominating_slot)
        if offering_slot is not None:
            metadata["offering_slot"] = str(offering_slot)
    settings = {f"budget_{slot}": amount for slot, amount in (budgets or {}).items()}
    raw = {
        "draft_id": "D1",
        "status": status,
        "type": "auction",
        "settings": settings,
        "metadata": metadata,
        "slot_to_roster_id": {
            str(slot): roster_id
            for slot, roster_id in (
                slot_to_roster_id or {slot: slot for slot in range(1, teams + 1)}
            ).items()
        },
    }
    return SourceTick(
        draft=parse_draft(raw),
        picks=tuple(parse_picks(list(picks))),
        stale_endpoints=frozenset(stale),
    )


def make_poller(
    config, entries, rows, *, keepers_by_slot=None, tracker=None, my_slot=None
):
    # pylint: disable=too-many-arguments  # fixture builder wiring the
    # poller's injectable seams one-to-one (tracker, keepers, my slot).
    """A poller over a scripted source with an injected clock."""
    tracker = tracker or DraftTracker(
        config, keepers_by_slot=keepers_by_slot, clock=lambda: 0.0
    )
    return DashboardPoller(
        ScriptedSource(entries),
        tracker,
        rows,
        config,
        keepers_by_slot=keepers_by_slot or {},
        my_slot=my_slot,
        clock=lambda: 1_000.0,
    )


def team_by_slot(state, slot):
    """The one team entry for ``slot`` in the snapshot."""
    matches = [team for team in state["teams"] if team["slot"] == slot]
    assert len(matches) == 1
    return matches[0]
