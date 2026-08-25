"""The FastAPI surface and the CLI: /state, the static page, the wiring.

The TestClient drives the real ASGI app; the poll loop is stepped by hand
(deterministic — no thread, no timer) except for the one test that runs
``run_poll_loop`` in a real thread, because that loop is the production
write path. CLI tests inject a fake server the same way trackdemo injects
its transport.
"""

from __future__ import annotations

import io
import threading
import time

from fastapi.testclient import TestClient

from draftbot.dashboard.app import create_app, main, run_poll_loop
from draftbot.valuesheet import write_csv

from .conftest import REPO_ROOT, FakeTransport
from .helpers_dashboard import make_poller, make_tick
from .helpers_engine import sheet_row

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"
CONFIG = REPO_ROOT / "league_config.toml"

# The real config's draft id (tests use the checked-in league_config.toml).
DRAFT_ID = "1389692302259138561"


class CapturingServer:
    """Stands in for uvicorn: records the wiring instead of binding a port."""

    # pylint: disable=too-few-public-methods  # a fake server: __call__ is
    # its entire interface, matching the injectable server seam.

    def __init__(self):
        self.app = None
        self.poller = None
        self.interval = None
        self.host = None
        self.port = None

    def __call__(self, app, poller, *, host, port, interval):
        self.app, self.poller = app, poller
        self.host, self.port, self.interval = host, port, interval


def _rows():
    return [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]


def test_state_endpoint_serves_the_snapshot_within_one_poll_cycle(config):
    """End to end through HTTP: a nomination new in the source is in the
    very next /state response after one poll cycle, uncached."""
    entries = [make_tick(), make_tick(nominee="B", offer=5)]
    poller = make_poller(config, entries, _rows())
    client = TestClient(create_app(poller))

    poller.step()
    before = client.get("/state")
    assert before.status_code == 200
    assert before.headers["cache-control"] == "no-store"
    assert before.json()["nomination"]["status"] == "none"

    poller.step()
    after = client.get("/state").json()
    assert after["nomination"]["player_id"] == "B"
    assert after["nomination"]["status"] == "live"
    assert after["poll_count"] == 2


def test_state_endpoint_serves_even_before_the_first_poll(config):
    """A browser that loads before the loop's first cycle gets an honest
    not-ready snapshot, not a 500."""
    poller = make_poller(config, [make_tick()], _rows())
    client = TestClient(create_app(poller))

    payload = client.get("/state").json()
    assert payload["ok"] is False
    assert payload["poll_count"] == 0


def test_index_page_carries_a_slot_for_every_required_element(config):
    """The static page is one self-contained file (no CDN, no npm) whose
    skeleton binds every element the issue names: nominee, high bid,
    worth, room price, max bid, verdict, profit, last-of-tier, the
    positional table, team budgets, and my pinned roster."""
    poller = make_poller(config, [make_tick()], _rows())
    client = TestClient(create_app(poller))

    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    for element_id in (
        'id="nominee"',
        'id="high-bid"',
        'id="worth"',
        'id="room-price"',
        'id="max-bid"',
        'id="verdict"',
        'id="profit"',
        'id="tier-warning"',
        'id="players"',
        'id="teams"',
        'id="my-team"',
        'id="banners"',
        'id="foot-note"',
    ):
        assert element_id in page
    assert "http://" not in page.replace("http://localhost", "")
    assert "https://" not in page  # fully self-contained: no external assets
    assert "/state" in page
    # The honest-failure surfaces: a crashed render says so in red, and
    # the default-budget asterisk carries its legend.
    assert "PAGE RENDER FAILED" in page
    assert "budget not entered on Sleeper yet" in page


