"""Live draft state: budgets, open slots, max bids, needs, and the guards.

Every test drives the tracker through the same ``SourceTick`` seam the real
sources produce, so nothing here can depend on which source is attached.
"""

from __future__ import annotations

import pytest

from draftbot.models import parse_draft, parse_picks
from draftbot.sources import SourceTick
from draftbot.tracker import (
    DraftTracker,
    Sale,
    default_expected_settings,
    keepers_by_slot_from_rosters,
)

from .conftest import raw_auction_pick

# The map a draft carries before its order is dealt (slot N = roster N),
# and the permuted one this league deals over it. Two different maps are
# what the keeper bridge falls behind.
_PLACEHOLDER_SLOTS = {slot: slot for slot in range(1, 13)}
_DEALT_SLOTS = {**_PLACEHOLDER_SLOTS, 4: 7, 7: 11, 11: 4}


def _tick(  # pylint: disable=too-many-arguments  # one keyword knob per
    # draft-object field a test needs to vary; a builder object adds nothing.
    *,
    picks: list[dict] | None = None,
    settings: dict | None = None,
    metadata: dict | None = None,
    status: str = "drafting",
    draft_type: str = "auction",
    stale: frozenset[str] = frozenset(),
    slots: dict[int, int] | None = None,
) -> SourceTick:
    raw_draft = {
        "draft_id": "1389692302259138561",
        "status": status,
        "type": draft_type,
        "settings": {"budget": 200, "teams": 12} | (settings or {}),
        "metadata": metadata or {},
        "slot_to_roster_id": {
            str(slot): roster_id
            for slot, roster_id in (slots or _PLACEHOLDER_SLOTS).items()
        },
    }
    return SourceTick(
        draft=parse_draft(raw_draft),
        picks=tuple(parse_picks(picks or [])),
        stale_endpoints=stale,
    )


def test_max_bid_reserves_a_dollar_for_every_other_open_slot(config):
    """Anti-cheat: budget 100, 8 keepers, 2 buys for $50 leaves 5 open slots
    and $50; the max bid is $46. Reserving nothing says $50; reserving $1
    for ALL open slots says $45. Both are wrong."""
    tracker = DraftTracker(
        config, keepers_by_slot={5: tuple(f"K{i}" for i in range(8))}
    )
    board = tracker.update(
        _tick(
            settings={"budget_5": 100},
            picks=[
                raw_auction_pick(1, "A", 5, "20"),
                raw_auction_pick(2, "B", 5, "30"),
            ],
        )
    )

    team = board.team(5)
    assert team.budget == 100
    assert team.spent == 50
    assert team.remaining == 50
    assert team.open_slots == 5
    assert team.max_bid == 46


def test_max_bid_edges_last_slot_gets_everything_full_roster_gets_zero(config):
    """With one open slot the whole remaining budget is biddable; with a
    full roster the team cannot bid at all."""
    tracker = DraftTracker(
        config, keepers_by_slot={3: tuple(f"K{i}" for i in range(14))}
    )
    one_open = tracker.update(_tick(settings={"budget_3": 37})).team(3)
    assert one_open.open_slots == 1
    assert one_open.max_bid == 37

    full = DraftTracker(config, keepers_by_slot={3: tuple(f"K{i}" for i in range(15))})
    no_open = full.update(_tick(settings={"budget_3": 37})).team(3)
    assert no_open.open_slots == 0
    assert no_open.max_bid == 0


def test_budgets_seed_from_budget_slot_and_fall_back_to_config_default(config):
    """budget_<slot> is authoritative when present; without it the team is
    seeded at the config default and labeled as defaulted."""
    board = DraftTracker(config).update(_tick(settings={"budget_5": 96}))

    entered = board.team(5)
    assert entered.budget == 96
    assert entered.budget_is_default is False

    defaulted = board.team(4)
    assert defaulted.budget == 200
    assert defaulted.budget_is_default is True
    assert defaulted.open_slots == 15  # no keepers known, nothing bought


