"""Snapshot CLI: fetch every Sleeper endpoint to the disk cache and print
a league summary (teams, budgets, keeper counts, draft status).

Run as ``python -m draftbot.snapshot``.
"""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Callable, TextIO

from draftbot.config import LeagueConfig, load_config
from draftbot.models import DraftState, parse_draft, parse_picks, spent_by_slot
from draftbot.sleeper_client import SleeperClient, SleeperUnavailableError

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


def _start_time_label(state: DraftState, tz: tzinfo | None = None) -> str:
    if state.start_time is None:
        return "start time n/a"
    moment = datetime.fromtimestamp(state.start_time / 1000, tz=timezone.utc)
    # tz=None renders in the machine's local timezone: a Sunday-evening
    # Denver draft must not read as Monday UTC. Injectable for tests.
    local = moment.astimezone(tz)
    return f"starts {local.strftime('%Y-%m-%d %H:%M %Z')}"


def _team_lines(config: LeagueConfig, data: dict, state: DraftState) -> list[str]:
    users_by_id = {user.get("user_id"): user for user in data.get("users", [])}
    spent = spent_by_slot(parse_picks(data.get("picks", [])))
    slot_by_roster = {
        roster_id: slot for slot, roster_id in state.slot_to_roster_id.items()
    }

    def slot_order(roster: dict):
        slot = slot_by_roster.get(roster.get("roster_id"))
        # Slot order (unknown slots last), roster id as the tiebreaker.
        return (slot is None, slot or 0, roster.get("roster_id", 0))

    lines = []
    for roster in sorted(data.get("rosters", []), key=slot_order):
        slot = slot_by_roster.get(roster.get("roster_id"))
        if slot in state.budget_by_slot:
            budget_label = f"budget ${state.budget_by_slot[slot]}"
        else:
            # Label every fallback, so a half-entered budget_<slot> map
            # never shows an unlabeled $200 next to real keeper budgets.
            budget_label = f"budget ${config.auction_budget} (default)"
        slot_label = f"slot {slot:>2}" if slot is not None else "slot  ?"
        name = _team_name(users_by_id.get(roster.get("owner_id")))
        lines.append(
            f"  {slot_label}  {name:<24} "
            f"{budget_label}  spent ${spent.get(slot, 0)}  "
            f"{_keeper_label(roster)}"
        )
    return lines


def build_summary(
    config: LeagueConfig,
    data: dict,
    *,
    degraded: set[str] | None = None,
    failures: dict[str, str] | None = None,
    tz: tzinfo | None = None,
) -> str:
    """Render the league summary from a full endpoint snapshot.

    ``degraded`` names endpoints served from the disk cache (live fetch
    failed); ``failures`` maps endpoints with no data at all to errors.
    Both are labeled so cached bytes never read as a live feed. ``tz``
    overrides the start-time zone (default: the machine's local zone).
    """
    league = data.get("league", {})
    state = parse_draft(data.get("draft", {}))
    status = state.status + (" [PAUSED]" if state.paused else "")
    lines = [
        (
            f"League: {league.get('name', '(unknown)')} - "
            f"season {league.get('season', '?')}, "
            f"{league.get('total_rosters', config.teams)} teams, "
            f"${config.auction_budget} auction"
        ),
        (
            f"Draft {state.draft_id}: {status} ({state.draft_type}), "
            f"{_start_time_label(state, tz)}"
        ),
    ]
    if degraded:
        lines.append(
            "WARNING - served from disk cache, NOT live (live fetch "
            "failed): " + ", ".join(sorted(degraded))
        )
    if failures:
        lines.append(
            "WARNING - unavailable (live fetch failed, no cache): "
            + ", ".join(sorted(failures))
        )
    if not state.budget_by_slot:
        lines.append(
            "Budgets: budgets not yet entered by the commissioner - "
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

    Exit codes: 0 all endpoints snapshotted; 1 the summary printed but at
    least one endpoint had neither a live response nor a cache; 2 nothing
    ran at all (missing, malformed, or incomplete config).
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

    if out is None and hasattr(sys.stdout, "reconfigure"):
        # Team names are user data (curly quotes, emoji); never let a
        # Windows console encoding crash the summary on draft night.
        sys.stdout.reconfigure(errors="replace")

    err = out if out is not None else sys.stderr
    config_path = Path(args.config)
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"error: config file not found: {config_path}", file=err)
        return 2
    except tomllib.TOMLDecodeError as error:
        print(f"error: config file is not valid TOML: {config_path}: {error}", file=err)
        return 2
    except KeyError as error:
        print(
            f"error: config file {config_path} is missing required key {error}",
            file=err,
        )
        return 2
    except ValueError as error:
        # A wrong-typed or out-of-range [valuation] knob is incomplete
        # config, not a crash: load_config raises a ValueError that already
        # names the key, so print it and exit 2 like any other bad config.
        print(f"error: {error}", file=err)
        return 2
    # cache_dir resolution lives in load_config (anchored to the config
    # file's folder); the CLI only forwards an explicit override.
    cache_dir = Path(args.cache_dir) if args.cache_dir is not None else None
    client = SleeperClient(config, cache_dir=cache_dir, http_get=http_get, clock=clock)
    try:
        result = client.snapshot_all()
    except SleeperUnavailableError as error:
        # snapshot_all degrades per endpoint, so this is a safety net for
        # unexpected total failures: a message, never a raw traceback.
        print(f"error: {error}", file=err)
        return 2
    print(
        build_summary(
            config, result.data, degraded=result.degraded, failures=result.failures
        ),
        file=out if out is not None else sys.stdout,
    )
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
