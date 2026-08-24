"""The dashboard's web surface and CLI.

``create_app`` wraps one :class:`~draftbot.dashboard.state.DashboardPoller`
in a two-route FastAPI app: ``GET /state`` returns the poller's current
snapshot verbatim (no-store, so the page's ~1s fetch timer never sees a
cached body), and ``GET /`` serves the single self-contained static page.
The poll loop runs in a daemon thread owned by ``main``; tests step the
poller directly instead.

Run ``python -m draftbot.dashboard --replay tests/fixtures/draft_2025.json
--accelerate 4`` for the zero-input replay demo, or omit ``--replay`` (and
pass ``--sheet``) to poll the live draft.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import tomllib
from pathlib import Path
from typing import TextIO

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from draftbot.config import LeagueConfig, load_config
from draftbot.dashboard.sheets import read_sheet_csv, replay_sheet
from draftbot.dashboard.state import DashboardPoller
from draftbot.models import parse_draft
from draftbot.sleeper_client import SleeperClient, SleeperUnavailableError
from draftbot.sources import LivePollSource, ReplaySource
from draftbot.tracker import (
    DraftTracker,
    default_expected_settings,
    keepers_by_slot_from_rosters,
)
from draftbot.valuation import value_map

STATIC_PAGE = Path(__file__).resolve().parent / "static" / "index.html"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_PORT = 8724


def create_app(poller: DashboardPoller) -> FastAPI:
    """The two-route ASGI app over one poller."""
    app = FastAPI(
        title="draftbot dashboard", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/state")
    def state() -> JSONResponse:
        """The current snapshot, exactly as the poller built it."""
        return JSONResponse(poller.snapshot, headers={"Cache-Control": "no-store"})

    @app.get("/")
    def index() -> HTMLResponse:
        """The static page (read per request; it is one small file)."""
        return HTMLResponse(STATIC_PAGE.read_text(encoding="utf-8"))

    return app


def run_poll_loop(
    poller: DashboardPoller, interval: float, stop: threading.Event
) -> None:
    """Step the poller until ``stop`` is set, one cycle per ``interval``
    seconds. ``step()`` absorbs every exception (source outages AND
    processing errors) into a labeled snapshot, so nothing can kill the
    loop mid-draft."""
    while not stop.is_set():
        poller.step()
        stop.wait(interval)


def _serve(
    app: FastAPI, poller: DashboardPoller, *, host: str, port: int, interval: float
) -> None:
    """The real server: the poll loop in a daemon thread, uvicorn in the
    foreground (Ctrl-C stops both)."""
    stop = threading.Event()
    thread = threading.Thread(
        target=run_poll_loop, args=(poller, interval, stop), daemon=True
    )
    thread.start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        stop.set()
        thread.join(timeout=5)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m draftbot.dashboard", description=__doc__
    )
    parser.add_argument(
        "--replay",
        default=None,
        help="replay fixture JSON ({'draft': ..., 'picks': ...}); "
        "omit to poll the live draft",
    )
    parser.add_argument(
        "--sheet",
        default=None,
        help="value-sheet CSV from `python -m draftbot.valuesheet` "
        "(required live; a replay without one derives a demo sheet "
        "from its own hammer prices)",
    )
    parser.add_argument("--config", default=str(REPO_ROOT / "league_config.toml"))
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="seconds per poll cycle (default 1.0)",
    )
    parser.add_argument(
        "--accelerate",
        type=float,
        default=1.0,
        help="divide the poll interval by this factor "
        "(replay demo: 4 reveals four sales per second)",
    )
    parser.add_argument(
        "--my-slot",
        type=int,
        default=None,
        help="my draft slot (default: resolved from the config's roster id)",
    )
    parser.add_argument(
        "--allow-no-keepers",
        action="store_true",
        help="serve live mode even though this keeper league's rosters "
        "returned zero keepers (only if the league truly kept no one)",
    )
    return parser.parse_args(argv)


def _load_config_or_none(path: str, err: TextIO) -> LeagueConfig | None:
    try:
        return load_config(path)
    except FileNotFoundError:
        print(f"error: config file not found: {path}", file=err)
    except tomllib.TOMLDecodeError as error:
        print(f"error: config file is not valid TOML: {path}: {error}", file=err)
    except KeyError as error:
        print(f"error: config file {path} is missing required key {error}", file=err)
    return None


def _read_sheet_or_none(path: str, err: TextIO):
    try:
        return read_sheet_csv(path)
    except (OSError, ValueError, KeyError) as error:
        print(f"error: cannot read sheet CSV {path}: {error}", file=err)
        return None


def _replay_runtime(args: argparse.Namespace, err: TextIO):
    """(source, rows, empty keepers) for replay mode, or None after
    printing why. The 2025 fixture carries zero keepers, so the empty
    keeper mapping is the truth, not a shortcut."""
    try:
        data = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        source = ReplaySource(data["draft"], data["picks"])
    except (OSError, ValueError, KeyError) as error:
        print(f"error: cannot load replay fixture {args.replay}: {error}", file=err)
        return None
    if args.sheet is None:
        return source, replay_sheet(data["picks"]), {}
    rows = _read_sheet_or_none(args.sheet, err)
    return None if rows is None else (source, rows, {})


def _build_runtime(
    args: argparse.Namespace,
    config: LeagueConfig,
    err: TextIO,
    http_get=None,
):
    """(source, rows, keepers_by_slot) for the chosen mode, or None after
    printing why. Live mode touches the network exactly once here (draft
    + rosters, for the keeper lists the picks feed never carries)."""
    if args.replay is not None:
        return _replay_runtime(args, err)
    if args.sheet is None:
        print(
            "error: live mode needs --sheet (generate the CSV with "
            "`python -m draftbot.valuesheet`)",
            file=err,
        )
        return None
    rows = _read_sheet_or_none(args.sheet, err)
    if rows is None:
        return None
    client = SleeperClient(config, cache_dir=args.cache_dir, http_get=http_get)
    try:
        draft = parse_draft(client.get_draft())
        keepers = keepers_by_slot_from_rosters(draft, client.get_rosters())
    except SleeperUnavailableError as error:
        print(f"error: {error}", file=err)
        return None
    if _keepers_missing(args, config, keepers):
        print(
            f"error: this is a keeper league (max_keepers "
            f"{config.max_keepers}) but Sleeper's rosters returned zero "
            "keepers; pricing the board keeper-free would overstate open "
            "slots and offer kept players for sale. Enter keepers on "
            "Sleeper, or pass --allow-no-keepers if the league truly "
            "kept no one.",
            file=err,
        )
        return None
    return LivePollSource(client), rows, keepers


def _keepers_missing(args: argparse.Namespace, config: LeagueConfig, keepers) -> bool:
    """True when a keeper league's rosters yielded no keepers at all and
    the operator did not explicitly wave it through. Silently pricing a
    keeper board keeper-free is the quiet version of a wrong number, and
    no banner would fire (the keeper_budgets warning keys off the same
    empty mapping) — so this refuses at startup instead."""
    return (
        config.max_keepers > 0
        and not any(keepers.values())
        and not args.allow_no_keepers
    )


def main(
    argv: list[str] | None = None,
    *,
    server=None,
    out: TextIO | None = None,
    http_get=None,
):
    """Build the runtime for the chosen mode and serve the dashboard.

    ``server``, ``out``, and ``http_get`` are injectable for tests (the
    default server is uvicorn plus the poll-loop thread; the default
    transport is the real one — the trackdemo pattern). Exit codes: 0
    served and shut down cleanly; 2 nothing ran (bad config, fixture, or
    sheet path, an out-of-range --my-slot, or live mode without a sheet /
    with a dead unavailable API / with a keeper league whose rosters
    carry no keepers).
    """
    args = _parse_args(argv)
    if out is None and hasattr(sys.stdout, "reconfigure"):
        # Player names are user data; never let a Windows console
        # encoding crash the server banner.
        sys.stdout.reconfigure(errors="replace")
    stream = out if out is not None else sys.stdout
    err = out if out is not None else sys.stderr

    config = _load_config_or_none(args.config, err)
    if config is None:
        return 2
    if args.my_slot is not None and not 1 <= args.my_slot <= config.teams:
        # Fail fast: a slot outside the league would otherwise surface
        # only as a per-poll error banner once the draft is underway.
        print(
            f"error: --my-slot {args.my_slot} is outside this league's "
            f"draft slots (1-{config.teams})",
            file=err,
        )
        return 2
    runtime = _build_runtime(args, config, err, http_get=http_get)
    if runtime is None:
        return 2
    poller = _wire_poller(runtime, config, my_slot=args.my_slot, note=_mode_note(args))
    app = create_app(poller)
    interval = args.interval / max(args.accelerate, 1e-9)
    print(
        f"dashboard: serving in {'replay' if args.replay else 'live'} mode "
        f"at {args.host}:{args.port} (poll interval {interval:g}s); "
        "Ctrl-C stops",
        file=stream,
    )
    run = _serve if server is None else server
    run(app, poller, host=args.host, port=args.port, interval=interval)
    return 0


def _mode_note(args: argparse.Namespace) -> str | None:
    """The standing on-page caveat for the current mode: the replay demo
    with no --sheet prices every lot at its own hammer, so profit reads
    $0 and every verdict is PASS by construction — a viewer who skipped
    the README should learn that from the page, not wonder at it."""
    if args.replay is not None and args.sheet is None:
        return (
            "replay demo sheet (derived from this replay's own hammer "
            "prices): profit reads $0 and every verdict is PASS by "
            "construction"
        )
    return None


def _wire_poller(
    runtime, config: LeagueConfig, *, my_slot: int | None, note: str | None = None
):
    """Tracker + poller over one runtime, wired consistently: the same
    keeper mapping feeds both (the tracker's keeper counts and the
    engine's keeper exclusions must never disagree), and the same sheet
    provides positions, off-model flagging, and pricing."""
    source, rows, keepers = runtime
    tracker = DraftTracker(
        config,
        keepers_by_slot=keepers,
        player_positions={row.player_id: row.position for row in rows},
        expected_settings=default_expected_settings(config),
        value_sheet=value_map(rows),
    )
    return DashboardPoller(
        source,
        tracker,
        rows,
        config,
        keepers_by_slot=keepers,
        my_slot=my_slot,
        note=note,
    )