def test_manual_budget_override_fills_a_slot_sleeper_never_carried(config):
    """The operator's `--budget SLOT=AMOUNT` lever, keyed from the league
    sheet when the commissioner never enters budget_<slot>.

    Anti-cheat on all three sides at once: slot 5 is overridden and must
    report the OVERRIDE ($96, not the $200 default and not defaulted),
    slot 4 is untouched and must still report the default AND still be
    labeled defaulted (a blanket clear of the flag fails here), and the
    override must move the real spendable numbers, not just the flag —
    $96 across 15 open slots is a max bid of $82, where the default
    would say $186."""
    tracker = DraftTracker(config, budget_overrides={5: 96})
    board = tracker.update(_tick())

    overridden = board.team(5)
    assert overridden.budget == 96
    assert overridden.budget_is_default is False
    assert overridden.remaining == 96
    assert overridden.max_bid == 82  # 96 - 14 held for the other open slots

    untouched = board.team(4)
    assert untouched.budget == 200
    assert untouched.budget_is_default is True
    assert untouched.max_bid == 186


def test_keeper_budget_banner_counts_an_override_as_covered(config):
    """The banner names the money the page is SHOWING. An overridden slot
    shows a real number, so listing it under 'shown at the $200 default'
    would be a false statement next to a correct figure — while a keeper
    slot covered by neither source keeps the banner up for the room."""
    keepers = {3: ("K1",), 5: ("K2",)}
    both_missing = DraftTracker(config, keepers_by_slot=keepers).update(_tick())
    (warning,) = [
        w for w in both_missing.settings_warnings if w.field == "keeper_budgets"
    ]
    assert "slots 3, 5" in str(warning.actual)

    one_covered = DraftTracker(
        config, keepers_by_slot=keepers, budget_overrides={5: 96}
    ).update(_tick())
    (warning,) = [
        w for w in one_covered.settings_warnings if w.field == "keeper_budgets"
    ]
    assert "1 keeper team(s) (slots 3)" in str(warning.actual)

    all_covered = DraftTracker(
        config, keepers_by_slot=keepers, budget_overrides={3: 143, 5: 96}
    ).update(_tick())
    assert not [w for w in all_covered.settings_warnings if w.field == "keeper_budgets"]


def test_a_manual_override_outranks_a_sleeper_budget_key(config):
    """Precedence, pinned — and DELIBERATELY REVERSED from what this test
    asserted when it was first written.

    It used to pin Sleeper-first: a live budget_<slot> key beat a
    hand-keyed override, on the reasoning that server data is fresher
    than a number typed at the command line. That reasoning does not
    survive the case the lever exists for. A commissioner who enters the
    flat league budget for twelve keeper teams produces twelve real keys
    carrying fiction: nothing is flagged, budget_is_default reads False
    everywhere, and under Sleeper-first the operator's override is
    discarded silently, leaving him no lever at all. The recoverable
    direction is the other one — a stale override costs a few dollars on
    one team and the operator can drop the flag.

    So: explicit operator input, then the remote value, then the default.
    $96 here, not $143. The discard raises its own banner (pinned in
    test_an_override_that_discards_a_real_budget_says_so), so trading the
    invisible remote failure for an invisible local one is ruled out."""
    tracker = DraftTracker(config, budget_overrides={5: 96})
    board = tracker.update(_tick(settings={"budget_5": 143}))

    team = board.team(5)
    assert team.budget == 96
    assert team.budget_is_default is False
    # The untouched slots still read the key Sleeper carries for them.
    assert tracker.update(_tick(settings={"budget_4": 143})).team(4).budget == 143


