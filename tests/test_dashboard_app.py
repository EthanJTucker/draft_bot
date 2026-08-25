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
from .helpers_dashboard import (
    MY_DRAFT_SLOT,
    PERMUTED_SLOTS,
    make_poller,
    make_tick,
)
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
    # The defaulted-budget marks. No test in this suite executes the
    # page's JavaScript, so these assert the SOURCE EXPRESSIONS rather
    # than bare key names: `var guessed = false;` and `var guessedBudget
    # = false;` both mute a mark while leaving every bare key elsewhere
    # in the file, and both survived the suite before these lines. The
    # bare-name version cannot catch them either, because
    # `me.budget_is_default` is a substring of `s.me.budget_is_default`
    # and of `t.budget_is_default`. The rendering itself (amber on the
    # my-team money, the caps line, the max-bid figure and its sub;
    # plain when the budgets are real) was checked in a browser.
    assert "BUDGET NOT ENTERED" in page
    assert "!!me.budget_is_default" in page
    assert "!!(s.me && s.me.budget_is_default)" in page
    assert "!!(s.defaulted_keeper_slots && s.defaulted_keeper_slots.length)" in page
    assert ".sub.guessed" in page
    assert "#my-team .money.guessed" in page
    # The id is load-bearing: a bare `.value.guessed` loses the cascade
    # to `#max-bid` and the 30px figure stays confident blue.
    assert "#max-bid.guessed" in page
    # The two class assignments that put the amber ON that figure and its
    # sub-line. Asserted as whole expressions, not by element id: dropping
    # the conditional back to a bare 'value num' survives every looser
    # form of this check. Brittle to reformatting on purpose — an edit
    # here has to be re-checked in a browser, because nothing runs it.
    assert "'value num' + (guessedBudget || guessedRoom ? ' guessed' : '')" in page
    assert (
        "'sub' + (analysis && (guessedBudget || guessedRoom) ? ' guessed' : '')" in page
    )
    assert "room default" in page  # the sub-line's room qualifier


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


