"""End-to-end engine behavior: a simulated auction and the 2025 replay.

The simulation is the spend-down acceptance test: eleven value-paying
opponents against the engine's max bids, full roster shape, 180 lots.
The engine must lose the early lots (bargain stance), absorb lots at
real prices mid-draft, and finish with a full roster and no meaningful
unspent cash. The 2025 replay drives the real 180-sale feed through the
tracker and asks the engine for a record at every sale, always on the
board as it stood BEFORE that sale folded in — the seam contract the
backtest (issue #6) must copy — and must produce byte-identical records
across two full runs.
"""

from __future__ import annotations

import json

from draftbot.draft_engine import analyze_player
from draftbot.sources import ReplaySource
from draftbot.tracker import DraftTracker, Sale, default_expected_settings
from draftbot.valuation import SheetRow, value_map

from .conftest import REPO_ROOT
from .helpers_engine import make_board, sheet_row

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"

#: Sim league shape: twelve $200 teams, fifteen drafted slots each.
_SLOTS_PER_TEAM = 15
_TEAMS = 12
_BUDGET = 200
_MY_SLOT = 7


def _sim_rows() -> list[SheetRow]:
    """A 190-player pool with a decaying value curve summing near the
    room's \\$2400, positions cycling so every roster can fill legally,
    and K/DEF floor players at the tail. Deterministic by construction."""
    pattern = ("RB", "WR", "WR", "RB", "QB", "TE")
    rows = []
    for index in range(166):
        worth = round(1.0 + 62.0 * 0.975**index, 2)
        rows.append(
            sheet_row(index + 1, f"p{index:03d}", pattern[index % len(pattern)], worth)
        )
    for index in range(24):
        position = "K" if index % 2 == 0 else "DEF"
        rows.append(sheet_row(167 + index, f"q{index:03d}", position, 1.0))
    return rows


def _best_remaining(rows, sold):
    remaining = [row for row in rows if row.player_id not in sold]
    return max(remaining, key=lambda row: (row.value, row.player_id))


def _room_bids(board, nominee_value, my_slot):
    """Every opponent with an open slot bids up to the nominee's value,
    capped by its own max bid. Deterministic: ties break by slot."""
    bids = []
    for team in board.teams:
        if team.slot == my_slot or team.open_slots <= 0:
            continue
        cap = min(round(nominee_value), team.max_bid)
        if cap >= 1:
            bids.append((cap, team.slot))
    return bids


def _settle(my_bid, my_slot, room_bids):
    """English-auction settlement: the highest cap wins at one dollar
    over the runner-up (ties to the lowest slot; the engine's bid wins
    ties it is part of only by outbidding, never by matching)."""
    if not room_bids:
        return my_slot, 1
    top_cap, top_slot = max(room_bids, key=lambda bid: (bid[0], -bid[1]))
    if my_bid > top_cap:
        return my_slot, max(1, top_cap + 1)
    runner_up = max(
        [my_bid, *[cap for cap, slot in room_bids if slot != top_slot]] or [0]
    )
    return top_slot, max(1, min(top_cap, runner_up + 1))


def _run_simulated_draft(config):
    """The full 180-lot auction; returns (final board, my winning lots)."""
    rows = _sim_rows()
    budgets = {slot: _BUDGET for slot in range(1, _TEAMS + 1)}
    sales: list[Sale] = []
    my_lots = []
    for _ in range(_TEAMS * _SLOTS_PER_TEAM):
        board = make_board(budgets, sales, drafted_slots=_SLOTS_PER_TEAM)
        sold = {sale.player_id for sale in sales}
        nominee = _best_remaining(rows, sold)
        analysis = analyze_player(
            nominee.player_id, rows, board, config, my_slot=_MY_SLOT
        )
        my_bid = analysis.max_bid if board.team(_MY_SLOT).open_slots > 0 else 0
        winner, price = _settle(
            my_bid, _MY_SLOT, _room_bids(board, nominee.value, _MY_SLOT)
        )
        if winner == _MY_SLOT and board.team(_MY_SLOT).open_slots <= 0:
            raise AssertionError("engine bought into a full roster")
        sales.append(Sale(nominee.player_id, price, winner))
        if winner == _MY_SLOT:
            my_lots.append((len(sales), nominee.player_id, price))
    return make_board(budgets, sales, drafted_slots=_SLOTS_PER_TEAM), my_lots