def test_an_override_that_discards_a_real_budget_says_so(config):
    """The condition attached to the precedence flip. Filling a hole and
    discarding real server data are different acts, and the operator has
    to be able to tell them apart, so only the second raises a banner —
    named by slot, carrying BOTH amounts so he can see what he replaced.

    Anti-cheat: the equal case is not a discard. An override that agrees
    with the key Sleeper carries changes nothing, and a banner there
    would be noise the operator learns to ignore."""
    keyed = DraftTracker(config, budget_overrides={5: 96}).update(
        _tick(settings={"budget_5": 143})
    )
    (warning,) = [w for w in keyed.settings_warnings if w.field == "budget_override"]
    assert "slot 5 = $96" in str(warning.expected)
    assert "slot 5 = $143" in str(warning.actual)

    hole = DraftTracker(config, budget_overrides={5: 96}).update(_tick())
    assert not [w for w in hole.settings_warnings if w.field == "budget_override"]

    agrees = DraftTracker(config, budget_overrides={5: 143}).update(
        _tick(settings={"budget_5": 143})
    )
    assert not [w for w in agrees.settings_warnings if w.field == "budget_override"]


def test_a_budget_a_keeper_roster_cannot_afford_raises_a_banner(config):
    """`--budget 5=200` on a slot holding three keepers is not merely
    unverified, it is provably impossible: keeper cost is bounded below
    by the $5 floor, so three keepers leave at most $185 of post-keeper
    money. Left unflagged it is the pre-fix wrong number ($200 where the
    truth is at most $185) wearing the fix's all-clear — the banner it
    would otherwise have raised is silenced by the override.

    Anti-cheat on both boundaries: $185 is exactly reachable and must
    stay quiet, and a keeper-free slot has no ceiling to breach at all,
    so $200 there is unverified rather than wrong."""
    keepers = {5: ("K1", "K2", "K3")}
    over = DraftTracker(
        config, keepers_by_slot=keepers, budget_overrides={5: 200}
    ).update(_tick())
    (warning,) = [w for w in over.settings_warnings if w.field == "budget_ceiling"]
    assert "slot 5 = $200" in str(warning.actual)
    assert "$185" in str(warning.actual)
    assert "hand-keyed with --budget" in str(warning.actual)

    exact = DraftTracker(
        config, keepers_by_slot=keepers, budget_overrides={5: 185}
    ).update(_tick())
    assert not [w for w in exact.settings_warnings if w.field == "budget_ceiling"]

    keeper_free = DraftTracker(config, budget_overrides={5: 200}).update(_tick())
    assert not [w for w in keeper_free.settings_warnings if w.field == "budget_ceiling"]


def test_the_ceiling_reads_sleepers_own_key_too_but_not_the_default(config):
    """The silent variant the whole fail-closed budget rule exists for.

    A commissioner who types a flat $200 into all twelve boxes leaves
    every ``budget_<slot>`` key present, so ``budget_is_default`` reads
    False, the keeper_budgets banner clears, and wrong money renders with
    no mark at all. The same impossible figure typed as `--budget 5=200`
    raises a banner. Same money, same board, opposite signal — so the
    ceiling test runs on the RESOLVED per-slot budget and names where the
    figure came from.

    ANTI-CHEAT on the carve-out, both directions on ONE board. Slot 5
    holds three keepers and carries a real $200 key: provably impossible,
    banner. Slot 6 holds three keepers and carries NO key from either
    source, so its resolved budget is the same $200 — and it must stay
    out of this banner, because the keeper_budgets banner already names
    it and double-reporting one hole as two faults is how a banner
    becomes wallpaper. An implementation that simply prices every keeper
    slot's resolved budget reports slot 6 as well and fails here."""
    keepers = {5: ("K1", "K2", "K3"), 6: ("K4", "K5", "K6")}
    board = DraftTracker(config, keepers_by_slot=keepers).update(
        _tick(settings={"budget_5": 200})
    )

    (warning,) = [w for w in board.settings_warnings if w.field == "budget_ceiling"]
    assert "slot 5 = $200" in str(warning.actual)
    assert "$185" in str(warning.actual)
    assert "Sleeper's own key" in str(warning.actual)
    assert "slot 6" not in str(warning.actual)
    # Slot 6 is the uncovered one, and the standing banner that already
    # owns it says so by slot.
    (uncovered,) = [w for w in board.settings_warnings if w.field == "keeper_budgets"]
    assert "slots 6" in str(uncovered.actual)

    # Quiet case: nobody entered anything. Every keeper slot resolves to
    # the $200 default, and the ceiling banner must stay silent on all of
    # them — one hole, one banner.
    nothing = DraftTracker(config, keepers_by_slot=keepers).update(_tick())
    assert not [w for w in nothing.settings_warnings if w.field == "budget_ceiling"]

    # Quiet case: real keys a keeper roster can actually afford.
    real = DraftTracker(config, keepers_by_slot=keepers).update(
        _tick(settings={"budget_5": 185, "budget_6": 106})
    )
    assert not [w for w in real.settings_warnings if w.field == "budget_ceiling"]


