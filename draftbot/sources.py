"""Draft-state sources: a live poll and a historical replay, one interface.

Both produce :class:`SourceTick` objects from ``poll()``; the tracker
consumes ticks without knowing which source made them. Later slices reuse
the seam: the replay backtest (issue #6) drives the full engine through it
and the dashboard (issue #7) polls it.

Seam contract:

- ``LivePollSource.poll()`` CAN raise
  :class:`~draftbot.sleeper_client.SleeperUnavailableError` when an
  endpoint's live fetch fails and no disk cache exists; poll loops must
  catch it. ``ReplaySource.poll()`` never raises.
- Replay synthesis: a historical picks feed records only completed sales,
  so a replayed draft object carries exactly what the feed can know. The
  sale's winner IS the high bidder at the hammer, so the winner's
  ``draft_slot`` goes in ``offering_slot`` and the winner's ``picked_by``
  (when non-empty) in ``offering_user_id``; the NOMINATOR's identity is
  unknowable from picks, so ``nominating_slot`` is always None in replay —
  never fabricated.
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
        """Fetch the draft object and the picks feed once each.

        Raises :class:`~draftbot.sleeper_client.SleeperUnavailableError`
        when a live fetch fails and that endpoint has no cache to degrade
        to (unlike ``ReplaySource.poll``, which never raises).
        """
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
        """The next tick; past the final sale it repeats the finished draft.

        Never raises (the live source's ``SleeperUnavailableError`` has no
        replay analogue: the data is already in hand).
        """
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
        pointer keeps doing exactly that until the next lot opens).

        Only what the picks feed can know is synthesized (see the module
        docstring): winner's slot as ``offering_slot`` (the high bidder at
        the hammer is the winner), winner's ``picked_by`` as
        ``offering_user_id`` when the feed carries it, and None — not a
        fabrication — for the unknowable ``nominating_slot``."""
        last = self._picks[revealed - 1] if revealed else None
        return dataclasses.replace(
            self._draft,
            status=self._draft.status if revealed == len(self._picks) else "drafting",
            nominated_player_id=last.player_id if last else None,
            highest_offer=str(last.amount) if last else None,
            nominating_slot=None,
            offering_slot=str(last.draft_slot) if last else None,
            offering_user_id=(last.picked_by or None) if last else None,
        )