def test_run_poll_loop_steps_the_poller_until_stopped(config):
    """The poll thread's one production line, exercised for real: a
    thread running run_poll_loop at a tiny interval advances poll_count
    past 2 and stops cleanly when the event is set. Deleting the step()
    call from the loop leaves poll_count at 0 and fails here."""
    poller = make_poller(config, [make_tick()], _rows())
    stop = threading.Event()
    thread = threading.Thread(target=run_poll_loop, args=(poller, 0.005, stop))

    thread.start()
    deadline = time.time() + 10.0
    while poller.snapshot["poll_count"] < 2 and time.time() < deadline:
        time.sleep(0.005)
    stop.set()
    thread.join(timeout=10.0)

    assert not thread.is_alive()
    assert poller.snapshot["poll_count"] >= 2
    assert poller.snapshot["ok"] is True


def test_cli_rejects_a_my_slot_outside_the_league():
    """--my-slot 99 in a 12-team league would otherwise surface only as a
    per-poll error once the draft is underway; it is a wiring mistake, so
    it fails fast at startup with exit 2 and never starts the server."""
    server = CapturingServer()
    for bad_slot in ("0", "99"):
        out = io.StringIO()
        code = main(
            [
                "--replay",
                str(FIXTURE),
                "--my-slot",
                bad_slot,
                "--config",
                str(REPO_ROOT / "league_config.toml"),
            ],
            server=server,
            out=out,
        )
        assert code == 2
        assert "--my-slot" in out.getvalue()
        assert "1-12" in out.getvalue()
    assert server.app is None


def test_replay_cli_wires_the_full_stack_with_zero_manual_input():
    """``--replay <fixture> --accelerate 4`` builds the whole runtime from
    the fixture alone: replay source, tracker with the config's expected
    settings, the replay-derived sheet, and a 4x-fast poll interval. Two
    hand-driven steps then show the first sale exactly as /state will."""
    server = CapturingServer()
    out = io.StringIO()

    exit_code = main(
        [
            "--replay",
            str(FIXTURE),
            "--accelerate",
            "4",
            "--config",
            str(REPO_ROOT / "league_config.toml"),
        ],
        server=server,
        out=out,
    )

    assert exit_code == 0
    assert server.interval == 0.25
    assert server.host == "127.0.0.1"

    server.poller.step()  # empty opening tick
    state = server.poller.step()  # first sale: St. Brown to slot 5, $51
    assert state["sales"][0]["name"] == "Amon-Ra St. Brown"
    assert state["sales"][0]["amount"] == 51
    assert state["nomination"]["player_id"] == state["sales"][0]["player_id"]
    assert state["nomination"]["status"] == "sold_between_lots"
    assert state["nomination"]["analysis"] is not None
    assert state["nomination"]["verdict"] is not None
    # Roster 7 drafted from slot 8 in 2025: my_slot resolves from config.
    assert state["me"]["slot"] == 8
    assert not state["settings_warnings"]
    # The demo's all-PASS/$0 caveat rides the snapshot onto the page.
    assert "PASS by construction" in state["note"]

    client = TestClient(server.app)
    assert client.get("/state").json()["poll_count"] == 2


def test_replay_cli_accepts_a_real_sheet_csv(tmp_path):
    """--sheet overrides the replay-derived demo sheet with a real CSV."""
    sheet = tmp_path / "sheet.csv"
    write_csv([sheet_row(1, "7547", "WR", 44.0, name="Amon-Ra St. Brown")], sheet)
    server = CapturingServer()

    exit_code = main(
        ["--replay", str(FIXTURE), "--sheet", str(sheet)],
        server=server,
        out=io.StringIO(),
    )

    assert exit_code == 0
    server.poller.step()
    state = server.poller.step()
    # Sheet worth (44), not the hammer price (51): the CSV won.
    assert state["nomination"]["analysis"]["worth"] == 44.0
    # Every other 2025 sale is off this one-row sheet: flagged, not hidden.
    assert state["players"] == []
    # A real sheet is not the demo: no all-PASS caveat on the page.
    assert state["note"] is None