def test_the_impossible_slots_ride_the_board_with_the_banner(config):
    """The banner proves a figure impossible; the board hands the page the
    slots it named, so the proof can reach the marks.

    Provenance alone cannot express this. A ceiling-breaching Sleeper key
    IS a real key, so ``budget_is_default`` reads False and the page paints
    the figure as confidently as any correct one — a board that says "this
    cannot be true" in a banner while painting the same money green.

    ANTI-CHEAT that this list is the banner's OWN set and not a
    re-derivation. Slot 5 carries a real $200 key against a $185 ceiling
    and is named. Slot 6 holds the same three keepers and resolves to the
    same $200, but from the default, so the carve-out leaves it to the
    keeper_budgets banner and it must NOT appear here. An implementation
    that prices every keeper slot's resolved budget, or one that keys on
    ``budget_is_default``, reports slot 6 and fails.

    The provenance flag itself stays untouched on purpose: widening it
    would suppress the verdict, and a tool that blanks is worse than one
    that marks."""
    keepers = {5: ("K1", "K2", "K3"), 6: ("K4", "K5", "K6")}
    board = DraftTracker(config, keepers_by_slot=keepers).update(
        _tick(settings={"budget_5": 200})
    )

    assert board.impossible_keeper_slots == (5,)
    (warning,) = [w for w in board.settings_warnings if w.field == "budget_ceiling"]
    assert "slot 5" in str(warning.actual)
    assert "slot 6" not in str(warning.actual)
    assert board.team(5).budget_is_default is False

    nothing = DraftTracker(config, keepers_by_slot=keepers).update(_tick())
    assert not nothing.impossible_keeper_slots

    affordable = DraftTracker(config, keepers_by_slot=keepers).update(
        _tick(settings={"budget_5": 185, "budget_6": 106})
    )
    assert not affordable.impossible_keeper_slots


def test_nominee_in_the_sold_set_never_renders_live(config):
    """Anti-cheat: the stale pointer can lag MORE than one lot. This nominee
    was sold two picks ago, so a guard that only string-compares against the
    current pick's winner calls it live."""
    tracker = DraftTracker(config, clock=lambda: 100.0)
    board = tracker.update(
        _tick(
            picks=[
                raw_auction_pick(1, "A", 1, "51"),
                raw_auction_pick(2, "B", 2, "10"),
            ],
            metadata={
                "nominated_player_id": "A",
                "highest_offer": "51",
                "nominating_slot": "1",
            },
        )
    )

    assert board.nomination.player_id == "A"
    assert board.nomination.is_live is False
    assert board.nomination.status == "sold_between_lots"


def test_fresh_nominee_renders_live_with_parsed_offer(config):
    """A nominee absent from the sold set is a genuinely open lot; the
    string metadata fields arrive parsed."""
    board = DraftTracker(config).update(
        _tick(
            picks=[raw_auction_pick(1, "A", 1, "51")],
            metadata={
                "nominated_player_id": "C",
                "highest_offer": "23",
                "nominating_slot": "4",
            },
        )
    )

    nomination = board.nomination
    assert nomination.is_live is True
    assert nomination.status == "live"
    assert nomination.player_id == "C"
    assert nomination.highest_offer == 23
    assert nomination.nominating_slot == 4


