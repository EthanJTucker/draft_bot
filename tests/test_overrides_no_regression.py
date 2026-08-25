"""With no override file, nothing moves. The gate on the whole feature.

An override sheet is an occasional lever. Baseline pricing is every
number the tool shows, on every lot, all night -- and it is pinned by
golden max bids and a 180-sale replay backtest. A feature that perturbs
those when it is NOT IN USE is a silent regression across every price,
which is strictly worse than the feature is valuable.

So this module replays the real 2025 draft and asserts that every field
of every record is identical with the override argument omitted, passed
as an empty book, and passed as a book naming only players who are not
the nominee. Field-by-field rather than max-bid-only: a layer perturbed
upstream of the floor can land on the same integer for a whole fixture
and still be wrong.
"""

# pylint: disable=duplicate-code  # the replay loop below re-drives the
# real feed with the same source/tracker construction the engine and
# backtest suites use; driving it the SAME way is what makes "nothing
# moved" a claim about the engine rather than about a private harness.

from __future__ import annotations

import dataclasses
import json

import pytest

from draftbot.backtest import build_history_price_sheet
from draftbot.config import load_config
from draftbot.draft_engine import analyze_player
from draftbot.overrides import PlayerOverride
from draftbot.sources import ReplaySource
from draftbot.tracker import DraftTracker, default_expected_settings
from draftbot.valuation import value_map

from .conftest import REPO_ROOT

DRAFT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"
HISTORY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "league_history.json"

#: A book that must never touch a 2025 lot: ids the fixture cannot carry.
#: Present so "no override file" and "an override file about other
#: players" are both proven inert, not just the first.
ELSEWHERE = {
    "not_a_real_player": PlayerOverride(
        player_id="not_a_real_player",
        name=None,
        tier=1,
        target=True,
        avoid=True,
        delta=99,
        note="must never land",
    )
}


@pytest.fixture(scope="module", name="replay")
def replay_fixture():
    """The real 2025 feed plus the 2023-24 fitted sheet. Module-scoped:
    the replay is the expensive step."""
    config = load_config(REPO_ROOT / "league_config.toml")
    history = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))
    data = json.loads(DRAFT_FIXTURE.read_text(encoding="utf-8"))
    rows = build_history_price_sheet(history, config, season=2025)
    return {"config": config, "data": data, "rows": rows}


def _lots(replay):
    """Every 2025 lot as (nominee, pre-sale board), in feed order."""
    config, rows = replay["config"], replay["rows"]
    source = ReplaySource(replay["data"]["draft"], replay["data"]["picks"])
    tracker = DraftTracker(
        config,
        expected_settings=default_expected_settings(config),
        value_sheet=value_map(rows),
    )
    board = tracker.update(source.poll())
    while board.status != "complete":
        pre_sale = board
        board = tracker.update(source.poll())
        yield board.sales[-1].player_id, pre_sale


def test_no_override_book_reproduces_the_baseline_on_all_180_lots(replay):
    """The anti-regression gate. Three ways of saying "no opinion about
    this player" must all reproduce the record byte for byte, on every
    field, on every one of the 180 real lots.

    ``override`` itself is expected to be None throughout: a record that
    exists is the page's signal to draw a chip, so an all-zero record on
    an un-listed player would claim a row the operator never wrote."""
    config, rows = replay["config"], replay["rows"]
    lots = 0
    for nominee, pre_sale in _lots(replay):
        lots += 1
        baseline = analyze_player(nominee, rows, pre_sale, config)
        assert baseline.override is None
        for book in ({}, ELSEWHERE):
            other = analyze_player(nominee, rows, pre_sale, config, overrides=book)
            assert dataclasses.asdict(other) == dataclasses.asdict(baseline)
    assert lots == 180  # every lot was actually reached


def test_the_pinned_goldens_still_hold_with_the_override_seam_in_place(replay):
    """The published numbers, re-asserted here rather than only in the
    engine suite: these are the figures the backtest report renders and
    the ones a reviewer diffs. ``max_bid`` must stay exactly the floor of
    the spend-adjusted price, capped by my team's max bid, on every lot
    the fixture scores."""
    config, rows = replay["config"], replay["rows"]
    pinned = []
    for nominee, pre_sale in _lots(replay):
        record = analyze_player(nominee, rows, pre_sale, config)
        assert record.max_bid == min(record.my_cap, max(1, int(record.spend_adjusted)))
        pinned.append(record.max_bid)

    # Lot 2 is my own team's buy -- the roster-collapse lever the seam
    # contract already pins -- plus the running total over all 180, so a
    # wholesale shift in baseline pricing cannot slip through by keeping
    # one lot right. Both figures were read off the pre-feature tree at
    # e12017e and must not move when no override book is supplied.
    assert pinned[1] == 53
    assert sum(pinned) == 1099