def _live_transport(rosters):
    """Canned live-mode payloads: the draft object, the rosters (keeper
    carrier), and an empty picks feed."""
    draft = {
        "draft_id": DRAFT_ID,
        "status": "pre_draft",
        "type": "auction",
        "settings": {},
        "metadata": {},
        "slot_to_roster_id": {str(slot): slot for slot in range(1, 13)},
    }
    return FakeTransport(
        {f"/draft/{DRAFT_ID}": draft, "/rosters": rosters, "/picks": []}
    )


def _live_args(tmp_path, sheet, *extra):
    return [
        "--sheet",
        str(sheet),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--config",
        str(REPO_ROOT / "league_config.toml"),
        *extra,
    ]


def _keeper_sheet(tmp_path):
    sheet = tmp_path / "sheet.csv"
    write_csv(
        [
            sheet_row(1, "KP", "RB", 30.0, name="Kept Back"),
            sheet_row(2, "B", "WR", 20.0),
        ],
        sheet,
    )
    return sheet


def test_live_mode_refuses_a_keeper_league_with_zero_keepers(tmp_path):
    """The config says keeper league (max_keepers 3); if Sleeper's rosters
    return zero keepers on draft night the dashboard would silently price
    a keeper board keeper-free — open slots overstated, kept players in
    the buyable pool — and no banner fires because the keeper_budgets
    warning keys off the same empty mapping. Startup refuses instead."""
    rosters = [{"roster_id": slot, "keepers": []} for slot in range(1, 13)]
    server = CapturingServer()
    out = io.StringIO()

    code = main(
        _live_args(tmp_path, _keeper_sheet(tmp_path)),
        server=server,
        out=out,
        http_get=_live_transport(rosters),
    )

    assert code == 2
    message = out.getvalue()
    assert "keeper" in message
    assert "--allow-no-keepers" in message
    assert server.app is None


def test_live_mode_allow_no_keepers_waves_through_an_empty_keeper_map(tmp_path):
    """The escape hatch: an operator who has confirmed the league truly
    kept no one can serve anyway, explicitly."""
    rosters = [{"roster_id": slot, "keepers": []} for slot in range(1, 13)]
    server = CapturingServer()

    code = main(
        _live_args(tmp_path, _keeper_sheet(tmp_path), "--allow-no-keepers"),
        server=server,
        out=io.StringIO(),
        http_get=_live_transport(rosters),
    )

    assert code == 0
    assert server.app is not None
    assert server.poller.step()["ok"] is True


def test_live_mode_wires_roster_keepers_into_the_board(tmp_path):
    """Rosters WITH keepers start cleanly and the keeper mapping flows to
    both tracker and engine: the kept player is off the buyable table and
    pinned on my roster as a keeper."""
    rosters = [
        {"roster_id": slot, "keepers": ["KP"] if slot == 7 else []}
        for slot in range(1, 13)
    ]
    server = CapturingServer()

    code = main(
        _live_args(tmp_path, _keeper_sheet(tmp_path)),
        server=server,
        out=io.StringIO(),
        http_get=_live_transport(rosters),
    )

    assert code == 0
    state = server.poller.step()
    assert state["ok"] is True
    assert [player["player_id"] for player in state["players"]] == ["B"]
    me = state["me"]
    assert me["slot"] == 7  # roster 7 resolved through slot_to_roster_id
    assert me["open_slots"] == 14  # 15 drafted slots minus the keeper
    keeper_entries = [entry for entry in me["roster"] if entry["keeper"]]
    assert [entry["player_id"] for entry in keeper_entries] == ["KP"]
    assert keeper_entries[0]["price"] is None  # kept, not bought