def test_no_nominee_is_its_own_quiet_state(config):
    """Between the draft start and the first nomination there is nothing to
    show, and that must not read as an error."""
    nomination = DraftTracker(config).update(_tick()).nomination
    assert nomination.player_id is None
    assert nomination.is_live is False
    assert nomination.status == "none"


def test_sold_nominee_beyond_the_grace_window_reads_as_a_stuck_feed(config):
    """Within the post-sale grace window a sold nominee is the normal
    between-lots lull; well past it the pointer has been stuck too long
    and the state says so (still never live either way)."""
    now = [100.0]
    tracker = DraftTracker(config, clock=lambda: now[0], grace_seconds=10.0)
    tick = _tick(
        picks=[raw_auction_pick(1, "A", 1, "51")],
        metadata={"nominated_player_id": "A"},
    )

    assert tracker.update(tick).nomination.status == "sold_between_lots"

    now[0] = 109.9  # still inside the window measured from the observed sale
    assert tracker.update(tick).nomination.status == "sold_between_lots"

    now[0] = 111.0
    beyond = tracker.update(tick).nomination
    assert beyond.status == "sold_stale"
    assert beyond.is_live is False


def test_grace_boundary_exactly_at_grace_seconds_is_still_between_lots(config):
    """The window is inclusive by intent: elapsed == grace_seconds is the
    last instant of the normal between-lots lull, not yet a stuck feed
    (pins the <= boundary; a < would flip this exact tick to sold_stale)."""
    now = [100.0]
    tracker = DraftTracker(config, clock=lambda: now[0], grace_seconds=10.0)
    tick = _tick(
        picks=[raw_auction_pick(1, "A", 1, "51")],
        metadata={"nominated_player_id": "A"},
    )
    tracker.update(tick)

    now[0] = 110.0  # exactly grace_seconds after the observed sale
    assert tracker.update(tick).nomination.status == "sold_between_lots"


def test_degraded_picks_feed_never_renders_a_recent_winner_live(config):
    """Anti-cheat: a sale lands, then the picks endpoint fails and the
    client degrades to its older cache while the draft fetch stays fresh.
    The pointer names the recent winner, the regressed sold set no longer
    contains him — LIVE here is a ghost lot at his final price."""
    tracker = DraftTracker(config, clock=lambda: 100.0)
    fresh = _tick(
        picks=[raw_auction_pick(1, "A", 1, "51"), raw_auction_pick(2, "B", 2, "10")],
        metadata={"nominated_player_id": "B", "highest_offer": "10"},
    )
    assert tracker.update(fresh).nomination.is_live is False

    degraded = _tick(
        picks=[raw_auction_pick(1, "A", 1, "51")],
        metadata={"nominated_player_id": "B", "highest_offer": "10"},
        stale=frozenset({"picks"}),
    )
    view = tracker.update(degraded).nomination
    assert view.is_live is False
    assert view.status == "untrusted"
    assert view.player_id == "B"


def test_silent_picks_regression_is_caught_by_the_watermark(config):
    """Anti-cheat: the picks list shrinks WITHOUT the stale flag (a cache
    regressed silently). The cross-tick watermark — the most picks this
    tracker has ever observed — still refuses to rule the lot live."""
    tracker = DraftTracker(config, clock=lambda: 100.0)
    tracker.update(
        _tick(
            picks=[
                raw_auction_pick(1, "A", 1, "51"),
                raw_auction_pick(2, "B", 2, "10"),
            ],
            metadata={"nominated_player_id": "B", "highest_offer": "10"},
        )
    )

    regressed = _tick(
        picks=[raw_auction_pick(1, "A", 1, "51")],
        metadata={"nominated_player_id": "B", "highest_offer": "10"},
    )
    view = tracker.update(regressed).nomination
    assert view.is_live is False
    assert view.status == "untrusted"