def test_spend_down_forces_full_budget_use(config):
    """Acceptance (and the timid-schedule anti-cheat): across a full
    simulated auction the engine must fill all fifteen slots and leave no
    meaningful cash — shallower schedules strand \\$10-16 of the \\$200
    here, and a value-capped bidder strands \\$40+. The bargain stance
    must also show: the engine wins none of the opening lots, where the
    room still pays full value and no pace deficit exists."""
    board, my_lots = _run_simulated_draft(config)

    me = board.team(_MY_SLOT)
    assert me.open_slots == 0
    assert me.purchase_count == _SLOTS_PER_TEAM
    assert me.remaining <= 5, f"finished with ${me.remaining} unspent"
    assert me.spent >= _BUDGET - 5
    assert all(lot_number > 5 for lot_number, _, _ in my_lots)
    # The whole room stayed legal: nobody overspent or overfilled.
    for team in board.teams:
        assert team.remaining >= 0
        assert team.open_slots >= 0


def _replay_rows(picks: list[dict]) -> list[SheetRow]:
    """A value sheet for the 2025 pool priced at the actual amounts, with
    two real sales deliberately left OFF the sheet so the replay carries
    off-model lots for the tracker to flag and the engine to exclude."""
    priced = sorted(
        picks,
        key=lambda pick: (-int(pick["metadata"]["amount"]), pick["player_id"]),
    )
    rows = []
    for rank, pick in enumerate(priced, start=1):
        if pick["player_id"] in _OFF_MODEL_2025:
            continue
        rows.append(
            sheet_row(
                rank,
                pick["player_id"],
                pick["metadata"].get("position") or "WR",
                float(max(1, int(pick["metadata"]["amount"]))),
            )
        )
    return rows


#: Two real 2025 sales kept off the replay sheet (an early expensive lot
#: and a late $1 lot) so the off-model path runs against real data.
_OFF_MODEL_2025 = frozenset({"7547", "8155"})


def _replay_once(config, rows):
    """One full replay, returning the engine's record for every sale.

    THE SEAM CONTRACT (what the issue #6 backtest must copy): each
    nominee is priced on the board as it stood BEFORE his own sale
    folded in — the state at the moment of nomination. Analyzing on the
    post-sale board contaminates the comparison against the winning
    amount: the player is out of his own pool and, on lots my team won,
    already sits on my roster, collapsing the bid to a bench-retention
    price."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source = ReplaySource(data["draft"], data["picks"])
    tracker = DraftTracker(
        config,
        expected_settings=default_expected_settings(config),
        value_sheet=value_map(rows),
    )
    board = tracker.update(source.poll())
    records = []
    while board.status != "complete":
        pre_sale = board
        board = tracker.update(source.poll())
        nominee = board.nomination.player_id
        # The board being priced must not know the nominee's own sale.
        assert nominee not in {sale.player_id for sale in pre_sale.sales}
        records.append(analyze_player(nominee, rows, pre_sale, config))
    return records


def test_engine_prices_every_2025_sale_deterministically(config):
    """The issue #6 seam, proven on the real feed: the engine produces a
    record at all 180 sale moments, each priced on the PRE-sale board
    (my slot resolving from the config's roster id through the 2025
    slot map), every bid is a whole-dollar number within my cap, the
    off-model lots price at the floor, the board's repricing genuinely
    moves mid-draft, and two full replays are byte-identical."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = _replay_rows(data["picks"])

    first = _replay_once(config, rows)
    second = _replay_once(config, rows)

    assert len(first) == 180
    assert first == second
    assert [repr(record) for record in first] == [repr(record) for record in second]
    for record in first:
        assert record.max_bid >= 0
        assert record.max_bid <= record.my_cap or record.my_cap == 0
    off_model = [record for record in first if record.player_id in _OFF_MODEL_2025]
    assert len(off_model) == 2
    assert all(record.rank is None and record.worth == 1.0 for record in off_model)
    assert any(record.inflation != 1.0 for record in first)
    # Golden pins on the real feed: the first twelve pre-sale max bids
    # and the full-draft total. These freeze the floor rounding of the
    # headline integer (mutating floor() to round() lifts the total to
    # $1242 across 34 lots) and the pre-sale seam itself (lot 2 is a $55
    # sale my own team won: priced before the sale folds in, the engine
    # offers $56; priced after, the player already sits on my roster and
    # the bid collapses to a bench-retention $13). Lot 1 is the early
    # off-model sale, pinned at the $1 floor.
    assert [record.max_bid for record in first[:12]] == [
        1,
        56,
        52,
        30,
        37,
        18,
        36,
        11,
        37,
        37,
        10,
        26,
    ]
    assert sum(record.max_bid for record in first) == 1208
