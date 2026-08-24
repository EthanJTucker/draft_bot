"""End-to-end: the real 2025 draft replayed through source and tracker.

The fixture is the live 2025 draft (id 1257407146123857920) reduced to the
fields the models parse, fetched once on 2026-08-23; every dollar below is
real. Beyond dropping unparsed fields, the fixture makes two deliberate
VALUE reductions from the real feed: (a) ``picked_by`` is blanked to ""
on all 180 picks (the real feed carries the winner's user id; blanking it
is an anti-cheat against attributing purchases by ``picked_by`` instead
of ``draft_slot``), and (b) ``draft.metadata`` is emptied (the real
completed object still carries the stale nomination pointer at the last
sale — the replay source re-synthesizes that pointer per tick, so the
guard is still exercised on all 180 of them).

Four teams finished with money unspent — an auction does not force
spend-down — so instead of assuming spent == budget the checks pin each
team's EXACT final spend and leftover against the draft object's own
``budget_<slot>`` values, which is strictly stronger.
"""

from __future__ import annotations

import json

from draftbot.sources import ReplaySource
from draftbot.tracker import DraftTracker, default_expected_settings

from .conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"

# Verified against the live API on 2026-08-23: the 2025 draft object's
# budget_<slot> map, and the dollars each team left on the table (8 of 12
# teams spent to exactly zero).
BUDGETS_2025 = {
    1: 133,
    2: 115,
    3: 134,
    4: 170,
    5: 117,
    6: 185,
    7: 111,
    8: 137,
    9: 106,
    10: 170,
    11: 129,
    12: 137,
}
LEFTOVER_2025 = {2: 8, 5: 1, 7: 2, 10: 1}

# Also real: four teams ended the auction with full rosters but genuine
# positional holes (slot 4 bought no TE; slot 5 no TE or DEF; slots 6 and
# 10 no K or DEF). Needs must report those holes instead of pretending a
# surplus RB can sit in a TE slot.
HOLES_2025 = {
    4: {"TE": 1},
    5: {"TE": 1, "DEF": 1},
    6: {"K": 1, "DEF": 1},
    10: {"K": 1, "DEF": 1},
}


def _source() -> ReplaySource:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ReplaySource(data["draft"], data["picks"])


def _tracker(config) -> DraftTracker:
    # Timer expectations included: they ride in from the config's
    # [auction] keys, not from literals merged here.
    return DraftTracker(config, expected_settings=default_expected_settings(config))


def test_2025_replay_reaches_every_teams_exact_final_dollars(config):
    """Replaying all 180 sales lands every team on the dollar: spent equals
    its own slot budget minus its recorded leftover, all 15 slots filled,
    nothing left to bid."""
    source = _source()
    tracker = _tracker(config)

    board = tracker.update(source.poll())
    polls = 1
    while board.status != "complete":
        board = tracker.update(source.poll())
        polls += 1
        assert polls <= 200, "replay did not complete"

    assert polls == 181  # the empty opening tick plus one per sale
    assert sum(team.purchase_count for team in board.teams) == 180
    # The board's own sale list carries the full ledger: 180 sales whose
    # dollars sum to exactly the money the twelve teams spent.
    assert len(board.sales) == 180
    assert sum(sale.amount or 0 for sale in board.sales) == sum(
        team.spent for team in board.teams
    )
    for slot, budget in BUDGETS_2025.items():
        team = board.team(slot)
        leftover = LEFTOVER_2025.get(slot, 0)
        assert team.budget == budget
        assert team.budget_is_default is False
        assert team.spent == budget - leftover
        assert team.remaining == leftover
        assert team.purchase_count == 15
        assert team.open_slots == 0
        assert team.max_bid == 0
        holes = {label: count for label, count in team.needs.items() if count}
        assert holes == HOLES_2025.get(slot, {})

    # The 2026 assumptions hold for the 2025 draft too (type, teams, $200
    # base budget, both 10-second timers): a warning here would be noise.
    assert not board.settings_warnings


def test_2025_replay_never_shows_a_sold_nominee_as_live(config):
    """On every mid-replay tick the draft metadata points at the just-sold
    winner, exactly like live Sleeper between lots; the guard must rule it
    not-live on all 180 of them."""
    source = _source()
    tracker = _tracker(config)

    board = tracker.update(source.poll())
    assert board.nomination.player_id is None
    while board.status != "complete":
        board = tracker.update(source.poll())
        nomination = board.nomination
        assert nomination.player_id is not None
        assert nomination.is_live is False
        assert nomination.status == "sold_between_lots"
        # Honest replay synthesis on every tick: the winner IS the high
        # bidder at the hammer (offering_slot), and the nominator is
        # unknowable from the picks feed (never fabricated).
        assert nomination.offering_slot == board.sales[-1].draft_slot
        assert nomination.nominating_slot is None