def test_feed_recovery_restores_live_nominations(config):
    """Once the picks feed returns fresh at (or past) the watermark, a
    genuinely new lot renders live again — the guard suppresses only
    while the feed cannot be trusted."""
    tracker = DraftTracker(config, clock=lambda: 100.0)
    full_picks = [raw_auction_pick(1, "A", 1, "51"), raw_auction_pick(2, "B", 2, "10")]
    tracker.update(_tick(picks=full_picks, metadata={"nominated_player_id": "B"}))
    tracker.update(
        _tick(
            picks=full_picks[:1],
            metadata={"nominated_player_id": "B"},
            stale=frozenset({"picks"}),
        )
    )

    recovered = tracker.update(
        _tick(picks=full_picks, metadata={"nominated_player_id": "C"})
    )
    assert recovered.nomination.is_live is True
    assert recovered.nomination.status == "live"


def test_empty_pre_draft_feed_still_allows_the_first_nomination(config):
    """A legitimately empty feed (no sales yet, watermark 0) is not a
    regression: the draft's first nomination must render live."""
    view = (
        DraftTracker(config)
        .update(_tick(metadata={"nominated_player_id": "A", "highest_offer": "1"}))
        .nomination
    )
    assert view.is_live is True
    assert view.status == "live"


def test_settings_diff_catches_a_timer_mismatch_not_just_budget(config):
    """Anti-cheat: budget, teams, and type all match here; only the
    nomination timer differs. A diff that only checks the budget shows a
    clean board for a draft whose timers changed under us. The timer
    expectations ride in from the config's [auction] keys — no caller
    merges them by hand anymore."""
    expected = default_expected_settings(config)
    tracker = DraftTracker(config, expected_settings=expected)
    board = tracker.update(_tick(settings={"nomination_timer": 15, "pick_timer": 10}))

    assert len(board.settings_warnings) == 1
    warning = board.settings_warnings[0]
    assert warning.field == "nomination_timer"
    assert warning.expected == 10
    assert warning.actual == 15


def test_settings_diff_flags_type_teams_and_budget(config):
    """Each of the criteria's fields produces its own visible mismatch."""
    expected = default_expected_settings(config)
    tracker = DraftTracker(config, expected_settings=expected)
    board = tracker.update(
        _tick(
            draft_type="snake",
            settings={
                "teams": 10,
                "budget": 300,
                "nomination_timer": 10,
                "pick_timer": 10,
            },
        )
    )

    by_field = {warning.field: warning for warning in board.settings_warnings}
    assert by_field["type"].expected == "auction"
    assert by_field["type"].actual == "snake"
    assert by_field["teams"].actual == 10
    assert by_field["budget"].actual == 300


def test_matching_settings_produce_no_warnings(config):
    """A clean board stays clean: warnings only exist to be believed."""
    expected = default_expected_settings(config)
    tracker = DraftTracker(config, expected_settings=expected)
    board = tracker.update(_tick(settings={"nomination_timer": 10, "pick_timer": 10}))
    assert not board.settings_warnings


def test_keeper_budget_warning_holds_until_every_keeper_team_is_covered(config):
    """The 2026 pre-entry condition: rosters show keepers but no
    budget_<slot> keys exist yet, so every budget on the board is a $200
    default that overstates what keeper teams can spend.

    Anti-cheat: the commissioner enters budgets ONE TEAM AT A TIME on
    draft morning. The first entry must not clear the banner while ten
    keeper teams still show the default; covering every keeper-holding
    slot must clear it (a non-keeper slot's missing key never cries wolf).
    """
    keepers = {slot: ("K1", "K2", "K3") for slot in range(1, 12)}
    tracker = DraftTracker(config, keepers_by_slot=keepers)

    warned = tracker.update(_tick())
    fields = [warning.field for warning in warned.settings_warnings]
    assert "keeper_budgets" in fields

    partial = tracker.update(_tick(settings={"budget_1": 150}))
    partial_fields = [warning.field for warning in partial.settings_warnings]
    assert "keeper_budgets" in partial_fields

    # All 11 keeper-holding slots covered; slot 12 holds no keepers, so
    # its absent budget_12 key must not keep the warning alive.
    entered = tracker.update(
        _tick(settings={f"budget_{slot}": 150 for slot in range(1, 12)})
    )
    assert not entered.settings_warnings


