"""Live draft-state tracking: team budgets, open slots, max bids, needs.

The tracker consumes :class:`~draftbot.sources.SourceTick` objects and is
deliberately blind to whether a live poll or a historical replay produced
them. Valuation and inflation math live elsewhere: a value sheet enters
only as an optional plain mapping, used to FLAG off-model sales.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from types import MappingProxyType

from draftbot.config import LeagueConfig
from draftbot.models import DraftState, Pick, to_int
from draftbot.sources import SourceTick

# The nomination pointer legitimately keeps naming the just-sold winner for
# about one nomination timer after a sale; longer than this and the feed is
# stuck, not merely between lots.
DEFAULT_GRACE_SECONDS = 10.0

NOMINATION_LIVE = "live"
NOMINATION_NONE = "none"
NOMINATION_SOLD_GRACE = "sold_between_lots"
NOMINATION_SOLD_STALE = "sold_stale"
# The picks feed behind the sold set is degraded or has regressed, so it
# cannot prove the lot is open: is_live stays False, whatever the pointer says.
NOMINATION_UNTRUSTED = "untrusted"

FLEX_ELIGIBLE = ("RB", "WR", "TE")

# Where a team's budget came from, in precedence order. These strings are
# printed in the settings banner: the same impossible figure must not be
# blamed on the ``--budget`` flag when Sleeper supplied it.
BUDGET_FROM_OVERRIDE = "hand-keyed with --budget"
BUDGET_FROM_SLEEPER = "Sleeper's own key"
BUDGET_FROM_DEFAULT = "the league-wide default"
# The fourth source, and the one this league actually uses: the
# commissioner entered every keeper as a PRICED PICK, so the keeper
# dollars arrive as spend rather than as a reduced budget_<slot>. A slot
# whose keepers are priced carries the WHOLE league budget by
# construction, and its post-keeper money is derived, not guessed.
BUDGET_FROM_KEEPER_PICKS = "the keeper prices in the picks feed"


def keeper_priced_slots(picks: Sequence[Pick]) -> frozenset[int]:
    """Draft slots whose keepers reached the feed as priced picks.

    A parsed dollar amount is the whole test. ``is_keeper`` alone says a
    slot kept somebody, not what it paid, and a keeper row with no amount
    (a truncated or malformed feed) leaves the slot's post-keeper money
    exactly as unknown as it was — which has to keep failing closed.
    """
    return frozenset(
        pick.draft_slot for pick in picks if pick.is_keeper and pick.amount is not None
    )


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
    # Dollars this team's KEEPER picks took out of ``spent``. Zero on a
    # board whose keepers arrived as a reduced budget_<slot> (there the
    # budget is already post-keeper), and the keeper spend on a board
    # whose keepers arrived priced. The inflation model subtracts it to
    # reach the same discretionary figure from either shape.
    keeper_spend: int = 0

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

    ``is_live`` is true only for a nominee absent from the sold set AND
    observed through a trusted picks feed. A sold nominee is
    ``sold_between_lots`` within the post-sale grace window and
    ``sold_stale`` beyond it; an unsold-looking nominee behind a degraded
    or regressed picks feed is ``untrusted`` — in no case do any of these
    render as live.

    ``nominating_slot`` is the slot that NOMINATED the player;
    ``offering_slot`` is the slot holding the current HIGH BID (two
    different teams on the wire).
    """

    player_id: str | None
    is_live: bool
    status: str
    highest_offer: int | None = None
    nominating_slot: int | None = None
    offering_slot: int | None = None


@dataclass(frozen=True)
class SettingsMismatch:
    """One live draft setting that differs from a valuation assumption."""

    field: str
    expected: object
    actual: object