# Live-mode fixtures use the permuted slot map (shared with the state
# tests), so a rule reading the wrong identifier cannot pass by accident.
def _live_transport(rosters, *, slots=None, settings=None):
    """Canned live-mode payloads: the draft object, the rosters (keeper
    carrier), and an empty picks feed."""
    draft = {
        "draft_id": DRAFT_ID,
        "status": "pre_draft",
        "type": "auction",
        "settings": settings or {},
        "metadata": {},
        "slot_to_roster_id": {
            str(slot): roster_id
            for slot, roster_id in (slots or PERMUTED_SLOTS).items()
        },
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
    pinned on my roster as a keeper.

    Rosters key their keepers by ROSTER ID and the board keys everything
    by DRAFT SLOT, and this fixture's map sends roster 7 to slot 4. A
    bridge that conflated the two would hang my keeper on slot 7, an
    opponent, and leave my own roster empty."""
    rosters = [
        {"roster_id": roster_id, "keepers": ["KP"] if roster_id == 7 else []}
        for roster_id in range(1, 13)
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
    assert me["slot"] == MY_DRAFT_SLOT  # roster 7 drafts from slot 4 here
    assert me["open_slots"] == 14  # 15 drafted slots minus the keeper
    keeper_entries = [entry for entry in me["roster"] if entry["keeper"]]
    assert [entry["player_id"] for entry in keeper_entries] == ["KP"]
    assert keeper_entries[0]["price"] is None  # kept, not bought


def _three_keeper_rosters():
    """My roster (id 7) keeps three; nobody else keeps anyone."""
    return [
        {
            "roster_id": roster_id,
            "keepers": ["KP", "K2", "K3"] if roster_id == 7 else [],
        }
        for roster_id in range(1, 13)
    ]


def test_live_mode_budget_flag_puts_my_real_money_on_the_board(tmp_path):
    """End to end through the CLI on the board this league will actually
    show: Sleeper carries no budget_<slot> key for anyone (settings is
    empty), and my three keepers sit on DRAFT SLOT 4. Left alone the page
    reads the $200 league default — $189 of max bid. `--budget 4=96` keys
    the real number off the league sheet and the page reads $85.

    The flag is repeatable, so slot 3 is set in the same run and must
    land too, while every un-keyed slot stays honestly marked."""
    server = CapturingServer()

    code = main(
        _live_args(
            tmp_path, _keeper_sheet(tmp_path), "--budget", "4=96", "--budget", "3=143"
        ),
        server=server,
        out=io.StringIO(),
        http_get=_live_transport(_three_keeper_rosters()),
    )

    assert code == 0
    state = server.poller.step()
    me = state["me"]
    assert me["slot"] == MY_DRAFT_SLOT
    assert me["open_slots"] == 12  # 15 drafted slots minus three keepers
    assert me["remaining"] == 96  # the real budget, not the $200 default
    assert me["max_bid"] == 85  # 96 - 11 held; the default would say 189
    assert me["budget_is_default"] is False
    slots = {team["slot"]: team for team in state["teams"]}
    assert slots[3]["remaining"] == 143
    assert slots[3]["budget_is_default"] is False
    assert slots[5]["remaining"] == 200
    assert slots[5]["budget_is_default"] is True


def test_startup_echoes_the_overrides_and_shouts_when_they_miss_my_slot(tmp_path):
    """The two startup lines that make a slot-keyed flag safe to use.

    `--budget` takes a DRAFT slot; the operator's roster id is 7 and, on
    this map, his draft slot is 4. Keying the roster id funds slot 7,
    which is somebody else, and nothing downstream can detect it — roster
    ids are 1-12 as well. So startup echoes every parsed pair, and shouts
    when an override was supplied while MY resolved slot still has real
    money from neither source, which is exactly that mistake's signature.

    Anti-cheat: the mis-keyed run must be shown to actually go wrong (my
    money stays defaulted, slot 7's does not), so the warning is pinned
    against a real failure and not an imaginary one. And the correct run
    must stay quiet, or an always-on warning would be ignored by draft
    night."""
    server = CapturingServer()
    out = io.StringIO()

    code = main(
        _live_args(tmp_path, _keeper_sheet(tmp_path), "--budget", "7=96"),
        server=server,
        out=out,
        http_get=_live_transport(_three_keeper_rosters()),
    )

    assert code == 0  # it still serves; this is a warning, not a refusal
    printed = out.getvalue()
    assert "budget overrides parsed (DRAFT slots): slot 7 = $96" in printed
    assert "MY draft slot (4)" in printed
    assert "--budget 4=AMOUNT" in printed
    assert "roster id (7)" in printed
    # The mistake the warning is about, measured: my own money is still a
    # guess and an opponent quietly took the $96.
    state = server.poller.step()
    assert state["me"]["slot"] == MY_DRAFT_SLOT
    assert state["me"]["budget_is_default"] is True
    assert state["nomination"]["verdict"] is None
    slots = {team["slot"]: team for team in state["teams"]}
    assert slots[7]["remaining"] == 96
    assert slots[7]["budget_is_default"] is False

    quiet = io.StringIO()
    code = main(
        _live_args(tmp_path, _keeper_sheet(tmp_path), "--budget", "4=96"),
        server=CapturingServer(),
        out=quiet,
        http_get=_live_transport(_three_keeper_rosters()),
    )

    assert code == 0
    assert "budget overrides parsed (DRAFT slots): slot 4 = $96" in quiet.getvalue()
    assert "MY draft slot" not in quiet.getvalue()


IDENTITY_SLOTS = {slot: slot for slot in range(1, 13)}


def test_startup_draws_no_conclusion_while_the_draft_order_is_a_placeholder(tmp_path):
    """The launch sequence draft night will actually use.

    The dashboard comes up BEFORE the commissioner assigns the draft
    order, so the draft sits in pre_draft carrying the placeholder map
    (slot N = roster N) that this league permutes the moment the order
    lands. Startup resolves my slot from that map exactly once, which
    means it is drawing conclusions from a number about to change. Both
    directions were wrong:

    * `--budget 7=96`, keyed to my ROSTER id, matches the placeholder, so
      the guard printed a clean bill and the operator learned nothing —
      and once the order landed, slot 7 was an opponent.
    * `--budget 4=96`, the CORRECT figure for my eventual draft slot,
      tripped the guard, which told him to pass `--budget 7=AMOUNT`
      instead: to move his real money onto that same opponent's row.

    A tool that instructs the wrong action is worse than one that says
    nothing, so on a placeholder map neither line is printed. Startup
    says the order is not assigned yet and stops; the live board carries
    the check from there.
    """
    for flag in ("7=96", "4=96"):
        out = io.StringIO()

        code = main(
            _live_args(tmp_path, _keeper_sheet(tmp_path), "--budget", flag),
            server=CapturingServer(),
            out=out,
            http_get=_live_transport(_three_keeper_rosters(), slots=IDENTITY_SLOTS),
        )

        assert code == 0
        printed = out.getvalue()
        assert f"budget overrides parsed (DRAFT slots): slot {flag[0]} = $96" in printed
        assert "order is not assigned yet" in printed
        # No verdict on the flag, in EITHER direction: no all-clear, and
        # above all no instruction to re-key it against the placeholder.
        assert "MY draft slot" not in printed
        assert "AMOUNT instead" not in printed


def test_an_explicit_my_slot_is_checked_even_before_the_order_lands(tmp_path):
    """ANTI-CHEAT on the placeholder caveat: it is about the MAP, not
    about the status.

    `--my-slot 4` is the operator stating his slot himself, so resolving
    it never touches the slot map and an undealt order cannot make it
    stale. The check must still run here and still catch `--budget 7=96`.
    A caveat read off the draft object alone — dropping the "and my slot
    did not come from that map" half — goes quiet on this board and fails
    here. The other half of the pair is pinned next door: the shouts test
    runs a pre_draft draft whose order HAS been dealt, so a caveat keyed
    on the status alone swallows the loud warning and fails there."""
    out = io.StringIO()

    code = main(
        _live_args(
            tmp_path,
            _keeper_sheet(tmp_path),
            "--my-slot",
            "4",
            "--budget",
            "7=96",
        ),
        server=CapturingServer(),
        out=out,
        http_get=_live_transport(_three_keeper_rosters(), slots=IDENTITY_SLOTS),
    )

    assert code == 0
    printed = out.getvalue()
    assert "order is not assigned yet" not in printed
    assert "MY draft slot (4)" in printed
    assert "--budget 4=AMOUNT" in printed


def test_a_mis_keyed_override_is_named_in_a_banner_once_sleeper_carries_keys(tmp_path):
    """The quiet case the precedence flip creates, and its remaining tell.

    Once the commissioner enters budget_<slot> for everyone, a mis-keyed
    `--budget 7=96` leaves MY slot 4 on its correct Sleeper key. My money
    is right, my verdict renders, and the startup warning above correctly
    stays silent — nothing about my own half is wrong. But slot 7's real
    budget was just discarded for a number typed against the wrong
    identifier, and that team's money feeds inflation and the pace pool.

    The standing settings banner is what names it, by slot and with both
    amounts. Without it the flip would trade one invisible failure for
    another."""
    server = CapturingServer()
    out = io.StringIO()
    entered = {f"budget_{slot}": 150 for slot in range(1, 13)}

    code = main(
        _live_args(tmp_path, _keeper_sheet(tmp_path), "--budget", "7=96"),
        server=server,
        out=out,
        http_get=_live_transport(_three_keeper_rosters(), settings=entered),
    )

    assert code == 0
    assert "MY draft slot" not in out.getvalue()  # my own money is fine
    state = server.poller.step()
    assert state["me"]["remaining"] == 150  # my real key, untouched
    assert state["me"]["budget_is_default"] is False
    (banner,) = [
        warning
        for warning in state["settings_warnings"]
        if warning["field"] == "budget_override"
    ]
    assert "slot 7 = $96" in banner["expected"]
    assert "slot 7 = $150" in banner["actual"]


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
