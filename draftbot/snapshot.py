"""Snapshot CLI: fetch every Sleeper endpoint to the disk cache and print
a league summary (teams, budgets, keeper counts, draft status).

Run as ``python -m draftbot.snapshot``.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from draftbot.config import LeagueConfig, load_config
from draftbot.models import DraftState, parse_draft, parse_picks, spent_by_slot
from draftbot.sleeper_client import SleeperClient

REPO_ROOT = Path(__file__).resolve().parent.parent


def _team_name(user: dict | None) -> str:
    if user is None:
        return "(unknown owner)"
    metadata = user.get("metadata") or {}
    return metadata.get("team_name") or user.get("display_name", "(unnamed)")


def _keeper_label(roster: dict) -> str:
    keepers = roster.get("keepers")
    if keepers is None:
        return "keepers: n/a (not shown by API)"
    return f"keepers: {len(keepers)}"


def _start_time_label(state: DraftState) -> str:
    if state.start_time is None:
        return "start time n/a"
    moment = datetime.fromtimestamp(state.start_time / 1000, tz=timezone.utc)
    return f"starts {moment.isoformat()}"


def _team_lines(config: LeagueConfig, data: dict, state: DraftState) -> list[str]:
    users_by_id = {user.get("user_id"): user for user in data.get("users", [])}
    spent = spent_by_slot(parse_picks(data.get("picks", [])))
    slot_by_roster = {
        roster_id: slot for slot, roster_id in state.slot_to_roster_id.items()
    }
    lines = []
    rosters = sorted(data.get("rosters", []), key=lambda r: r.get("roster_id", 0))
    for roster in rosters:
        slot = slot_by_roster.get(roster.get("roster_id"))
        budget = state.budget_by_slot.get(slot, config.auction_budget)
        slot_label = f"slot {slot:>2}" if slot is not None else "slot  ?"
        name = _team_name(users_by_id.get(roster.get("owner_id")))
        lines.append(
            f"  {slot_label}  {name:<24} "
            f"budget ${budget}  spent ${spent.get(slot, 0)}  "
            f"{_keeper_label(roster)}"
        )
    return lines


def build_summary(config: LeagueConfig, data: dict) -> str:
    """Render the league summary from a full endpoint snapshot."""
    league = data.get("league", {})
    state = parse_draft(data.get("draft", {}))
    status = state.status + (" [PAUSED]" if state.paused else "")
    lines = [
        (
            f"League: {league.get('name', '(unknown)')} — "
            f"season {league.get('season', '?')}, "
            f"{league.get('total_rosters', config.teams)} teams, "
            f"${config.auction_budget} auction"
        ),
        (
            f"Draft {state.draft_id}: {status} ({state.draft_type}), "
            f"{_start_time_label(state)}"
        ),
    ]
    if not state.budget_by_slot:
        lines.append(
            "Budgets: budgets not yet entered by the commissioner — "
            f"all teams shown at the ${config.auction_budget} default"
        )
    lines.append("Teams (by draft slot):")
    lines.extend(_team_lines(config, data, state))
    lines.append(
        f"Cached: {len(data.get('players', {}))} players in map, "
        f"{len(data.get('projections', []))} projection rows, "
        f"{len(data.get('picks', []))} picks"
    )
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    http_get: Callable[[str], bytes] | None = None,
    clock: Callable[[], float] = time.time,
    out: TextIO | None = None,
) -> int:
    """Snapshot every endpoint to the disk cache and print the summary.

    ``http_get``, ``clock``, and ``out`` are injectable for tests; the real
    CLI uses the requests transport, the wall clock, and stdout.
    """
    parser = argparse.ArgumentParser(
        prog="python -m draftbot.snapshot", description=__doc__
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "league_config.toml"),
        help="path to the league config TOML",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="cache directory (default: the config's cache dir, "
        "resolved against the config file's folder)",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = load_config(config_path)
    if args.cache_dir is not None:
        cache_dir = Path(args.cache_dir)
    else:
        cache_dir = config_path.resolve().parent / config.cache_dir
    client = SleeperClient(config, cache_dir=cache_dir, http_get=http_get, clock=clock)
    data = client.snapshot_all()
    print(build_summary(config, data), file=out if out is not None else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