def test_off_model_sale_is_flagged_but_still_debits_the_budget(config):
    """Anti-cheat: player B has no value in the sheet, so it is flagged for
    the inflation math (issue #5) to exclude — but its $7 was real money
    and must still leave the buying team's budget."""
    tracker = DraftTracker(config, value_sheet={"A": 42.0})
    board = tracker.update(
        _tick(
            picks=[raw_auction_pick(1, "A", 5, "30"), raw_auction_pick(2, "B", 5, "7")]
        )
    )

    assert board.off_model_player_ids == ("B",)
    team = board.team(5)
    assert team.spent == 37
    assert team.remaining == 163


def test_without_a_value_sheet_nothing_is_flagged(config):
    """No sheet injected means no basis for calling any sale off-model."""
    board = DraftTracker(config).update(
        _tick(picks=[raw_auction_pick(1, "A", 5, "30")])
    )
    assert not board.off_model_player_ids


def test_positional_needs_fill_dedicated_slots_then_flex_then_bench(config):
    """Keepers (positions via the injected map) and purchases (positions via
    pick metadata) fill their dedicated slots first; surplus RB/WR/TE spill
    into FLEX, then the bench. Needs must track the roster shape, not just
    a raw count."""
    tracker = DraftTracker(
        config,
        keepers_by_slot={7: ("KWad", "KHam", "KMcC")},
        player_positions={"KWad": "WR", "KHam": "RB", "KMcC": "RB"},
    )
    board = tracker.update(
        _tick(
            picks=[
                raw_auction_pick(1, "Q1", 7, "20", position="QB"),
                raw_auction_pick(2, "R3", 7, "30", position="RB"),  # 3rd RB -> FLEX
                raw_auction_pick(3, "R4", 7, "5", position="RB"),  # 4th RB -> BN
            ]
        )
    )

    team = board.team(7)
    assert team.keeper_count == 3
    assert team.purchase_count == 3
    assert team.open_slots == 9
    assert team.needs == {
        "QB": 0,
        "RB": 0,
        "WR": 1,
        "TE": 1,
        "FLEX": 0,
        "K": 1,
        "DEF": 1,
        "BN": 5,
    }
    assert sum(team.needs.values()) == team.open_slots


def test_unknown_position_players_occupy_a_bench_slot(config):
    """A purchase with no position anywhere still occupies a roster spot;
    parking it on the bench keeps the needs honest instead of losing it."""
    pick = raw_auction_pick(1, "X", 2, "3")
    pick["metadata"].pop("position")
    team = DraftTracker(config).update(_tick(picks=[pick])).team(2)

    assert team.needs["BN"] == 5
    assert sum(team.needs.values()) == team.open_slots == 14


def test_board_lists_every_sale_for_the_inflation_math(config):
    """Issue #5's inflation ratio removes sold players from the pool and
    counts on-model dollars from the board ALONE: every sale surfaces with
    id, price, and buying slot in feed order, not just the off-model
    subset."""
    board = DraftTracker(config).update(
        _tick(
            picks=[raw_auction_pick(1, "A", 5, "30"), raw_auction_pick(2, "B", 2, "7")]
        )
    )
    assert board.sales == (Sale("A", 30, 5), Sale("B", 7, 2))


def test_board_carries_the_timer_and_pause_still_blocks_expiry(config):
    """Issue #7 renders the countdown from the board: timer_end_at
    surfaces, and the pause guard rides along — a paused draft's frozen
    timer must not read as expired from the board either."""
    running = DraftTracker(config).update(_tick(metadata={"timer_end_at": "1000000"}))
    assert running.timer_end_at == 1_000_000
    assert running.is_timer_expired(now_ms=1_000_000) is True
    assert running.is_timer_expired(now_ms=999_999) is False

    paused = DraftTracker(config).update(
        _tick(metadata={"timer_end_at": "1000000", "paused": "true"})
    )
    assert paused.is_timer_expired(now_ms=2_000_000) is False


