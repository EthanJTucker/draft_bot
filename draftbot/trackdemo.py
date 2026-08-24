"""Replay demo: drive the draft tracker through a historical draft.

Run as ``python -m draftbot.trackdemo``. Replays a past season's draft
(default 2025) sale by sale: each pick prints the buying team's updated
state, the full board prints every ``--table-every`` sales and at the end,
and the final table verifies each team's total spend against the draft
object's own ``budget_<slot>`` values.
"""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable, TextIO

from draftbot.config import load_config
from draftbot.models import Pick
from draftbot.sleeper_client import SleeperClient, SleeperUnavailableError
from draftbot.sources import ReplaySource
from draftbot.tracker import (
    BoardState,
    DraftTracker,
    TeamState,
    default_expected_settings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The 10-second timers are league facts (see GAMEPLAN), not config keys;
# the demo states them as data for the settings-differ check.
EXPECTED_TIMERS = {"nomination_timer": 10, "pick_timer": 10}


def _player_name(pick: Pick) -> str:
    parts = (pick.metadata.get("first_name"), pick.metadata.get("last_name"))
    return " ".join(part for part in parts if part) or pick.player_id


def _needs_label(team: TeamState) -> str:
    holes = " ".join(f"{label}:{count}" for label, count in team.needs.items() if count)
    return holes or "-"


def _team_line(team: TeamState) -> str:
    default_mark = "*" if team.budget_is_default else " "
    return (
        f"  slot {team.slot:>2}  budget ${team.budget:>3}{default_mark} "
        f"spent ${team.spent:>3}  left ${team.remaining:>3}  "
        f"open {team.open_slots:>2}  max ${team.max_bid:>3}  "
        f"needs {_needs_label(team)}"
    )


def _board_lines(board: BoardState) -> list[str]:
    lines = [f"Board ({board.status}{' PAUSED' if board.paused else ''}):"]
    nomination = board.nomination
    if nomination.player_id is not None:
        lines.append(
            f"  nominee {nomination.player_id}: {nomination.status}"
            + (f", high bid ${nomination.highest_offer}" if nomination.is_live else "")
        )
    lines.extend(_team_line(team) for team in board.teams)
    if board.stale_endpoints:
        lines.append(
            "  WARNING - served from cache, not live: "
            + ", ".join(sorted(board.stale_endpoints))
        )
    return lines


def _warning_lines(board: BoardState) -> list[str]:
    return [
        f"WARNING - settings differ: {warning.field}: "
        f"expected {warning.expected}, live {warning.actual}"
        for warning in board.settings_warnings
    ]


def _pick_line(pick: Pick, team: TeamState) -> str:
    return (
        f"pick {pick.pick_no:>3}: {_player_name(pick)} "
        f"({pick.metadata.get('position') or '?'}) ${pick.amount or 0} "
        f"-> slot {pick.draft_slot:>2} | left ${team.remaining}, "
        f"open {team.open_slots}, max ${team.max_bid}"
    )


def _verification_lines(board: BoardState) -> tuple[list[str], bool]:
    """The final check: no team's spend may exceed its own slot budget.

    Leftover money is data, not an error — real auctions end with some
    (four 2025 teams kept $1-8) — so the pass mark is ``spent <= budget``
    per team, with the exact leftover printed for eyeballing.
    """
    lines = ["Final budgets (spent vs the draft object's budget_<slot>):"]
    all_ok = True
    for team in board.teams:
        ok = team.remaining >= 0
        all_ok = all_ok and ok
        lines.append(
            f"  slot {team.slot:>2}  budget ${team.budget:>3}  "
            f"spent ${team.spent:>3}  left ${team.remaining:>3}  "
            f"{'[OK]' if ok else '[OVERSPENT]'}"
        )
    return lines, all_ok


def _load_config_or_none(config_path: Path, err: TextIO):
    try:
        return load_config(config_path)
    except FileNotFoundError:
        print(f"error: config file not found: {config_path}", file=err)
    except tomllib.TOMLDecodeError as error:
        print(f"error: config file is not valid TOML: {config_path}: {error}", file=err)
    except KeyError as error:
        print(
            f"error: config file {config_path} is missing required key {error}",
            file=err,
        )
    return None


def main(
    argv: list[str] | None = None,
    http_get: Callable[[str], bytes] | None = None,
    clock: Callable[[], float] = time.time,
    out: TextIO | None = None,
) -> int:
    """Replay the chosen historical draft through the tracker.

    ``http_get``, ``clock``, and ``out`` are injectable for tests; the
    real CLI fetches the draft live once (then serves the per-id cache).

    Exit codes: 0 replay completed and no team overspent its slot budget;
    1 completed but some team's spend exceeds its budget; 2 nothing ran
    (missing, malformed, or incomplete config, an unknown season, or a
    draft that could not be fetched and has no cache).
    """
    parser = argparse.ArgumentParser(
        prog="python -m draftbot.trackdemo", description=__doc__
    )
    parser.add_argument("--config", default=str(REPO_ROOT / "league_config.toml"))
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument(
        "--year",
        default="2025",
        help="historical season to replay (a [league.historical_drafts] key)",
    )
    parser.add_argument(
        "--table-every",
        type=int,
        default=30,
        help="print the full board every N sales (0 disables)",
    )
    args = parser.parse_args(argv)

    if out is None and hasattr(sys.stdout, "reconfigure"):
        # Player names are user data; never let a Windows console encoding
        # crash the replay.
        sys.stdout.reconfigure(errors="replace")
    stream = out if out is not None else sys.stdout
    err = out if out is not None else sys.stderr

    config = _load_config_or_none(Path(args.config), err)
    if config is None:
        return 2
    draft_id = config.historical_draft_ids.get(args.year)
    if draft_id is None:
        print(
            f"error: no historical draft for {args.year} "
            f"(have: {', '.join(sorted(config.historical_draft_ids))})",
            file=err,
        )
        return 2

    client = SleeperClient(
        config,
        cache_dir=Path(args.cache_dir) if args.cache_dir is not None else None,
        http_get=http_get,
        clock=clock,
    )
    try:
        raw_draft = client.get_draft(draft_id=draft_id)
        raw_picks = client.get_picks(draft_id=draft_id)
    except SleeperUnavailableError as error:
        print(f"error: {error}", file=err)
        return 2

    return _replay(
        config,
        args=args,
        draft_id=draft_id,
        raw_draft=raw_draft,
        raw_picks=raw_picks,
        stream=stream,
    )


def _replay(  # pylint: disable=too-many-arguments  # internal seam between
    # the CLI plumbing and the replay loop; every keyword is used once.
    config,
    *,
    args,
    draft_id,
    raw_draft,
    raw_picks,
    stream: TextIO,
) -> int:
    source = ReplaySource(raw_draft, raw_picks)
    tracker = DraftTracker(
        config,
        expected_settings=default_expected_settings(config) | EXPECTED_TIMERS,
    )

    board = tracker.update(source.poll())
    print(
        f"Replaying {args.year} draft {draft_id}: "
        f"{len(raw_picks)} sales, {len(board.teams)} teams",
        file=stream,
    )
    for line in _warning_lines(board):
        print(line, file=stream)

    sales = 0
    for _ in range(len(raw_picks)):
        tick = source.poll()
        board = tracker.update(tick)
        sales = len(tick.picks)
        pick = tick.picks[-1]
        print(_pick_line(pick, board.team(pick.draft_slot)), file=stream)
        if args.table_every and sales % args.table_every == 0:
            for line in _board_lines(board):
                print(line, file=stream)

    print(f"\nReplay complete: {sales} sales.", file=stream)
    for line in _board_lines(board):
        print(line, file=stream)
    lines, all_ok = _verification_lines(board)
    for line in lines:
        print(line, file=stream)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
