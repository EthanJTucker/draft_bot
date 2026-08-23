"""Draft-state sources: a live poll and a historical replay, one interface.

Both produce :class:`SourceTick` objects from ``poll()``; the tracker
consumes ticks without knowing which source made them. Later slices reuse
the seam: the replay backtest (issue #6) drives the full engine through it
and the dashboard (issue #7) polls it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from draftbot.models import DraftState, Pick, parse_draft, parse_picks
from draftbot.sleeper_client import SleeperClient


@dataclass(frozen=True)
class SourceTick:
    """One observation of the draft: the draft object plus the picks feed.

    ``stale_endpoints`` names the endpoints served from the disk cache on
    THIS tick because the live fetch failed (always empty for a replay).
    """

    draft: DraftState
    picks: tuple[Pick, ...]
    stale_endpoints: frozenset[str] = frozenset()


class LivePollSource:
    """Polls the Sleeper API through the caching client, one tick per call.

    ``client.last_fetch_degraded`` is LAST-CALL state, so it is read
    immediately after each ``get_*`` call — never once at the end, where a
    later fresh fetch would erase an earlier endpoint's degradation.
    """

    # pylint: disable=too-few-public-methods  # poll() IS the source seam;
    # both sources expose exactly this one method.

    def __init__(self, client: SleeperClient, draft_id: str | None = None):
        self._client = client
        self._draft_id = draft_id

    def poll(self) -> SourceTick:
        """Fetch the draft object and the picks feed once each."""
        stale = set()
        raw_draft = self._client.get_draft(draft_id=self._draft_id)
        if self._client.last_fetch_degraded:
            stale.add("draft")
        raw_picks = self._client.get_picks(draft_id=self._draft_id)
        if self._client.last_fetch_degraded:
            stale.add("picks")
        return SourceTick(
            draft=parse_draft(raw_draft),
            picks=tuple(parse_picks(raw_picks)),
            stale_endpoints=frozenset(stale),
        )


class ReplaySource:
    """Feeds a historical draft back one sale at a time.

    The first ``poll()`` returns the draft with no sales yet; each later
    poll reveals the next pick in ``pick_no`` order.
    """

    # pylint: disable=too-few-public-methods  # poll() IS the source seam;
    # both sources expose exactly this one method.

    def __init__(self, raw_draft: dict, raw_picks: list[dict]):
        self._draft = parse_draft(raw_draft)
        self._picks = tuple(
            sorted(parse_picks(raw_picks), key=lambda pick: pick.pick_no)
        )
        self._revealed = 0

    def poll(self) -> SourceTick:
        """The next tick; past the final sale it repeats the finished draft."""
        revealed = self._revealed
        if self._revealed < len(self._picks):
            self._revealed += 1
        return SourceTick(
            draft=self._draft_as_of(revealed), picks=self._picks[:revealed]
        )

    def _draft_as_of(self, revealed: int) -> DraftState:
        """The draft object as live Sleeper would serve it after ``revealed``
        sales: mid-draft the status is ``drafting`` and the nomination
        metadata still points at the just-sold player (Sleeper's stale
        pointer keeps doing exactly that until the next lot opens)."""
        last = self._picks[revealed - 1] if revealed else None
        return dataclasses.replace(
            self._draft,
            status=self._draft.status if revealed == len(self._picks) else "drafting",
            nominated_player_id=last.player_id if last else None,
            highest_offer=str(last.amount) if last else None,
            nominating_slot=str(last.draft_slot) if last else None,
            offering_user_id=None,
        )
