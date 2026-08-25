"""The 2025 replay backtest: the engine's running price vs the room's bids.

Replays the real 180-sale 2025 draft feed through the tracker and asks the
engine for a full analysis at every sale moment, always on the board as it
stood BEFORE that sale folded in (the pre-sale seam contract from
``draftbot/draft_engine.py``: pricing a nominee on a board that already
contains his own sale contaminates the comparison). The value sheet is fit
HONESTLY: room prices from the 2023-2024 winning bids only, applied to
2025 ADP — nothing the bot could not have known on 2025 draft night.

Three estimates are scored against each actual winning bid:

- **running** — ``PlayerAnalysis.inflation_adjusted``, the engine's market
  estimate (sheet price under tapered positional inflation). The primary
  metric of the backtest gate.
- **static** — the sheet price alone, no draft-state adjustment: the
  diagnostic baseline that separates sheet calibration from what the
  engine's dynamics add.
- **max_bid** — the engine's final bid ceiling. Bid POLICY (my roster
  need and spend schedule folded in), not a price prediction; reported
  as a clearly-labeled secondary comparison only.

Error convention: ``error = estimate - actual`` (negative = the estimate
ran under the room). ``bias`` is the mean signed error, ``mae`` the mean
absolute error. Sales the sheet does not price (all K/DEF in 2025 — the
fixture's history carries no K/DEF market) are off-model: the engine flags
them, the statistics exclude them, and the report counts and lists them.

Offline data source for the dashboard (issue #7): the dashboard's
``--replay`` mode drives the SAME chain through the existing public
seams — feed ``ReplaySource(data["draft"], data["picks"])`` into a
``DraftTracker(config,
expected_settings=default_expected_settings(config),
value_sheet=value_map(rows))``, hold each board before polling the next
tick, and price nominees with ``analyze_player`` on that PRE-sale board
(:func:`replay_records` is the reference) — but each builds its own
sheet: this backtest fits the honest history sheet with
:func:`build_history_price_sheet`, while the dashboard derives its demo
sheet from the replay's own hammer prices
(``draftbot.dashboard.sheets.replay_sheet``) unless ``--sheet``
supplies a real one. Everything is offline and deterministic: two runs
emit byte-identical reports.

Run as ``python -m draftbot.backtest`` to (re)generate the committed
report at ``reports/backtest_2025.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from draftbot.config import LeagueConfig, load_config
from draftbot.draft_engine import INFLATION_MIN, analyze_player, taper_weight
from draftbot.models import parse_picks
from draftbot.sources import ReplaySource
from draftbot.tracker import DraftTracker, default_expected_settings
from draftbot.valuation import (
    FLOOR_PRICE,
    PriceModel,
    SheetRow,
    build_bids,
    detect_keeper_picks,
    parse_projections,
    value_map,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The committed fixtures the backtest replays (both offline).
DEFAULT_HISTORY = REPO_ROOT / "tests" / "fixtures" / "league_history.json"
DEFAULT_DRAFT = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"

#: Where the committed report lives.
DEFAULT_OUT = REPO_ROOT / "reports" / "backtest_2025.md"

#: The season being replayed; the price fit uses strictly earlier years.
DEFAULT_SEASON = 2025

#: Round reported statistics to this many decimals (determinism; the
#: report prints two decimals, tests compare at full precision).
_QUANTIZE_DECIMALS = 10

# --- The gate's bounds: measured FIRST, then bounded with a documented ---
# --- margin (reports/backtest_2025.md derives every number here). Each ---
# --- bound's tightness guard in the tests asserts bound - measured <=  ---
# --- margin, so a stale loose bound that a broken engine could slip    ---
# --- under is itself a test failure.                                   ---

#: Running (inflation-adjusted) estimate, overall MAE: measured 5.5376
#: over the 159 scored lots.
RUNNING_MAE_BOUND = 6.5
RUNNING_MAE_MARGIN = 1.0

#: Running estimate, early/mid/late bias spread: measured 11.7962
#: (segment bias -11.81 / -2.69 / -0.02). A REGRESSION PIN of a real,
#: explained deflation drift — NOT a no-drift certificate; see the
#: "Findings" section of the report before trusting the early-board
#: running estimate on draft night.
RUNNING_DRIFT_SPREAD_BOUND = 13.0
RUNNING_DRIFT_SPREAD_MARGIN = 1.5

#: Static sheet price, overall MAE: measured 2.2388 — the history fit's
#: calibration claim.
STATIC_MAE_BOUND = 2.75
STATIC_MAE_MARGIN = 0.6

#: Static sheet price, early/mid/late bias spread: measured 1.9351. The
#: honest no-systematic-drift assertion lives here.
STATIC_DRIFT_SPREAD_BOUND = 2.5
STATIC_DRIFT_SPREAD_MARGIN = 0.75

#: Static sheet price, absolute overall bias: measured 0.5522.
STATIC_BIAS_BOUND = 1.5
STATIC_BIAS_MARGIN = 1.0


def _quantize(value: float) -> float:
    return round(value, _QUANTIZE_DECIMALS)


def _season_rows(history: Mapping, year: int) -> dict:
    """One season's projections through the REAL parser, rebuilt from the
    history fixture's compact shape (``players[pid] = {adp: {year: adp},
    exp, pos}``; per the fixture's note, ``exp`` is the current-snapshot
    value in every season, exactly as the live API serves it)."""
    return parse_projections(
        [
            {
                "player_id": player_id,
                "stats": {
                    "adp_half_ppr": entry["adp"][str(year)],
                    "pts_half_ppr": None,
                },
                "player": {"position": entry["pos"], "years_exp": entry["exp"]},
            }
            for player_id, entry in sorted(history["players"].items())
            if str(year) in entry["adp"]
        ]
    )


def _fit_picks(history: Mapping, years: Sequence[int]) -> dict:
    """The fit years' picks feeds through the REAL parser (compact shape:
    ``picks[year] = [[player_id, position, amount, slot], ...]``)."""
    return {
        year: parse_picks(
            [
                {
                    "player_id": player_id,
                    "draft_slot": slot,
                    "metadata": {"amount": str(amount), "position": position},
                }
                for player_id, position, amount, slot in history["picks"][str(year)]
            ]
        )
        for year in years
    }


def build_history_price_sheet(
    history: Mapping, config: LeagueConfig, *, season: int
) -> list[SheetRow]:
    """The honest backtest sheet: what the bot could have printed BEFORE
    the ``season`` draft. Room prices are fit on the winning bids of the
    seasons strictly before ``season`` only (unflagged keeper rows
    detected and excluded, the production fit path), then applied to
    ``season``'s preseason ADP. The replayed season's own amounts never
    reach the fit.

    ``worth`` and ``value`` both carry the fitted room price and the
    keeper premium is zero by decision: the fit is empirical, so the
    room's keeper appetite is already inside the 2023-24 bids, and adding
    the model's premium on top would double-count it in the market
    estimate under test. Rows rank by price (ties by player id); players
    without a ``season`` ADP stay off the sheet, exactly as the
    production sheet omits players it cannot price.
    """
    fit_years = sorted(int(year) for year in history["picks"] if int(year) < season)
    seasons = {year: _season_rows(history, year) for year in (*fit_years, season)}
    slot_maps = {
        int(year): {int(slot): roster for slot, roster in mapping.items()}
        for year, mapping in history["slot_to_roster_id"].items()
        if int(year) in fit_years
    }
    picks = _fit_picks(history, fit_years)
    bids = build_bids(picks, seasons, exclude=detect_keeper_picks(picks, slot_maps))
    prices = PriceModel(
        bids,
        band_ratio=config.band_ratio,
        min_band_samples=config.min_band_samples,
        curve_cap=config.curve_cap,
    )
    priced = sorted(
        (
            (_quantize(prices.room_price(row.position, row.adp)), player_id, row)
            for player_id, row in seasons[season].items()
            if row.adp is not None
        ),
        key=lambda entry: (-entry[0], entry[1]),
    )
    return [
        SheetRow(
            rank=rank,
            player_id=player_id,
            name=player_id,  # the history fixture carries no names
            position=row.position,
            adp=row.adp,
            points=None,
            worth=price,
            room_price=price,
            price_source=prices.price_source(row.position, row.adp),
            keeper_premium=0.0,
            value=price,
        )
        for rank, (price, player_id, row) in enumerate(priced, start=1)
    ]


@dataclass(frozen=True)
class BacktestRecord:
    """One 2025 sale, scored: the three estimates the engine produced on
    the pre-sale board next to the dollars the room actually paid."""

    # pylint: disable=too-many-instance-attributes  # derived record type:
    # one field per number the report shows for a scored lot.

    lot: int  # 1-based sale order
    player_id: str
    name: str
    position: str | None  # None for an off-sheet nominee
    actual: int  # the winning bid
    running: float  # PlayerAnalysis.inflation_adjusted
    static: float  # the sheet price (0.0 on an off-model lot: unscored)
    max_bid: int  # the engine's bid ceiling (policy, not prediction)
    inflation: float  # the position's inflation ratio at the sale moment
    off_model: bool  # the sheet does not price this player


def _pick_names(raw_picks: Sequence[Mapping]) -> dict[str, str]:
    """Player id -> display name, straight from the picks feed's metadata
    (the history sheet carries no names)."""
    names = {}
    for pick in raw_picks:
        metadata = pick.get("metadata") or {}
        name = " ".join(
            part
            for part in (metadata.get("first_name"), metadata.get("last_name"))
            if part
        )
        names[str(pick["player_id"])] = name or str(pick["player_id"])
    return names


def replay_records(
    raw_draft: Mapping,
    raw_picks: Sequence[Mapping],
    rows: Sequence[SheetRow],
    config: LeagueConfig,
) -> tuple[BacktestRecord, ...]:
    """Replay the draft sale by sale and score the engine at every moment.

    THE PRE-SALE SEAM (the contract from ``draft_engine.py`` and the
    reference replay in the engine's own suite): each nominee is priced on
    the board as it stood BEFORE his own sale folded into the tracker —
    the state at the moment of nomination. Pricing on the post-sale board
    contaminates the comparison: the player has left his own position's
    remaining pool, his dollars have already debited the room, and on lots
    my team won he already sits on my roster.

    The 2025 feed carries no keepers (each team's 15 slots are all
    purchases and every ``keeper_count`` is zero), so no ``keepers_by_slot``
    is needed; the engine's keeper guard would refuse a keeper board.
    """
    source = ReplaySource(dict(raw_draft), list(raw_picks))
    tracker = DraftTracker(
        config,
        expected_settings=default_expected_settings(config),
        value_sheet=value_map(rows),
    )
    names = _pick_names(raw_picks)
    feed_positions = {
        str(pick["player_id"]): (pick.get("metadata") or {}).get("position")
        for pick in raw_picks
    }
    static = {row.player_id: row.worth for row in rows}
    board = tracker.update(source.poll())
    records = []
    for _ in range(len(raw_picks)):
        if board.status == "complete":
            break
        pre_sale = board
        board = tracker.update(source.poll())
        sale = board.sales[-1]
        analysis = analyze_player(sale.player_id, rows, pre_sale, config)
        records.append(
            BacktestRecord(
                lot=len(board.sales),
                player_id=sale.player_id,
                name=names[sale.player_id],
                # Sheet position for a priced player; the feed's own label
                # for an off-sheet one (the report's exclusion table still
                # names the position even though the sheet cannot).
                position=analysis.position or feed_positions.get(sale.player_id),
                actual=sale.amount or 0,
                running=analysis.inflation_adjusted,
                # An off-model lot has no sheet price: an explicit 0.0
                # (never scored, never printed), not a live-looking
                # fallback to the engine's off-sheet worth.
                static=static.get(sale.player_id, 0.0),
                max_bid=analysis.max_bid,
                inflation=analysis.inflation,
                off_model=analysis.rank is None,
            )
        )
    return tuple(records)


@dataclass(frozen=True)
class ErrorStats:
    """Mean signed and mean absolute error of one estimate over some lots."""

    n: int
    mae: float
    bias: float


def scored_records(
    records: Sequence[BacktestRecord],
) -> tuple[BacktestRecord, ...]:
    """The lots the statistics score: everything the sheet prices."""
    return tuple(record for record in records if not record.off_model)


def off_model_records(
    records: Sequence[BacktestRecord],
) -> tuple[BacktestRecord, ...]:
    """The excluded lots, for the report's exclusion table."""
    return tuple(record for record in records if record.off_model)


def overall_stats(records: Sequence[BacktestRecord], estimate: str) -> ErrorStats:
    """Overall MAE/bias of one estimate, off-model lots excluded: a sale
    the sheet cannot price measures nothing about the sheet's estimates."""
    return error_stats(scored_records(records), estimate)


@dataclass(frozen=True)
class SegmentStats:
    """One third of the draft, by sale order, with its error statistics."""

    label: str  # "early", "mid", "late"
    first_lot: int
    last_lot: int
    stats: ErrorStats  # scored lots only; the span still counts off-model


def segment_stats(
    records: Sequence[BacktestRecord], estimate: str
) -> tuple[SegmentStats, ...]:
    """The early/mid/late drift comparison: the lots in sale order, split
    into three contiguous thirds (60/60/60 on the 180-lot draft — lot 60
    is the last early lot, lot 61 the first mid lot), each scored with
    off-model lots excluded from its statistics but not from its span."""
    ordered = sorted(records, key=lambda record: record.lot)
    third = len(ordered) // 3
    chunks = (ordered[:third], ordered[third : 2 * third], ordered[2 * third :])
    return tuple(
        SegmentStats(
            label=label,
            first_lot=chunk[0].lot,
            last_lot=chunk[-1].lot,
            stats=error_stats(scored_records(chunk), estimate),
        )
        for label, chunk in zip(("early", "mid", "late"), chunks)
    )


def position_stats(
    records: Sequence[BacktestRecord], estimate: str
) -> dict[str, ErrorStats]:
    """Per-position MAE/bias over the scored lots, keys sorted. Positions
    never cancel against each other: an RB overestimate and a WR
    underestimate are two findings, not zero."""
    scored = scored_records(records)
    positions = sorted({record.position for record in scored})
    return {
        position: error_stats(
            [record for record in scored if record.position == position], estimate
        )
        for position in positions
    }


def drift_spread(segments: Sequence[SegmentStats]) -> float:
    """The between-segment bias spread the drift gate bounds: max segment
    bias minus min segment bias, in dollars."""
    biases = [segment.stats.bias for segment in segments]
    return _quantize(max(biases) - min(biases))


def error_stats(records: Sequence[BacktestRecord], estimate: str) -> ErrorStats:
    """MAE and bias of ``estimate`` ("running", "static", or "max_bid")
    over ``records``: error = estimate - actual, bias the mean signed
    error, MAE the mean absolute — two genuinely different numbers (a +5
    and a -5 miss average to zero bias but a $5 MAE)."""
    errors = [getattr(record, estimate) - record.actual for record in records]
    return ErrorStats(
        n=len(errors),
        mae=_quantize(sum(abs(error) for error in errors) / len(errors)),
        bias=_quantize(sum(errors) / len(errors)),
    )


def _table(header: Sequence[str], body: Sequence[Sequence[str]]) -> list[str]:
    """A markdown table as lines (fixed formatting, byte-stable)."""
    return [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
        *("| " + " | ".join(cells) + " |" for cells in body),
    ]


def _segment_table(
    records: Sequence[BacktestRecord], estimates: Sequence[tuple[str, str]]
) -> list[str]:
    """Early/mid/late rows with MAE and bias per requested estimate."""
    columns = [segment_stats(records, estimate) for estimate, _ in estimates]
    header = ["segment", "lots", "n"]
    for _, label in estimates:
        header += [f"{label} MAE", f"{label} bias"]
    body = []
    for index, segment in enumerate(columns[0]):
        cells = [
            segment.label,
            f"{segment.first_lot}-{segment.last_lot}",
            str(segment.stats.n),
        ]
        for column in columns:
            cells += [
                f"{column[index].stats.mae:.2f}",
                f"{column[index].stats.bias:+.2f}",
            ]
        body.append(cells)
    return _table(header, body)


def _overall_row(records: Sequence[BacktestRecord], estimate: str) -> str:
    """An overall row closing a segment table: the same statistic over
    every scored lot, so a reader can check the report against the
    overall number the PR and the summary line quote."""
    stats = overall_stats(records, estimate)
    span = f"{records[0].lot}-{records[-1].lot}"
    return f"| overall | {span} | {stats.n} | {stats.mae:.2f} | {stats.bias:+.2f} |"


def _report_intro(records: Sequence[BacktestRecord], rows: Sequence[SheetRow]) -> str:
    scored = scored_records(records)
    excluded = off_model_records(records)
    sources: dict[str, int] = {}
    for row in rows:
        sources[row.price_source] = sources.get(row.price_source, 0) + 1
    source_note = ", ".join(
        f"{count} {label}" for label, count in sorted(sources.items())
    )
    total = sum(record.actual for record in records)
    return f"""# 2025 replay backtest

The real 2025 auction — all {len(records)} sales, ${total} of winning
bids — replayed sale by sale through the tracker and the repricing
engine. At every sale moment the engine prices the nominee on the board
as it stood BEFORE that sale folded in (the pre-sale seam contract), and
the resulting estimates are scored against the amount the room actually
paid.

Method, in five rules:

- **Honest sheet.** Room prices are fit on the 2023-2024 winning bids
  only (the unflagged-keeper detection path runs on the fit and finds
  none in 2023-2024; the exclusion wiring itself is pinned by test on
  a 2025-inclusive fit) and applied to 2025 preseason ADP — the
  post-preseason snapshot, approximately draft-morning: nothing the
  bot could not have known on 2025 draft night. The sheet prices
  {len(rows)} players ({source_note}).
  The keeper premium is deliberately zero: the empirical fit already
  contains the room's keeper appetite, and stacking the model's premium
  on top would double-count it in a market estimate.
- **Pre-sale boards.** A nominee is never priced on a board that already
  contains his own sale (post-sale pricing removes him from his own
  pool, debits his own dollars, and on lots my team won collapses the
  bid to a bench-retention number).
- **Three estimates.** *running* = the engine's inflation-adjusted price
  (the market estimate under test); *static* = the sheet price alone
  (the diagnostic baseline); *max bid* = the engine's bid ceiling —
  bid POLICY, reported only as a labeled secondary comparison.
- **Error convention.** error = estimate - actual; bias is the mean
  signed error (negative = the estimate ran under the room), MAE the
  mean absolute error.
- **Exclusions.** Sales the sheet does not price are off-model: flagged
  by the engine, excluded from every statistic, counted and listed
  below. {len(excluded)} of {len(records)} lots are off-model here
  (${sum(r.actual for r in excluded)} of the room's ${total});
  {len(scored)} lots are scored.
"""


def _report_finding(records: Sequence[BacktestRecord], rows: Sequence[SheetRow]) -> str:
    scored = scored_records(records)
    run = overall_stats(records, "running")
    static = overall_stats(records, "static")
    early, mid, late = segment_stats(records, "running")
    opening = scored[0].inflation
    clamped = sum(1 for record in scored if record.inflation == INFLATION_MIN)
    sold = {record.player_id for record in records}
    unsold = sum(
        taper_weight(row.rank) * max(0.0, row.worth - FLOOR_PRICE)
        for row in rows
        if row.player_id not in sold
    )
    return f"""## Finding: the running estimate deflates the early board

The calibration lives in the static sheet: MAE {static.mae:.2f}, bias
{static.bias:+.2f} — this room's own 2023-2024 prices predict its 2025
prices to within about two dollars a lot, with no meaningful drift
(segment table above). The running estimate then multiplies that sheet
by remaining-money-over-remaining-value per position, and that ratio
opens at {opening:.3f} and, on RB and WR (the positions carrying
the drift), only falls: the engine's denominator counts EVERY
unsold sheet row as competing for the room's money, but an
auction only absorbs 180 lots. The overhang never clears — when the
last lot closes, ${unsold:.0f} of taper-weighted above-floor sheet
value is still unsold against the $1464 of discretionary money the
room started with (the twelve 2025 budgets minus the $1 every roster
slot pins), so about 76% of a room's money worth of priced value never
sells. RB and WR fall to {INFLATION_MIN:.2f} before the opening third
is out and never leave it — and {INFLATION_MIN:.2f} is the engine's
INFLATION_MIN clamp, not a market reading; the raw ratio would keep
falling without it. {clamped} of the {run.n} scored lots price at
exactly that clamp. The resulting drift: the early board runs
{early.stats.bias:+.2f} per lot, and the bias then shrinks
({mid.stats.bias:+.2f} mid, {late.stats.bias:+.2f} late) NOT because
the ratio recovers — it never regains more than a few cents — but
because mid and late lots carry too little taper-weighted above-floor
worth for a wrong ratio to move. Overall the running estimate scores
MAE {run.mae:.2f}, bias {run.bias:+.2f} — strictly worse than the
static sheet it adjusts, on this fixture.

The shape is not an artifact of the sheet's normalization basis:
rescaling the sheet's above-floor prices so the engine's
worth-normalization contract holds exactly, or cutting the pool to the
top 180, reproduced the same early-deep, late-flat drift within a
fraction of a dollar. (Development measurements against the same
fixtures: neither variant is regenerated by this report or pinned by
the suite.) Draft-night reading: trust the static column for any
expensive lot after the opening stretch — the clean late-board numbers
above reflect 2025's cheap tail, not a recovered ratio, and an
expensive nominee arriving late would still be deflated — and on the
opening stretch itself treat the running estimate as a floor, not a
fair price, whenever the room's budgets sit far below the sheet's pool
value.
"""


def _report_gate(records: Sequence[BacktestRecord]) -> list[str]:
    run = overall_stats(records, "running")
    static = overall_stats(records, "static")
    run_spread = drift_spread(segment_stats(records, "running"))
    static_spread = drift_spread(segment_stats(records, "static"))
    body = [
        (
            "running MAE",
            f"{run.mae:.2f}",
            f"{RUNNING_MAE_BOUND:.2f}",
            f"{RUNNING_MAE_MARGIN:.2f}",
        ),
        (
            "running bias spread (early/mid/late)",
            f"{run_spread:.2f}",
            f"{RUNNING_DRIFT_SPREAD_BOUND:.2f}",
            f"{RUNNING_DRIFT_SPREAD_MARGIN:.2f}",
        ),
        (
            "static MAE",
            f"{static.mae:.2f}",
            f"{STATIC_MAE_BOUND:.2f}",
            f"{STATIC_MAE_MARGIN:.2f}",
        ),
        (
            "static bias spread (early/mid/late)",
            f"{static_spread:.2f}",
            f"{STATIC_DRIFT_SPREAD_BOUND:.2f}",
            f"{STATIC_DRIFT_SPREAD_MARGIN:.2f}",
        ),
        (
            "static absolute bias",
            f"{abs(static.bias):.2f}",
            f"{STATIC_BIAS_BOUND:.2f}",
            f"{STATIC_BIAS_MARGIN:.2f}",
        ),
    ]
    return [
        "## The pytest gate",
        "",
        "Bounds were set AFTER measuring (the values below), with the",
        "stated margins as regression headroom. The suite asserts each",
        "metric under its bound AND each bound within its margin of the",
        "measured value, so a loose stale bound fails the same as a",
        "regression. The running bias spread bound is a REGRESSION PIN of",
        "the measured deflation drift documented above — deliberately not",
        "a no-systematic-drift certificate; that claim is asserted where",
        "it is true, on the static sheet.",
        "",
        *_table(("metric", "measured", "bound", "margin"), body),
    ]


def render_report(records: Sequence[BacktestRecord], rows: Sequence[SheetRow]) -> str:
    """The full backtest report as deterministic markdown: fixed
    precision, sorted iteration, no clocks — two runs over the same
    fixtures are byte-identical."""
    scored = scored_records(records)
    run = overall_stats(records, "running")
    lines: list[str] = [_report_intro(records, rows)]
    lines += [
        "## Headline: the running estimate (gate metric)",
        "",
        *_table(
            ("estimate", "n", "MAE", "bias"),
            [
                (
                    "running (inflation-adjusted)",
                    str(run.n),
                    f"{run.mae:.2f}",
                    f"{run.bias:+.2f}",
                )
            ],
        ),
        "",
        "## Per-position",
        "",
    ]
    positions = position_stats(records, "running")
    static_positions = position_stats(records, "static")
    lines += _table(
        ("position", "n", "running MAE", "running bias", "static MAE", "static bias"),
        [
            (
                position,
                str(stats.n),
                f"{stats.mae:.2f}",
                f"{stats.bias:+.2f}",
                f"{static_positions[position].mae:.2f}",
                f"{static_positions[position].bias:+.2f}",
            )
            for position, stats in positions.items()
        ],
    )
    lines += [
        "",
        "## Early/mid/late drift",
        "",
        *_segment_table(records, (("running", "running"), ("static", "static"))),
        "",
        f"Running bias spread: {drift_spread(segment_stats(records, 'running')):.2f}."
        f" Static bias spread: {drift_spread(segment_stats(records, 'static')):.2f}.",
        "",
        _report_finding(records, rows),
        "## Worst running misses",
        "",
        *_table(
            ("lot", "player", "pos", "static", "running", "actual"),
            [
                (
                    str(record.lot),
                    record.name,
                    record.position or "-",
                    f"{record.static:.2f}",
                    f"{record.running:.2f}",
                    str(record.actual),
                )
                for record in sorted(
                    scored,
                    key=lambda r: (-_quantize(abs(r.running - r.actual)), r.lot),
                )[:8]
            ],
        ),
        "",
        "## Off-model lots (excluded from every statistic)",
        "",
        *_table(
            ("lot", "player", "pos", "actual"),
            [
                (
                    str(record.lot),
                    record.name,
                    record.position or "-",
                    str(record.actual),
                )
                for record in off_model_records(records)
            ],
        ),
        "",
        *_report_gate(records),
        "",
        "## Secondary: the max-bid ceiling (bid policy, not a prediction)",
        "",
        "The final max bid folds in MY roster need and the spend-down",
        "schedule; it is what the engine would let me pay, not what it",
        "thinks the room will pay. Shown for completeness only:",
        "",
        *_segment_table(records, (("max_bid", "max bid"),)),
        _overall_row(records, "max_bid"),
        "",
        "## Reproducing",
        "",
        "```",
        "python -m draftbot.backtest",
        "```",
        "",
        "Reads `tests/fixtures/league_history.json` and",
        "`tests/fixtures/draft_2025.json`, rewrites this report in place",
        "(`reports/backtest_2025.md`). Fully offline and deterministic;",
        "`tests/test_backtest_replay.py` regenerates it and fails if the",
        "committed copy differs by a byte.",
    ]
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m draftbot.backtest",
        description="Replay the 2025 draft through the engine and write "
        "the backtest report.",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "league_config.toml"),
        help="league config TOML (defaults to the checked-in league_config.toml)",
    )
    parser.add_argument(
        "--history",
        default=str(DEFAULT_HISTORY),
        help="league history fixture (picks feeds + ADP per season)",
    )
    parser.add_argument(
        "--draft",
        default=str(DEFAULT_DRAFT),
        help="replay fixture (the draft object and its full picks feed)",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=DEFAULT_SEASON,
        help="season being replayed; the price fit uses earlier years only",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="report output path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    """Build the sheet, replay the draft, write the report.

    ``out`` is injectable for tests. Exit codes: 0 report written; 2
    nothing produced (missing or unparseable config or fixture, or a
    replay in which the sheet prices no sale at all — statistics over
    zero scored lots are undefined, not zero).
    """
    args = _parse_args(argv)
    stream = out if out is not None else sys.stdout
    err = out if out is not None else sys.stderr
    try:
        config = load_config(args.config)
        history = json.loads(Path(args.history).read_text(encoding="utf-8"))
        data = json.loads(Path(args.draft).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        print(f"error: file not found: {error.filename}", file=err)
        return 2
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, KeyError) as error:
        print(f"error: unparseable input: {error}", file=err)
        return 2

    rows = build_history_price_sheet(history, config, season=args.season)
    records = replay_records(data["draft"], data["picks"], rows, config)
    if not scored_records(records):
        print(
            "error: no scored lots: the sheet prices none of the replayed sales",
            file=err,
        )
        return 2
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" keeps the committed artifact byte-stable across
    # platforms (the regenerate-and-compare test reads exact bytes).
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_report(records, rows))

    print(_summary_line(records), file=stream)
    print(f"wrote {out_path}", file=stream)
    return 0


def _summary_line(records: Sequence[BacktestRecord]) -> str:
    """The one-line stdout summary of the headline numbers."""
    run = overall_stats(records, "running")
    static = overall_stats(records, "static")
    spread = drift_spread(segment_stats(records, "running"))
    return (
        f"scored {run.n} of {len(records)} lots | "
        f"running MAE {run.mae:.2f} bias {run.bias:+.2f} "
        f"(drift spread {spread:.2f}) | "
        f"static MAE {static.mae:.2f} bias {static.bias:+.2f}"
    )


if __name__ == "__main__":
    sys.exit(main())