def default_expected_settings(config: LeagueConfig) -> dict[str, object]:
    """The assumptions the config encodes, in ``diff_settings`` form.

    The timer expectations come from the config's ``[auction]`` keys (the
    keys carry Sleeper's own settings names), so every consumer states the
    same league facts without merging literals by hand.
    """
    return {
        "type": "auction",
        "teams": config.teams,
        "budget": config.auction_budget,
        "nomination_timer": config.nomination_timer,
        "pick_timer": config.pick_timer,
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


def _ceiling_warnings(breaches: Sequence[tuple[int, str]]) -> list[SettingsMismatch]:
    """The IMPOSSIBLE banner over ``DraftTracker._ceiling_breaches``.

    One banner naming every breaching slot, never one per slot: the whole
    point of the ceiling rule is that impossible money is rare and loud,
    and a column of near-identical banners is how loud becomes wallpaper.
    """
    if not breaches:
        return []
    return [
        SettingsMismatch(
            field="budget_ceiling",
            expected="a keeper team's budget cannot exceed its ceiling",
            actual="; ".join(message for _, message in breaches),
        )
    ]


@dataclass(frozen=True)
class Sale:
    """One completed sale as the board carries it: id, price, buying slot,
    and whether it was a keeper — enough for issue #5 to compute the
    remaining player pool and on-model dollars from the board alone.

    ``is_keeper`` rides along because a keeper's price is a chain price
    (prior bid plus the increment, floored), not a market-clearing bid,
    and the consumer that has to leave it out of the inflation ratio sees
    only the board.
    """

    player_id: str
    amount: int | None
    draft_slot: int
    is_keeper: bool = False


@dataclass(frozen=True)
class BoardState:
    """Everything the tracker knows after one tick."""

    # pylint: disable=too-many-instance-attributes  # derived record type:
    # the board IS the consumer surface; one field per thing issues #5-#7
    # read from it.

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
    # Every sale in feed order (off_model_player_ids is the flagged subset).
    sales: tuple[Sale, ...] = ()
    timer_end_at: int | None = None
    # Keeper slots the ceiling banner just proved impossible, in slot
    # order. Provenance alone cannot carry this: a ceiling-breaching
    # Sleeper key IS a real key, so budget_is_default reads False and the
    # render layer would paint proven-wrong money like correct money.
    impossible_keeper_slots: tuple[int, ...] = ()
    # True once the draft order has moved out from under the keeper lists
    # this tracker was built with, which puts every keeper-derived figure
    # (roster panel, needs, open slots, max bid) on the wrong team. It
    # rides the board rather than being re-derived from the banner's
    # wording, so the banner and the suppressed verdict cannot disagree.
    keeper_map_stale: bool = False

    def team(self, slot: int) -> TeamState:
        """The team drafting from ``slot``."""
        for team in self.teams:
            if team.slot == slot:
                return team
        raise KeyError(f"no team in draft slot {slot}")

    def is_timer_expired(self, now_ms: int) -> bool:
        """Whether the bid/nomination timer has run out, honoring the
        pause guard (mirrors ``DraftState.is_timer_expired``: a paused
        draft freezes ``timer_end_at`` in the past and NEVER reads as
        expired)."""
        if self.paused or self.timer_end_at is None:
            return False
        return now_ms >= self.timer_end_at


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
        keeper_slot_map: Mapping[int, int] | None = None,
        player_positions: Mapping[str, str] | None = None,
        expected_settings: Mapping[str, object] | None = None,
        value_sheet: Mapping[str, float] | None = None,
        budget_overrides: Mapping[int, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
    ):
        self._config = config
        self._player_positions = dict(player_positions or {})
        self._expected_settings = dict(expected_settings or {})
        self._value_sheet = None if value_sheet is None else dict(value_sheet)
        # Hand-keyed real budgets, by DRAFT SLOT. These OUTRANK Sleeper's
        # own budget_<slot> keys: the operator can read the league sheet,
        # and a commissioner who enters a flat league default for twelve
        # keeper teams produces wrong money that no lever could correct
        # if the remote value won. Discarding a figure Sleeper actually
        # supplied raises its own banner, so the trade is never silent.
        self._budget_overrides = dict(budget_overrides or {})
        self._keepers_by_slot = {
            slot: tuple(players) for slot, players in (keepers_by_slot or {}).items()
        }
        # The slot-to-roster map ``keepers_by_slot`` was bridged through.
        # Rosters carry keepers by ROSTER ID and this mapping is keyed by
        # DRAFT SLOT, so it is only true of the order in force when it was
        # built — and this league deals that order at draft time, often
        # while the dashboard is already up. None declares no bridge and
        # turns the staleness check off: a hand-built keeper mapping owns
        # its own slots and has no startup map to fall behind.
        self._keeper_slot_map = (
            None if keeper_slot_map is None else dict(keeper_slot_map)
        )
        self._clock = clock
        self._grace_seconds = grace_seconds
        # Cross-tick watermark: the most picks this tracker has ever seen.
        # Times grace off each observed sale, and marks any SHORTER feed
        # as regressed (it is missing sales we know happened).
        self._observed_sales = 0
        self._last_sale_at: float | None = None

    @property
    def budget_overrides(self) -> Mapping[int, int]:
        """The hand-keyed ``--budget`` figures by DRAFT slot, read-only.

        Exposed because the render layer has to be able to say "an
        override was given and none of it landed on my slot" on every
        poll, and it is the layer that knows which slot is mine. Handing
        it a second copy of the same mapping is exactly how the two would
        drift apart.
        """
        return MappingProxyType(self._budget_overrides)

    def update(self, tick: SourceTick) -> BoardState:
        """Fold one observation of the draft into a fresh board."""
        if len(tick.picks) > self._observed_sales:
            self._observed_sales = len(tick.picks)
            self._last_sale_at = self._clock()
        # The sold-set guard would fail open ACROSS ticks if the picks
        # feed slipped: a cache-served ("picks" stale) or regressed
        # (shorter than the watermark) feed can be missing a recent sale,
        # so it can never prove a lot is open.
        picks_trusted = (
            "picks" not in tick.stale_endpoints
            and len(tick.picks) >= self._observed_sales
        )
        sold = {pick.player_id for pick in tick.picks}
        # Derived once per tick and passed down rather than re-derived by
        # each rule: the budget a team shows, the banner that would call
        # it guessed, and the ceiling that would call it impossible all
        # have to agree about which slots the feed prices.
        priced = keeper_priced_slots(tick.picks)
        # Computed once and used twice, so the banner's wording and the
        # page's marks can never name different slots.
        breaches = self._ceiling_breaches(tick.draft, priced)
        # Same discipline: one evaluation feeds both the RESTART banner and
        # the flag the verdict fails closed on.
        keeper_map_stale = self._keeper_map_is_stale(tick.draft)
        return BoardState(
            status=tick.draft.status,
            paused=tick.draft.paused,
            teams=tuple(
                self._team_state(slot, tick, priced) for slot in self._slots(tick)
            ),
            nomination=self._resolve_nomination(tick.draft, sold, picks_trusted),
            settings_warnings=self._settings_warnings(
                tick.draft, breaches, keeper_map_stale, priced
            ),
            impossible_keeper_slots=tuple(slot for slot, _ in breaches),
            keeper_map_stale=keeper_map_stale,
            off_model_player_ids=self._off_model(tick),
            stale_endpoints=tick.stale_endpoints,
            sales=tuple(
                Sale(pick.player_id, pick.amount, pick.draft_slot, pick.is_keeper)
                for pick in tick.picks
            ),
            timer_end_at=tick.draft.timer_end_at,
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

    def _settings_warnings(
        self,
        draft: DraftState,
        breaches: Sequence[tuple[int, str]],
        keeper_map_stale: bool,
        priced: AbstractSet[int] = frozenset(),
    ) -> tuple[SettingsMismatch, ...]:
        """Assumption diffs, the pre-entry keeper condition, a budget
        figure that fights the keeper prices already in the feed, the real
        budget a hand-keyed ``--budget`` override discards, money no
        keeper roster could have left over, and a keeper bridge the draft
        order has moved out from under — none of which the operator should
        have to discover from a wrong number.

        The stale-bridge banner leads. Every other banner here describes a
        number that is wrong; that one says the whole page is describing
        the wrong teams, and it is the only one whose remedy is an action
        the operator has to take.
        """
        warnings = self._keeper_map_warnings(draft) if keeper_map_stale else []
        warnings.extend(diff_settings(draft, self._expected_settings))
        warnings.extend(self._keeper_budget_warnings(draft, priced))
        warnings.extend(self._keeper_price_conflicts(draft, priced))
        warnings.extend(self._override_warnings(draft, priced))
        warnings.extend(_ceiling_warnings(breaches))
        return tuple(warnings)

    def _keeper_map_is_stale(self, draft: DraftState) -> bool:
        """Whether the draft order has moved since the keeper lists were
        bridged onto draft slots.

        ``keepers_by_slot`` is built once, at startup, from the rosters'
        roster-id-keyed keeper lists and whatever ``slot_to_roster_id`` the
        draft carried at that instant. Nothing re-fetches the rosters, so
        when the commissioner deals the order the two halves of the board
        stop describing the same team: the render layer re-resolves my slot
        from the live map every tick, while my keepers, needs, open slots
        and the max bid computed from them stay on the seat the old order
        gave them.

        The full repair is to hold the keeper lists by ROSTER ID and
        re-derive the slot mapping every tick, which re-plumbs this seam
        and is issue #23. This is the fail-closed stopgap in front of it:
        on a board whose numbers belong to another team, silence is the one
        answer that cannot be right.

        An EMPTY bridge has nothing to fall behind. Every figure this rule
        protects is slot-keyed and recomputed each tick, so on a board
        that carries no keeper at all the deal moves my slot and nothing
        else; blanking the tool there would cost the verdict and buy
        nothing.
        """
        if self._keeper_slot_map is None:
            return False
        if not any(self._keepers_by_slot.values()):
            return False
        return dict(draft.slot_to_roster_id) != self._keeper_slot_map

    def _keeper_map_warnings(self, draft: DraftState) -> list[SettingsMismatch]:
        """The RESTART banner, naming the slots that moved.

        Nothing on the page can be re-keyed to fix this and no flag
        corrects it, so the banner asks for the one thing that does: a
        restart, which rebuilds the bridge against the order now in force.
        """
        frozen = self._keeper_slot_map or {}
        live = dict(draft.slot_to_roster_id)
        moved = sorted(
            slot
            for slot in set(frozen) | set(live)
            if frozen.get(slot) != live.get(slot)
        )
        return [
            SettingsMismatch(
                field="keeper_map_stale",
                expected=(
                    "the draft order this dashboard's keeper lists were "
                    "built against at startup"
                ),
                actual=(
                    "the order changed while the dashboard was running "
                    f"(draft slots {', '.join(str(slot) for slot in moved)} "
                    "now seat different rosters), so every keeper list, "
                    "roster panel, need count and open-slot count still "
                    "sits on the seat the old order gave it — RESTART the "
                    "dashboard to rebuild them; the verdict stays withheld "
                    "until you do"
                ),
            )
        ]

    def uncovered_keeper_slots(
        self, draft: DraftState, priced: AbstractSet[int] = frozenset()
    ) -> tuple[int, ...]:
        """Keeper-holding DRAFT SLOTS showing the league-wide default
        because NO source carries real money for them, in slot order.

        This is the set whose money is provably wrong rather than merely
        unentered: a team that kept players paid for them, so the full
        league budget cannot be its post-keeper figure. A keeper-free
        slot at the default is unverified, not wrong, which is why the
        banner and the page's room mark both key on this set and not on
        every defaulted slot.

        ``priced`` is the third source and covers a slot exactly as the
        other two do. Its slots DO show the full league budget, and that
        is not the fiction this set exists to name: the keeper dollars
        are in ``spent`` instead, so the post-keeper figure on screen is
        derived from prices the feed actually carries.
        """
        return tuple(
            sorted(
                slot
                for slot, players in self._keepers_by_slot.items()
                if players
                and slot not in draft.budget_by_slot
                and slot not in self._budget_overrides
                and slot not in priced
            )
        )

    def _keeper_budget_warnings(
        self, draft: DraftState, priced: AbstractSet[int] = frozenset()
    ) -> list[SettingsMismatch]:
        """The pre-entry keeper condition: a keeper team with no real
        budget from EITHER source shows the config default, which
        overstates what it can spend. The warning holds while ANY
        keeper-holding slot is uncovered — the commissioner enters
        budgets one team at a time, and the first entry must not clear
        the banner for the rest of the room.

        A hand-keyed override covers its slot here exactly as a Sleeper
        key does. This banner names the money the page is SHOWING, and an
        overridden slot shows a real number; listing it anyway would put
        'shown at the $200 default' on screen beside a correct figure.
        """
        missing = self.uncovered_keeper_slots(draft, priced)
        if not missing:
            return []
        return [
            SettingsMismatch(
                field="keeper_budgets",
                expected="budget_<slot> entered for every keeper team",
                actual=(
                    f"missing for {len(missing)} keeper team(s) "
                    f"(slots {', '.join(str(slot) for slot in missing)}); "
                    f"shown at the ${self._config.auction_budget} default"
                ),
            )
        ]

    def _keeper_price_conflicts(
        self, draft: DraftState, priced: AbstractSet[int]
    ) -> list[SettingsMismatch]:
        """The REFUSED banner: a budget figure entered for a slot whose
        keepers the feed already prices.

        Both other sources carry the money a team has for the WHOLE draft,
        which on a keeper board is the POST-keeper figure off the league
        sheet. When the keepers arrive priced, those same dollars are
        already in ``spent``, so honoring the entered figure would subtract
        them twice and can drive a team's remaining money NEGATIVE. The
        measured prices win — they are observation, not transcription —
        and the discarded figure is named by slot and by source, because a
        lever that silently does nothing is worse than one that refuses.
        """
        discarded = []
        for slot in sorted(priced):
            if slot in self._budget_overrides:
                amount = self._budget_overrides[slot]
                discarded.append(f"slot {slot} = ${amount} ({BUDGET_FROM_OVERRIDE})")
            elif slot in draft.budget_by_slot:
                amount = draft.budget_by_slot[slot]
                discarded.append(
                    f"slot {slot} = ${amount} (budget_{slot}, {BUDGET_FROM_SLEEPER})"
                )
        if not discarded:
            return []
        return [
            SettingsMismatch(
                field="budget_keeper_conflict",
                expected=(
                    "one source of keeper money per slot: either a "
                    "post-keeper budget figure or priced keeper picks"
                ),
                actual=(
                    f"{'; '.join(discarded)} — these slots' keepers are "
                    "already priced in the picks feed and counted as spend, "
                    "so the entered figure would subtract the same keeper "
                    "dollars twice; DISCARDED, and the slots show the "
                    f"${self._config.auction_budget} league budget against "
                    "their measured keeper spend"
                ),
            )
        ]

    def _override_warnings(
        self, draft: DraftState, priced: AbstractSet[int] = frozenset()
    ) -> list[SettingsMismatch]:
        """The REPLACED banner: an override outranks a ``budget_<slot>``
        key, so it can discard real server data.

        Filling a hole is the ordinary case and stays quiet; replacing a
        figure Sleeper actually supplied is not, and the operator has to
        be able to tell the two apart — otherwise the flip trades one
        invisible failure for another.
        """
        kept, discarded = [], []
        for slot in sorted(self._budget_overrides):
            if slot in priced:
                # Neither figure is shown on a priced slot: the keeper
                # conflict banner one line up already said so, and
                # claiming "$96 shown" here would contradict it.
                continue
            amount = self._budget_overrides[slot]
            live = draft.budget_by_slot.get(slot)
            if live is not None and live != amount:
                kept.append(f"slot {slot} = ${amount}")
                discarded.append(f"slot {slot} = ${live}")
        if not kept:
            return []
        return [
            SettingsMismatch(
                field="budget_override",
                expected=f"{', '.join(kept)} (hand-keyed with --budget, shown)",
                actual=f"{', '.join(discarded)} (Sleeper's own key, discarded)",
            )
        ]

    def _resolved_budget(
        self, slot: int, draft: DraftState, priced: AbstractSet[int] = frozenset()
    ) -> tuple[int, str]:
        """One slot's budget and where it came from.

        The single statement of the precedence: priced keeper picks first,
        then explicit operator input, then the remote value, and ONLY then
        the league-wide default — which is a fiction on a keeper board, so
        the provenance travels with the number rather than being
        re-derived by each caller. ``TeamState.budget_is_default`` and the
        ceiling banner both read this, and a page that marks a figure the
        banner does not name (or the reverse) is exactly the disagreement
        that keeps them in one place.

        Priced keeper picks lead DESPITE outranking the operator's own
        lever, which every other rule here refuses to do. They are not
        another opinion about the same quantity: the other three sources
        all name a team's money for the WHOLE draft, which on a keeper
        board is the post-keeper figure, while priced picks put the keeper
        dollars in ``spent`` and leave the budget at the full league
        amount. Taking the entered figure on top of them subtracts the
        keeper money twice and can show a team a negative remaining. The
        refusal is never silent — it raises its own banner naming the slot
        and the source — and the number it keeps is measured rather than
        transcribed. The amount here is the same integer as the default;
        the SOURCE is the whole difference, because it is what the verdict
        fails closed on.
        """
        if slot in priced:
            return self._config.auction_budget, BUDGET_FROM_KEEPER_PICKS
        if slot in self._budget_overrides:
            return self._budget_overrides[slot], BUDGET_FROM_OVERRIDE
        if slot in draft.budget_by_slot:
            return draft.budget_by_slot[slot], BUDGET_FROM_SLEEPER
        return self._config.auction_budget, BUDGET_FROM_DEFAULT

    def _ceiling_breaches(
        self, draft: DraftState, priced: AbstractSet[int] = frozenset()
    ) -> list[tuple[int, str]]:
        """Every keeper slot whose budget is IMPOSSIBLE, as (slot,
        message) in slot order: provably wrong, not merely unverified.

        Keeper cost is bounded below by the config's floor, so a slot
        holding N keepers cannot carry more than ``budget - floor * N`` of
        post-keeper money. This reads the RESOLVED budget, not only the
        hand-keyed ones: the failure this whole rule exists for is a
        commissioner typing one flat league budget into all twelve boxes,
        which leaves every key present, clears the keeper_budgets banner,
        and renders impossible money with nothing marked. The same figure
        typed with ``--budget`` already raised a banner, and the same
        money must not give opposite signals depending on who typed it —
        so the message names the source instead.

        The carve-out: a slot the keeper_budgets banner ALREADY names
        (keeper team, no real money from any source, showing the league
        default) is skipped. Its default can breach the same ceiling, but
        that is one hole, and reporting it twice is how a banner becomes
        wallpaper.

        The second carve-out, and the reason it is not a weakening: a slot
        whose keepers the feed PRICES has nothing left to infer. The whole
        rule is a lower bound on money that must already be gone, drawn
        from the floor because the real keeper cost is unreadable. On a
        priced slot it is readable — the dollars are in ``spent``, and the
        budget is the full league amount by construction, so ``budget >
        budget - floor * N`` holds for every such slot and would fire the
        IMPOSSIBLE banner on all twelve teams of a correctly-entered
        board.

        The CLI cannot make this check — it parses before the network on
        purpose, and keeper lists arrive with the rosters — so it lands
        here, where both facts are in hand.

        The slots travel out beside the message because the banner is not
        enough on its own: the same figure it calls impossible carries a
        real provenance, so every mark on the page reads it as confident
        money unless this set says otherwise.
        """
        floor = self._config.keeper_cost_floor
        uncovered = set(self.uncovered_keeper_slots(draft, priced))
        breaches = []
        for slot, keepers in sorted(self._keepers_by_slot.items()):
            if not keepers or slot in priced:
                continue
            amount, source = self._resolved_budget(slot, draft, priced)
            if slot in uncovered:
                # THE CARVE-OUT. This slot's money is the league default,
                # which breaches the ceiling on any keeper roster — but
                # the keeper_budgets banner already names it, by slot,
                # and provenance already marks it. One hole, one banner.
                continue
            ceiling = self._config.auction_budget - floor * len(keepers)
            if amount > ceiling:
                breaches.append(
                    (
                        slot,
                        f"slot {slot} = ${amount} ({source}) against a "
                        f"ceiling of ${ceiling} ({len(keepers)} keeper(s) "
                        f"at the ${floor} floor)",
                    )
                )
        return breaches

    def _resolve_nomination(
        self, draft: DraftState, sold: set[str], picks_trusted: bool
    ) -> NominationView:
        """Rule on the nomination pointer against the FULL sold set — the
        pointer can lag more than one lot, so comparing against only the
        latest pick's winner is exactly the lazy guard this forbids. And
        the sold set is only as trustworthy as the picks feed behind it:
        a degraded or regressed feed can be missing a recent sale, so it
        never rules a lot LIVE (a fresh guard, deliberately, rather than a
        cumulative sold set — that would misread a commissioner pick-undo
        as a still-sold player)."""
        nominee = draft.nominated_player_id
        if not nominee:
            return NominationView(None, False, NOMINATION_NONE)
        offer = to_int(draft.highest_offer)
        nominator = to_int(draft.nominating_slot)
        bidder = to_int(draft.offering_slot)
        if nominee in sold:
            in_grace = (
                self._last_sale_at is not None
                # Inclusive boundary by intent: elapsed == grace_seconds
                # is the last instant of the normal between-lots lull.
                and self._clock() - self._last_sale_at <= self._grace_seconds
            )
            status = NOMINATION_SOLD_GRACE if in_grace else NOMINATION_SOLD_STALE
            return NominationView(nominee, False, status, offer, nominator, bidder)
        if not picks_trusted:
            return NominationView(
                nominee, False, NOMINATION_UNTRUSTED, offer, nominator, bidder
            )
        return NominationView(nominee, True, NOMINATION_LIVE, offer, nominator, bidder)

    def _slots(self, tick: SourceTick) -> list[int]:
        if tick.draft.slot_to_roster_id:
            return sorted(tick.draft.slot_to_roster_id)
        return list(range(1, self._config.teams + 1))

    def _team_state(
        self, slot: int, tick: SourceTick, priced: AbstractSet[int] = frozenset()
    ) -> TeamState:
        """One team's standing, with every kept player counted ONCE.

        This season's keepers are reachable from two sources at the same
        time — the roster's ``keepers`` array and the picks feed — and the
        same player through both is still one player on one roster slot.
        Taking the union rather than either source alone is what makes
        that true from both directions: a keeper the feed carries and the
        rosters do not still fills a slot, and so does the reverse.

        Keeper dollars stay in ``spent`` (that is how the post-keeper
        money derives at all) but keeper picks are NOT purchases: they
        never occupied an auction lot, and counting them as one is what
        put ``open_slots`` three low and the max bid three high.
        """
        picks = [pick for pick in tick.picks if pick.draft_slot == slot]
        keeper_picks = [pick for pick in picks if pick.is_keeper]
        purchases = [pick for pick in picks if not pick.is_keeper]
        kept = list(self._keepers_by_slot.get(slot, ()))
        keeper_positions = {}
        for pick in keeper_picks:
            keeper_positions[pick.player_id] = pick.metadata.get("position")
            if pick.player_id not in kept:
                kept.append(pick.player_id)
        # The default is a fiction on a keeper board, so the flag it
        # raises must survive all the way to the page and the verdict.
        # An override that merely fills a hole is the ordinary case; one
        # that discards a real budget_<slot>, or that fights keeper prices
        # already in the feed, is named by slot in the standing banner.
        budget, source = self._resolved_budget(slot, tick.draft, priced)
        open_slots = max(0, self._config.drafted_slots - len(kept) - len(purchases))
        positions = [
            keeper_positions.get(player_id) or self._player_positions.get(player_id)
            for player_id in kept
        ]
        positions.extend(
            pick.metadata.get("position") or self._player_positions.get(pick.player_id)
            for pick in purchases
        )
        return TeamState(
            slot=slot,
            roster_id=tick.draft.slot_to_roster_id.get(slot),
            budget=budget,
            budget_is_default=source == BUDGET_FROM_DEFAULT,
            spent=sum(pick.amount or 0 for pick in picks),
            keeper_count=len(kept),
            purchase_count=len(purchases),
            open_slots=open_slots,
            # Read-only proxy: a consumer decrementing needs in place would
            # corrupt every later reader of the same board.
            needs=MappingProxyType(self._compute_needs(positions)),
            keeper_spend=sum(pick.amount or 0 for pick in keeper_picks),
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
