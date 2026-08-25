"""The poll loop's core: fold one source tick into one JSON-ready snapshot.

:class:`DashboardPoller` owns the render surface. Each ``step()`` polls the
source once, folds the tick through the tracker, prices the nominated
player through the engine, and replaces the snapshot dict that ``/state``
serves. Fail-closed rules live here, not in the page: an untrusted or
paused board never carries a BID/PASS verdict, a dead source keeps the
last good snapshot (labeled), and a keeper board that cannot be priced
shows the error instead of a number.

Teams deliberately expose ``remaining`` and never ``spent``: the page
cannot bind the wrong figure if the wrong figure is not in the JSON.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence

from draftbot.config import LeagueConfig
from draftbot.draft_engine import PlayerAnalysis, analyze_player
from draftbot.sleeper_client import SleeperUnavailableError
from draftbot.sources import SourceTick
from draftbot.tracker import (
    NOMINATION_LIVE,
    NOMINATION_NONE,
    NOMINATION_SOLD_GRACE,
    NOMINATION_UNTRUSTED,
    BoardState,
    DraftTracker,
    Sale,
    TeamState,
)
from draftbot.valuation import SheetRow


def _verdict(  # pylint: disable=too-many-return-statements  # one return
    # per fail-closed rule keeps each suppression reason on its own line.
    status: str,
    paused: bool,
    high_bid: int | None,
    analysis: dict | None,
    error: str | None,
) -> tuple[dict | None, str]:
    """The plain BID/PASS call, deliberately simple (nuance is the
    engine's job): BID exactly when the current high bid is BELOW my max
    bid — at equality I could only match, never beat, so equality is PASS.

    Fail-closed by rule: no verdict on a paused draft, an untrusted picks
    feed, a stale (beyond-grace) pointer, a lot the engine could not
    price, or a lot with no recorded high bid (an open lot always carries
    one; its absence is suspect data, and a fabricated $0 offer would
    scream BID). A just-sold lot (within grace) keeps its verdict as the
    retrospective call the bot was making when the hammer fell.
    """
    if paused:
        return None, "draft paused"
    if status == NOMINATION_UNTRUSTED:
        return None, "picks feed untrusted (stale or regressed); not advising"
    if status == NOMINATION_NONE:
        return None, "no nomination data"
    if status not in (NOMINATION_LIVE, NOMINATION_SOLD_GRACE):
        return None, "nomination pointer stale beyond grace; not advising"
    if analysis is None:
        return None, error or "no analysis"
    if high_bid is None:
        return None, "no recorded high bid for this lot; not advising on suspect data"
    action = "BID" if high_bid < analysis["max_bid"] else "PASS"
    label = "final" if status == NOMINATION_SOLD_GRACE else "live"
    return {
        "action": action,
        "margin": analysis["max_bid"] - high_bid,
        "basis": label,
    }, label


class DashboardPoller:
    """Polls a source, tracks the board, and keeps the current snapshot.

    ``step()`` is the whole write path (the poll thread calls it once per
    cycle; tests call it directly); ``snapshot`` is the whole read path
    (the ``/state`` endpoint returns it verbatim). The snapshot is
    replaced atomically per step, never mutated in place, so a concurrent
    reader always sees one consistent state.
    """

    # pylint: disable=too-many-instance-attributes  # one attribute per
    # injected seam (source, tracker, sheet, config, keepers, slot, clock)
    # plus the loop's own carried state.

    def __init__(  # pylint: disable=too-many-arguments  # the poller wires
        # every seam of the render path; each keyword is one of them.
        self,
        source,
        tracker: DraftTracker,
        rows: Sequence[SheetRow],
        config: LeagueConfig,
        *,
        keepers_by_slot: Mapping[int, Sequence[str]],
        my_slot: int | None = None,
        clock: Callable[[], float] = time.time,
        note: str | None = None,
    ):
        self._source = source
        self._tracker = tracker
        self._rows = list(rows)
        self._rows_by_id = {row.player_id: row for row in self._rows}
        self._config = config
        self._keepers_by_slot = {
            slot: tuple(players) for slot, players in keepers_by_slot.items()
        }
        self._my_slot = my_slot
        self._clock = clock
        # A standing on-page caveat about THIS serving mode (the replay
        # demo's all-PASS/$0 shape); None in live mode.
        self._note = note
        self._names = {row.player_id: row.name for row in self._rows}
        self._positions = {row.player_id: row.position for row in self._rows}
        self._board: BoardState | None = None
        self._prev_board: BoardState | None = None
        # Cache for a SOLD nominee's pre-sale pricing, keyed by player id:
        # (player_id, analysis, pre_sale, error). Live lots never cache.
        self._sold_analysis: tuple[str, dict | None, bool, str | None] | None = None
        self._poll_count = 0
        self._snapshot: dict = {
            "ok": False,
            "poll_count": 0,
            "source_error": None,
        }

    @property
    def snapshot(self) -> dict:
        """The current JSON-ready state (the ``/state`` payload)."""
        return self._snapshot

    def step(self) -> dict:
        """One poll cycle: poll, fold, rebuild the snapshot.

        NOTHING here may kill the poll loop or blank the page — that is
        the thread the whole draft rides on. A
        :class:`SleeperUnavailableError` (live outage with no cache) and
        any other exception (malformed feed data, a null draft object, a
        board without my slot) both keep the last good snapshot served,
        labeled with ``source_error`` so the page's red banner fires
        instead of freezing confident numbers unlabeled.
        """
        self._poll_count += 1
        try:
            tick = self._source.poll()
            self._absorb_names(tick)
            prev = self._board
            board = self._tracker.update(tick)
            self._prev_board, self._board = prev, board
            self._snapshot = self._build(board)
        except SleeperUnavailableError as error:
            self._label_failure(str(error))
        except Exception as error:  # pylint: disable=broad-exception-caught
            # The deliberate catch-all: an uncaught exception here kills
            # the daemon poll thread and the page then serves a frozen,
            # unlabeled state forever. Type + message go on the page.
            self._label_failure(f"{type(error).__name__}: {error}")
        return self._snapshot

    def _label_failure(self, message: str) -> None:
        """Keep the last good snapshot, labeled — never a blank page and
        never an unlabeled frozen one."""
        self._snapshot = {
            **self._snapshot,
            "poll_count": self._poll_count,
            "source_error": message,
        }

    def _absorb_names(self, tick: SourceTick) -> None:
        """Names/positions from pick metadata, for players off the sheet
        (the sheet's own name wins when both exist)."""
        for pick in tick.picks:
            parts = (pick.metadata.get("first_name"), pick.metadata.get("last_name"))
            name = " ".join(part for part in parts if part)
            if name:
                self._names.setdefault(pick.player_id, name)
            position = pick.metadata.get("position")
            if position:
                self._positions.setdefault(pick.player_id, str(position))

    def _build(self, board: BoardState) -> dict:
        my_slot = self._resolve_my_slot(board)
        return {
            "ok": True,
            "poll_count": self._poll_count,
            "updated_at": self._clock(),
            "source_error": None,
            "status": board.status,
            "paused": board.paused,
            "stale_endpoints": sorted(board.stale_endpoints),
            "settings_warnings": [
                {
                    "field": warning.field,
                    "expected": str(warning.expected),
                    "actual": str(warning.actual),
                }
                for warning in board.settings_warnings
            ],
            "timer_end_at": board.timer_end_at,
            "nomination": self._nomination_json(board, my_slot),
            "teams": [self._team_json(team, my_slot) for team in board.teams],
            "me": self._me_json(board, my_slot),
            "players": self._players_json(board),
            "sales": [self._sale_json(sale) for sale in board.sales],
            "off_model_player_ids": list(board.off_model_player_ids),
            "note": self._note,
        }

    def _resolve_my_slot(self, board: BoardState) -> int | None:
        if self._my_slot is not None:
            return self._my_slot
        for team in board.teams:
            if team.roster_id == self._config.my_roster_id:
                return team.slot
        return None

    def _nomination_json(self, board: BoardState, my_slot: int | None) -> dict:
        nomination = board.nomination
        player_id = nomination.player_id
        analysis, pre_sale, error = self._analysis_for(board, my_slot)
        verdict, reason = _verdict(
            nomination.status, board.paused, nomination.highest_offer, analysis, error
        )
        if verdict is not None and "draft" in board.stale_endpoints:
            # The draft object is the ONLY carrier of the nomination
            # pointer and high bid, and the disk cache has no age limit: a
            # cache-served draft can name an arbitrarily old lot at an
            # arbitrarily old price. A verdict computed from it would glow
            # green under the SERVED FROM CACHE banner. Fail closed.
            verdict, reason = None, (
                "draft object served from cache; nomination data may be "
                "arbitrarily old — not advising"
            )
        tier = (analysis or {}).get("tier")
        value = (analysis or {}).get("value")
        profit = None
        if nomination.highest_offer is not None and value is not None:
            # The decided sign: PRICE minus value, centered at $0 —
            # negative means the current bid sits under the value.
            profit = round(nomination.highest_offer - value, 2)
        return {
            "status": nomination.status,
            "is_live": nomination.is_live,
            "player_id": player_id,
            "name": self._names.get(player_id, player_id),
            "position": self._positions.get(player_id),
            "high_bid": nomination.highest_offer,
            "nominating_slot": nomination.nominating_slot,
            "offering_slot": nomination.offering_slot,
            "analysis": analysis,
            "analysis_error": error,
            "pre_sale": pre_sale,
            "verdict": verdict,
            "verdict_reason": reason,
            "profit": profit,
            "last_of_tier": bool(tier and tier["last_of_tier"]),
        }

    def _analysis_for(
        self, board: BoardState, my_slot: int | None
    ) -> tuple[dict | None, bool, str | None]:
        """The engine's record for the nominated player, as (analysis,
        pre_sale, error).

        A nominee already in the sold set is priced against the board as
        it stood BEFORE his sale folded in (the engine's own seam
        contract); the result is cached per player so later ticks of the
        same lull reuse it instead of repricing on a post-sale board. A
        sold nominee with no observed pre-sale board — the dashboard
        started mid-lull, or the observed tick carried other missed sales
        alongside his (so the previous board predates more than his own
        hammer) — honestly reports why instead of a wrong number. A live
        nomination reprices every step — the board under it can
        legitimately move.
        """
        player_id = board.nomination.player_id
        if player_id is None:
            self._sold_analysis = None
            return None, False, None
        sold = {sale.player_id for sale in board.sales}
        if player_id not in sold:
            self._sold_analysis = None
            analysis, error = self._run_engine(player_id, board, my_slot)
            return analysis, False, error
        if self._sold_analysis is not None and self._sold_analysis[0] == player_id:
            return (
                self._sold_analysis[1],
                self._sold_analysis[2],
                self._sold_analysis[3],
            )
        prev = self._prev_board
        prev_sold = {sale.player_id for sale in prev.sales} if prev else None
        if prev is None or player_id in prev_sold:
            error = (
                "sold before this dashboard observed a pre-sale board; "
                "not repricing a completed lot on post-sale money"
            )
            self._sold_analysis = (player_id, None, False, error)
            return None, False, error
        new_sales = sum(1 for sale in board.sales if sale.player_id not in prev_sold)
        if new_sales > 1:
            # A recovery tick: this poll revealed other sales alongside
            # the nominee's, so the previous board predates ALL of them —
            # not just his hammer. 'Priced pre-sale' off that board would
            # be a several-sales-old number wearing an honest label.
            error = (
                f"{new_sales} sales landed in one poll; the last observed "
                "board predates more than this lot's own sale — not "
                "pricing it on money that old"
            )
            self._sold_analysis = (player_id, None, False, error)
            return None, False, error
        analysis, error = self._run_engine(player_id, prev, my_slot)
        self._sold_analysis = (player_id, analysis, True, error)
        return analysis, True, error

    def _run_engine(
        self, player_id: str, board: BoardState, my_slot: int | None
    ) -> tuple[dict | None, str | None]:
        """One engine call, fail-closed: the keeper guard's ValueError (and
        an unresolvable my-slot) become an on-page error, never a crash
        and never a confident number."""
        try:
            record = analyze_player(
                player_id,
                self._rows,
                board,
                self._config,
                keepers_by_slot=self._keepers_by_slot,
                my_slot=my_slot,
            )
        except ValueError as error:
            return None, str(error)
        return self._analysis_json(record), None

    def _analysis_json(self, record: PlayerAnalysis) -> dict:
        row = self._rows_by_id.get(record.player_id)
        tier = None
        if record.tier is not None:
            tier = {
                "tier": record.tier.tier,
                "size": record.tier.size,
                "remaining": record.tier.remaining,
                "last_of_tier": record.tier.last_of_tier,
            }
        return {
            "rank": record.rank,
            "worth": record.worth,
            "room_price": row.room_price if row is not None else None,
            "keeper_premium": record.keeper_premium,
            "value": record.value,
            "inflation": record.inflation,
            "inflation_adjusted": record.inflation_adjusted,
            "marginal_worth": record.marginal_worth,
            "need_bump": record.need_bump,
            "spend_margin": record.spend_margin,
            "spend_boost": record.spend_boost,
            "spend_adjusted": record.spend_adjusted,
            "tier": tier,
            "my_cap": record.my_cap,
            "max_bid": record.max_bid,
        }

    def _team_json(self, team: TeamState, my_slot: int | None) -> dict:
        """One team's render row: remaining (never spent), open slots, max
        possible bid."""
        return {
            "slot": team.slot,
            "roster_id": team.roster_id,
            "remaining": team.remaining,
            "open_slots": team.open_slots,
            "max_bid": team.max_bid,
            "budget_is_default": team.budget_is_default,
            "needs": {label: count for label, count in team.needs.items() if count},
            "is_me": team.slot == my_slot,
        }

    def _me_json(self, board: BoardState, my_slot: int | None) -> dict | None:
        if my_slot is None:
            return None
        me = board.team(my_slot)
        roster = [
            {
                "player_id": player_id,
                "name": self._names.get(player_id, player_id),
                "position": self._positions.get(player_id),
                "price": None,
                "keeper": True,
            }
            for player_id in self._keepers_by_slot.get(my_slot, ())
        ]
        roster.extend(
            {
                "player_id": sale.player_id,
                "name": self._names.get(sale.player_id, sale.player_id),
                "position": self._positions.get(sale.player_id),
                "price": sale.amount,
                "keeper": False,
            }
            for sale in board.sales
            if sale.draft_slot == my_slot
        )
        return {
            "slot": my_slot,
            "remaining": me.remaining,
            "open_slots": me.open_slots,
            "max_bid": me.max_bid,
            "needs": {label: count for label, count in me.needs.items() if count},
            "roster": roster,
        }

    def _players_json(self, board: BoardState) -> list[dict]:
        """Still-buyable sheet rows in rank order: sold and kept players
        are off the table."""
        sold = {sale.player_id for sale in board.sales}
        kept = {
            player_id
            for slot in sorted(self._keepers_by_slot)
            for player_id in self._keepers_by_slot[slot]
        }
        return [
            {
                "rank": row.rank,
                "player_id": row.player_id,
                "name": row.name,
                "position": row.position,
                "worth": row.worth,
                "room_price": row.room_price,
                "value": row.value,
            }
            for row in sorted(self._rows, key=lambda row: row.rank)
            if row.player_id not in sold and row.player_id not in kept
        ]

    def _sale_json(self, sale: Sale) -> dict:
        return {
            "player_id": sale.player_id,
            "name": self._names.get(sale.player_id, sale.player_id),
            "position": self._positions.get(sale.player_id),
            "amount": sale.amount,
            "slot": sale.draft_slot,
        }
