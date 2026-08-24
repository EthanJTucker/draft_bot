"""The 2025 replay backtest gate: the full feed, the bounds, the report.

The replay fixture is the real 180-sale 2025 draft; the sheet is fit on
2023-2024 bids only (nothing the bot could not have known on 2025 draft
night). The bounds pinned here are measured-first: each gate documents
its measured value and margin, and a tightness guard fails the suite if
the bound ever sits further from reality than the documented margin —
a loose bound that a broken engine could pass is itself a failure.
"""

# pylint: disable=duplicate-code  # the pre-sale seam anti-cheat re-drives
# the replay with the same source/tracker construction the engine suite's
# reference loop uses; that overlap IS the seam contract under test.

from __future__ import annotations

import io
import json

import pytest

from draftbot.backtest import (
    RUNNING_DRIFT_SPREAD_BOUND,
    RUNNING_DRIFT_SPREAD_MARGIN,
    RUNNING_MAE_BOUND,
    RUNNING_MAE_MARGIN,
    STATIC_BIAS_BOUND,
    STATIC_BIAS_MARGIN,
    STATIC_DRIFT_SPREAD_BOUND,
    STATIC_DRIFT_SPREAD_MARGIN,
    STATIC_MAE_BOUND,
    STATIC_MAE_MARGIN,
    build_history_price_sheet,
    drift_spread,
    main,
    off_model_records,
    overall_stats,
    render_report,
    replay_records,
    scored_records,
    segment_stats,
)
from draftbot.config import load_config
from draftbot.draft_engine import analyze_player
from draftbot.sources import ReplaySource
from draftbot.tracker import DraftTracker, default_expected_settings
from draftbot.valuation import value_map

from .conftest import REPO_ROOT

DRAFT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"
HISTORY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "league_history.json"


@pytest.fixture(scope="module", name="replay")
def replay_fixture():
    """One full replay: the sheet, the draft data, and the 180 records.

    Module-scoped (the replay is the expensive step), so the config loads
    here instead of through the function-scoped ``config`` fixture.
    """
    config = load_config(REPO_ROOT / "league_config.toml")
    history = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))
    data = json.loads(DRAFT_FIXTURE.read_text(encoding="utf-8"))
    rows = build_history_price_sheet(history, config, season=2025)
    records = replay_records(data["draft"], data["picks"], rows, config)
    return {"config": config, "data": data, "rows": rows, "records": records}


class TestReplayRecords:
    """The replay covers every sale, in order, against the real feed."""

    def test_every_2025_sale_is_scored_in_feed_order(self, replay):
        """180 records, lots 1..180, each carrying the actual winning bid
        and the player's name straight from the picks feed."""
        records = replay["records"]
        picks = sorted(replay["data"]["picks"], key=lambda pick: pick["pick_no"])
        assert len(records) == 180
        assert [record.lot for record in records] == list(range(1, 181))
        for record, pick in zip(records, picks):
            assert record.player_id == pick["player_id"]
            assert record.actual == int(pick["metadata"]["amount"])
            assert record.name == (
                f"{pick['metadata']['first_name']} {pick['metadata']['last_name']}"
            )

    def test_off_model_lots_are_exactly_the_21_k_def_sales(self, replay):
        """The 2025 history fixture carries no K/DEF market, so the 21
        kicker and defense sales ($37 of the room's $1632) are the
        off-model set — flagged by the engine (no sheet rank), never
        scored. Every scored lot is a QB/RB/WR/TE the sheet prices."""
        records = replay["records"]
        positions = {
            pick["player_id"]: pick["metadata"]["position"]
            for pick in replay["data"]["picks"]
        }
        excluded = off_model_records(records)
        scored = scored_records(records)
        assert len(excluded) == 21
        assert len(scored) == 159
        assert all(positions[record.player_id] in ("K", "DEF") for record in excluded)
        assert sum(record.actual for record in excluded) == 37
        sheet_ids = {row.player_id for row in replay["rows"]}
        assert all(record.player_id not in sheet_ids for record in excluded)
        assert all(record.player_id in sheet_ids for record in scored)
        assert all(positions[record.player_id] not in ("K", "DEF") for record in scored)