def test_live_mode_budget_flag_puts_my_real_money_on_the_board(tmp_path):
    """End to end through the CLI on the board this league will actually
    show: Sleeper carries no budget_<slot> key for anyone (settings is
    empty), and my slot 7 holds three keepers. Left alone the page reads
    the $200 league default — $189 of max bid. `--budget 7=96` keys the
    real number off the league sheet and the page reads $85.

    The flag is repeatable, so slot 3 is set in the same run and must
    land too, while every un-keyed slot stays honestly marked."""
    rosters = [
        {"roster_id": slot, "keepers": ["KP", "K2", "K3"] if slot == 7 else []}
        for slot in range(1, 13)
    ]
    server = CapturingServer()

    code = main(
        _live_args(
            tmp_path, _keeper_sheet(tmp_path), "--budget", "7=96", "--budget", "3=143"
        ),
        server=server,
        out=io.StringIO(),
        http_get=_live_transport(rosters),
    )

    assert code == 0
    state = server.poller.step()
    me = state["me"]
    assert me["slot"] == 7
    assert me["open_slots"] == 12  # 15 drafted slots minus three keepers
    assert me["remaining"] == 96  # the real budget, not the $200 default
    assert me["max_bid"] == 85  # 96 - 11 held; the default would say 189
    assert me["budget_is_default"] is False
    slots = {team["slot"]: team for team in state["teams"]}
    assert slots[3]["remaining"] == 143
    assert slots[3]["budget_is_default"] is False
    assert slots[5]["remaining"] == 200
    assert slots[5]["budget_is_default"] is True


def test_cli_rejects_malformed_budget_overrides():
    """Every rejection is a clear message and exit 2, never a traceback and
    never a silently-dropped flag — a budget the operator believes he set
    but which never landed is the same wrong number this flag exists to
    prevent. Checked before the network, so a typo fails instantly."""
    server = CapturingServer()
    for bad, expected in (
        ("7", "SLOT=AMOUNT"),  # no separator
        ("=96", "SLOT=AMOUNT"),  # no slot
        ("7=", "SLOT=AMOUNT"),  # no amount
        ("seven=96", "whole numbers"),
        ("7=ninety", "whole numbers"),
        ("7=96.5", "whole numbers"),
        ("99=96", "1-12"),  # slot outside the league
        ("0=96", "1-12"),
        ("7=-5", "$0 and $200"),  # negative money
        ("7=250", "$0 and $200"),  # above the league budget
    ):
        out = io.StringIO()
        code = main(
            ["--replay", str(FIXTURE), "--budget", bad, "--config", str(CONFIG)],
            server=server,
            out=out,
        )
        assert code == 2, f"--budget {bad} was accepted"
        assert "--budget" in out.getvalue()
        assert expected in out.getvalue(), f"--budget {bad}: {out.getvalue()}"
    assert server.app is None  # no bad run reached the server


def test_cli_rejects_the_same_slot_keyed_twice():
    """Two amounts for one slot is a typo with a silent loser; guessing
    which one the operator meant is exactly the confident-wrong-number
    failure this work exists to stop."""
    server = CapturingServer()
    out = io.StringIO()

    code = main(
        [
            "--replay",
            str(FIXTURE),
            "--budget",
            "7=96",
            "--budget",
            "7=100",
            "--config",
            str(CONFIG),
        ],
        server=server,
        out=out,
    )

    assert code == 2
    assert "slot 7" in out.getvalue()
    assert server.app is None


def test_cli_exits_2_on_missing_config_or_fixture(tmp_path):
    """Bad inputs exit 2 with a message, not a traceback, and never start
    the server."""
    server = CapturingServer()

    out = io.StringIO()
    code = main(
        ["--replay", str(FIXTURE), "--config", str(tmp_path / "no.toml")],
        server=server,
        out=out,
    )
    assert code == 2
    assert "config" in out.getvalue().lower()

    out = io.StringIO()
    code = main(["--replay", str(tmp_path / "no.json")], server=server, out=out)
    assert code == 2
    assert "replay" in out.getvalue().lower()

    assert server.app is None  # neither bad run reached the server


def test_cli_live_mode_requires_a_sheet():
    """Live mode with no --sheet is a config error (the sheet is the
    model; polling live without one would render a $1 board)."""
    server = CapturingServer()
    out = io.StringIO()

    code = main([], server=server, out=out)

    assert code == 2
    assert "--sheet" in out.getvalue()
    assert server.app is None
