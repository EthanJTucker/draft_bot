"""The FastAPI surface and the CLI: /state, the static page, the wiring.

The TestClient drives the real ASGI app; the poll loop is stepped by hand
(deterministic — no thread, no timer). CLI tests inject a fake server the
same way trackdemo injects its transport.
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from draftbot.dashboard.app import create_app, main
from draftbot.valuesheet import write_csv

from .conftest import REPO_ROOT
from .helpers_dashboard import make_poller, make_tick
from .helpers_engine import sheet_row

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"


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
    ):
        assert element_id in page
    assert "http://" not in page.replace("http://localhost", "")
    assert "https://" not in page  # fully self-contained: no external assets
    assert "/state" in page


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