def test_estimates_are_priced_on_the_pre_sale_board(replay):
    """The seam anti-cheat: re-drive the replay by hand and, on lots
    where the two boards provably disagree, compare the module's records
    against BOTH — the recorded estimate must equal the pre-sale number
    and differ from the post-sale one. Lot 2 is the sharpest lever: a $55
    sale my own team won, where post-sale pricing also collapses the max
    bid to a bench-retention number because the player already sits on my
    roster. The pre != post assertions keep the test honest — if the
    boards ever stopped disagreeing, the test fails loudly instead of
    passing vacuously."""
    config, rows = replay["config"], replay["rows"]
    records = replay["records"]
    source = ReplaySource(replay["data"]["draft"], replay["data"]["picks"])
    tracker = DraftTracker(
        config,
        expected_settings=default_expected_settings(config),
        value_sheet=value_map(rows),
    )
    board = tracker.update(source.poll())
    lot = 0
    while board.status != "complete":
        pre_sale = board
        board = tracker.update(source.poll())
        lot += 1
        if lot not in (2, 3, 30):
            continue
        nominee = board.sales[-1].player_id
        pre = analyze_player(nominee, rows, pre_sale, config)
        post = analyze_player(nominee, rows, board, config)
        assert records[lot - 1].running == pre.inflation_adjusted
        assert records[lot - 1].max_bid == pre.max_bid
        assert pre.inflation_adjusted != post.inflation_adjusted
        if lot == 2:  # my own team's buy: the roster-collapse lever
            assert pre.max_bid != post.max_bid
    assert lot == 180  # every checked lot was actually reached


class TestGateBounds:
    """The pytest gate: measured-first bounds with tightness guards.

    Every bound documents its measured value and margin in
    ``reports/backtest_2025.md``; the guards assert each bound sits no
    further above the measured number than its margin, so an engine
    regression trips the bound while an engine IMPROVEMENT trips the
    guard and forces the bound (and report) to be re-tightened.
    """

    def test_running_mae_stays_within_the_documented_bound(self, replay):
        """Measured 5.5376 on the committed fixtures; bound 6.50 with a
        $1.00 margin."""
        stats = overall_stats(replay["records"], "running")
        assert stats.mae <= RUNNING_MAE_BOUND
        assert RUNNING_MAE_BOUND - stats.mae <= RUNNING_MAE_MARGIN

    def test_running_drift_spread_stays_within_the_measured_pin(self, replay):
        """A REGRESSION PIN, deliberately not a no-drift certificate: the
        running estimate provably deflates the early board (measured
        segment bias -11.81 early, -2.69 mid, -0.02 late; spread 11.7962,
        pinned at 13.00 with a $1.50 margin) because the remaining-pool
        denominator carries sheet value the room never buys. The shape is
        pinned too: the deflation must fade monotonically as the pool
        empties, and the late board must price nearly on the money."""
        segments = segment_stats(replay["records"], "running")
        spread = drift_spread(segments)
        assert spread <= RUNNING_DRIFT_SPREAD_BOUND
        assert RUNNING_DRIFT_SPREAD_BOUND - spread <= RUNNING_DRIFT_SPREAD_MARGIN
        early, mid, late = segments
        assert early.stats.bias < mid.stats.bias < late.stats.bias
        assert late.stats.bias == pytest.approx(0.0, abs=0.5)

    def test_static_sheet_shows_no_systematic_drift(self, replay):
        """The no-systematic-drift claim holds where it is true: the
        history-fit sheet itself. Measured spread 1.9351 (bound 2.50,
        margin $0.75) and overall bias -0.5522 (bound 1.50, margin
        $1.00): the fitted prices track the room across the whole draft
        with no meaningful trend as the pool empties."""
        segments = segment_stats(replay["records"], "static")
        spread = drift_spread(segments)
        assert spread <= STATIC_DRIFT_SPREAD_BOUND
        assert STATIC_DRIFT_SPREAD_BOUND - spread <= STATIC_DRIFT_SPREAD_MARGIN
        bias = abs(overall_stats(replay["records"], "static").bias)
        assert bias <= STATIC_BIAS_BOUND
        assert STATIC_BIAS_BOUND - bias <= STATIC_BIAS_MARGIN

    def test_static_mae_stays_within_the_documented_bound(self, replay):
        """Measured 2.2388; bound 2.75 with a $0.60 margin."""
        stats = overall_stats(replay["records"], "static")
        assert stats.mae <= STATIC_MAE_BOUND
        assert STATIC_MAE_BOUND - stats.mae <= STATIC_MAE_MARGIN


