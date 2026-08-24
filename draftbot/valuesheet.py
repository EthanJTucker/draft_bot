"""Value-sheet CLI: the full ranked, priced player pool for the season.

Fetches the three historical picks feeds and the 2023-current projections
(cached to disk by the client; a dead endpoint degrades to its cache),
builds the static pre-draft sheet — worth, room price, and NPV-adjusted
value per player — writes it as CSV, and prints the top of the board.

Run as ``python -m draftbot.valuesheet``.
"""

# pylint: disable=duplicate-code  # the argparse/config/exit-code plumbing
# intentionally parallels draftbot/snapshot.py (same CLI conventions);
# snapshot.py is outside this slice's file ownership, so the shared helper
# extraction that would deduplicate it is deferred to a later slice.

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Callable, TextIO

from draftbot.config import LeagueConfig, load_config
from draftbot.models import parse_picks
from draftbot.sleeper_client import SleeperClient, SleeperUnavailableError
from draftbot.valuation import SheetRow, build_value_sheet, parse_projections

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Rows shown in the printed table (the CSV always holds the full pool).
DEFAULT_TOP = 30

CSV_HEADER = (
    "rank",
    "player_id",
    "name",
    "position",
    "adp",
    "points",
    "worth",
    "room_price",
    "price_source",
    "keeper_premium",
    "value",
)


def _fetch_inputs(client: SleeperClient, config: LeagueConfig):
    """Historical picks and per-season projections, tracking degradation.

    Returns ``(picks_by_year, seasons, degraded)`` where ``degraded`` names
    endpoints served from the disk cache because the live fetch failed.
    """
    degraded: set[str] = set()

    def watch(label: str, payload):
        if client.last_fetch_degraded:
            degraded.add(label)
        return payload

    picks_by_year = {}
    for year_text in sorted(config.historical_draft_ids):
        draft_id = config.historical_draft_ids[year_text]
        raw = watch(f"picks {year_text}", client.get_picks(draft_id=draft_id))
        picks_by_year[int(year_text)] = parse_picks(raw)
    seasons = {}
    years = {int(year) for year in config.historical_draft_ids} | {config.season}
    for year in sorted(years):
        raw = watch(f"projections {year}", client.get_projections(season=year))
        seasons[year] = parse_projections(raw)
    return picks_by_year, seasons, degraded


def _csv_cells(row: SheetRow) -> list[str]:
    """One CSV record with fixed formatting (byte-identical across runs)."""
    return [
        str(row.rank),
        row.player_id,
        row.name,
        row.position,
        f"{row.adp:.1f}" if row.adp is not None else "",
        f"{row.points:.1f}" if row.points is not None else "",
        f"{row.worth:.2f}",
        f"{row.room_price:.2f}",
        row.price_source,
        f"{row.keeper_premium:.2f}",
        f"{row.value:.2f}",
    ]


def write_csv(rows: Sequence[SheetRow], path: Path) -> None:
    """Write the full sheet to ``path`` (parent directories created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for row in rows:
            writer.writerow(_csv_cells(row))


def format_table(rows: Sequence[SheetRow], top: int) -> list[str]:
    """The top of the board as fixed-width lines for the terminal."""
    lines = [
        f"{'rank':>4} {'player':<24} {'pos':<4} {'adp':>6} "
        f"{'worth':>7} {'room':>7} {'keep+':>6} {'value':>7}"
    ]
    for row in rows[:top]:
        adp = f"{row.adp:.1f}" if row.adp is not None else "-"
        lines.append(
            f"{row.rank:>4} {row.name[:24]:<24} {row.position:<4} {adp:>6} "
            f"{row.worth:>7.2f} {row.room_price:>7.2f} "
            f"{row.keeper_premium:>6.2f} {row.value:>7.2f}"
        )
    return lines


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m draftbot.valuesheet", description=__doc__
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "league_config.toml"),
        help="path to the league config TOML",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="cache directory (default: the config's cache dir)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="CSV output path (default: <config dir>/data/" "value_sheet_<season>.csv)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"rows to print as a table (default {DEFAULT_TOP})",
    )
    return parser.parse_args(argv)


def _resolve_out_path(args: argparse.Namespace, season: int) -> Path:
    """--out verbatim, else <config dir>/data/value_sheet_<season>.csv."""
    if args.out is not None:
        return Path(args.out)
    return Path(args.config).resolve().parent / "data" / f"value_sheet_{season}.csv"


def main(
    argv: list[str] | None = None,
    http_get: Callable[[str], bytes] | None = None,
    clock: Callable[[], float] = time.time,
    out: TextIO | None = None,
) -> int:
    """Build the sheet, write the CSV, print the top of the board.

    ``http_get``, ``clock``, and ``out`` are injectable for tests. Exit
    codes: 0 sheet written (cache-served endpoints are labeled with a
    warning); 2 nothing produced (bad config path, or an endpoint with
    neither a live response nor a cached copy).
    """
    args = _parse_args(argv)
    if out is None and hasattr(sys.stdout, "reconfigure"):
        # Player names are user-facing data; never let a Windows console
        # encoding crash the sheet print.
        sys.stdout.reconfigure(errors="replace")
    stream = out if out is not None else sys.stdout
    err = out if out is not None else sys.stderr

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"error: config file not found: {args.config}", file=err)
        return 2
    client = SleeperClient(
        config, cache_dir=args.cache_dir, http_get=http_get, clock=clock
    )
    try:
        picks_by_year, seasons, degraded = _fetch_inputs(client, config)
    except SleeperUnavailableError as error:
        print(f"error: {error}", file=err)
        return 2

    rows = build_value_sheet(seasons, picks_by_year, config)
    out_path = _resolve_out_path(args, config.season)
    write_csv(rows, out_path)
    if degraded:
        print(
            "WARNING - served from disk cache, NOT live (live fetch "
            "failed): " + ", ".join(sorted(degraded)),
            file=stream,
        )
    print(f"wrote {len(rows)} players to {out_path}", file=stream)
    print("\n".join(format_table(rows, args.top)), file=stream)
    return 0


if __name__ == "__main__":
    sys.exit(main())