def test_nomination_view_distinguishes_nominator_from_high_bidder(config):
    """The wire carries two different teams: nominating_slot is who
    NOMINATED the player, offering_slot who holds the HIGH BID. The view
    must keep them apart — conflating them tells Ethan the wrong team is
    spending."""
    board = DraftTracker(config).update(
        _tick(
            metadata={
                "nominated_player_id": "C",
                "highest_offer": "23",
                "nominating_slot": "4",
                "offering_slot": "9",
            }
        )
    )
    assert board.nomination.is_live is True
    assert board.nomination.nominating_slot == 4
    assert board.nomination.offering_slot == 9


def test_team_needs_are_read_only(config):
    """TeamState is shared derived state; a consumer decrementing needs
    in place would corrupt every later reader of the same board."""
    team = DraftTracker(config).update(_tick()).team(1)
    with pytest.raises(TypeError):
        team.needs["QB"] = 0  # type: ignore[index]


def test_pause_and_staleness_propagate_to_the_board(config):
    """A paused draft is not an expired timer, and cache-served endpoints
    surface on the board tick by tick."""
    board = DraftTracker(config).update(
        _tick(metadata={"paused": "true"}, stale=frozenset({"picks"}))
    )
    assert board.paused is True
    assert board.status == "drafting"
    assert board.stale_endpoints == frozenset({"picks"})


def test_a_bridge_that_carries_no_keepers_has_nothing_to_fall_behind(config):
    """The stale-bridge rule fires on a MIS-ATTRIBUTION, and a bridge that
    carries no keeper at all attributes nothing.

    Every figure that rule protects — roster panels, need counts, open
    slots, the max bid behind them — is slot-keyed and recomputed each
    tick on a keeper-free board, so the deal moves my slot and nothing
    else. Blanking the tool there would cost the verdict and buy nothing.

    Narrow but real: startup refuses a keeper league with zero keepers
    unless ``--allow-no-keepers`` is passed, so this is the operator
    waving that through because Sleeper's keeper data has not landed, and
    then losing the verdict the moment the order deals.

    ANTI-CHEAT: both trackers are bridged against the same placeholder and
    fed the same dealt map, so the ONLY difference is whether anything was
    bridged. An implementation that stopped comparing the maps, or that
    read ``keepers_by_slot``'s KEYS (both trackers have twelve) rather
    than its values, passes the first assert and fails the second.
    """
    dealt = _tick(slots=_DEALT_SLOTS)
    empty = DraftTracker(
        config,
        keepers_by_slot={slot: () for slot in _PLACEHOLDER_SLOTS},
        keeper_slot_map=_PLACEHOLDER_SLOTS,
    )
    kept = DraftTracker(
        config,
        keepers_by_slot={slot: () for slot in _PLACEHOLDER_SLOTS} | {7: ("KP",)},
        keeper_slot_map=_PLACEHOLDER_SLOTS,
    )

    assert empty.update(dealt).keeper_map_stale is False
    assert not empty.update(dealt).settings_warnings
    assert kept.update(dealt).keeper_map_stale is True


def test_keepers_by_slot_derives_from_rosters_through_the_slot_map():
    """Rosters key keepers by roster_id and the draft object maps draft
    slots to roster ids; the mapping here is deliberately crossed so an
    implementation equating slot with roster id fails."""
    draft = parse_draft(
        {
            "draft_id": "d",
            "status": "pre_draft",
            "type": "auction",
            "settings": {},
            "metadata": {},
            "slot_to_roster_id": {"1": 7, "2": 1, "3": 3},
        }
    )
    rosters = [
        {"roster_id": 1, "keepers": ["A", "B", "C"]},
        {"roster_id": 7, "keepers": ["Z"]},
        {"roster_id": 3, "keepers": None},
    ]

    keepers = keepers_by_slot_from_rosters(draft, rosters)
    assert keepers[1] == ("Z",)
    assert keepers[2] == ("A", "B", "C")
    assert keepers[3] == ()