class TestReport:
    """The committed report is generated, complete, and byte-stable."""

    def test_two_full_pipeline_runs_render_byte_identical_reports(self, replay):
        """Determinism end to end: a second, completely independent run —
        fresh sheet fit, fresh replay — must render the exact same text
        (no clocks, no unsorted iteration, no float noise)."""
        first = render_report(replay["records"], replay["rows"])
        history = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))
        data = json.loads(DRAFT_FIXTURE.read_text(encoding="utf-8"))
        rows = build_history_price_sheet(history, replay["config"], season=2025)
        records = replay_records(data["draft"], data["picks"], rows, replay["config"])
        assert render_report(records, rows) == first

    def test_report_carries_every_required_number(self, replay):
        """The acceptance surface: overall and per-position MAE, the
        early/mid/late drift comparison, the documented bounds, and the
        off-model exclusions all appear in the rendered text."""
        text = render_report(replay["records"], replay["rows"])
        for required in (
            "## Headline",
            "## Per-position",
            "## Early/mid/late drift",
            "## Off-model lots",
            "## The pytest gate",
            "| QB |",
            "| RB |",
            "| WR |",
            "| TE |",
            "early",
            "mid",
            "late",
            f"{RUNNING_MAE_BOUND:.2f}",
            f"{RUNNING_DRIFT_SPREAD_BOUND:.2f}",
            f"{STATIC_MAE_BOUND:.2f}",
            f"{STATIC_DRIFT_SPREAD_BOUND:.2f}",
        ):
            assert required in text, f"report is missing {required!r}"

    def test_committed_report_matches_a_fresh_regeneration(self, replay):
        """The committed artifact is generated, never hand-edited: the
        checked-in ``reports/backtest_2025.md`` must equal a fresh render
        byte for byte. A hand-tuned number, a stale copy after an engine
        change, or a nondeterministic renderer all fail here."""
        committed = REPO_ROOT / "reports" / "backtest_2025.md"
        assert committed.read_text(encoding="utf-8") == render_report(
            replay["records"], replay["rows"]
        )

    def test_cli_writes_the_report_and_exits_zero(self, replay, tmp_path):
        """``python -m draftbot.backtest --out <path>`` writes exactly the
        rendered report and reports the headline numbers on stdout."""
        out_path = tmp_path / "report.md"
        stream = io.StringIO()
        assert main(["--out", str(out_path)], out=stream) == 0
        assert out_path.read_text(encoding="utf-8") == render_report(
            replay["records"], replay["rows"]
        )
        # The actual number next to its label, not just the label: a
        # summary printing bias where MAE belongs must fail here.
        run = overall_stats(replay["records"], "running")
        assert f"running MAE {run.mae:.2f} bias {run.bias:+.2f}" in stream.getvalue()

    def test_cli_fails_cleanly_on_a_missing_fixture(self, tmp_path):
        """A bad path is exit code 2 and a message, never a traceback."""
        stream = io.StringIO()
        missing = tmp_path / "nope.json"
        assert main(["--history", str(missing)], out=stream) == 2
        assert "error" in stream.getvalue()
