"""``--overrides``: the CLI seam for the operator's own sheet.

The file is runtime input. Every fixture writes its own CSV into
``tmp_path``, nothing is committed, and no path here is absolute.

The startup behaviour mirrors ``--budget``, which exists because a lever
that quietly fails to apply is worse than no lever: parse errors stop
startup before the network round trip, what DID load is echoed so the
operator can see it, and the one non-fatal case (ids the value sheet does
not carry) is counted out loud rather than swallowed.
"""

from __future__ import annotations

import io
import json

from draftbot.dashboard.app import main

from .conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"
CONFIG = REPO_ROOT / "league_config.toml"


class CapturingServer:
    """Stands in for uvicorn: records the wiring instead of binding a port."""

    # pylint: disable=too-few-public-methods  # a fake server: __call__ is
    # its entire interface, matching the injectable server seam.

    def __init__(self):
        self.poller = None

    def __call__(self, app, poller, *, host, port, interval):
        self.poller = poller


def a_real_player_id():
    """One player id the 2025 replay fixture actually carries, so the
    override joins to a real lot instead of a made-up one."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    picks = sorted(data["picks"], key=lambda pick: pick["pick_no"])
    return picks[0]["player_id"]


def run(tmp_path, csv_text, extra=()):
    """One ``main`` run in replay mode with an override CSV. Returns
    (exit code, combined output, captured server)."""
    path = tmp_path / "overrides.csv"
    path.write_text(csv_text, encoding="utf-8")
    out, server = io.StringIO(), CapturingServer()
    code = main(
        [
            "--replay",
            str(FIXTURE),
            "--config",
            str(CONFIG),
            "--overrides",
            str(path),
            *extra,
        ],
        server=server,
        out=out,
    )
    return code, out.getvalue(), server


def test_a_valid_sheet_loads_and_reaches_the_poller(tmp_path):
    """End to end: the CSV the operator maintains becomes the mapping the
    engine prices against, and startup says what it read."""
    player_id = a_real_player_id()

    code, output, server = run(tmp_path, f"player_id,delta,target\n{player_id},7,1\n")

    assert code == 0
    assert "override sheet: 1 row" in output
    assert server.poller is not None
    state = server.poller.step()
    assert state["ok"] is True


def test_a_malformed_row_stops_startup_and_names_the_line(tmp_path):
    """Loud, and before the network. A tweak that silently failed to
    parse leaves exactly the wrong number this lever exists to remove."""
    code, output, _ = run(tmp_path, "player_id,delta\nrb2,+ten\n")

    assert code == 2
    assert "override CSV line 2" in output
    assert "not a whole number" in output


def test_a_name_that_disagrees_with_the_sheet_stops_startup(tmp_path):
    """The cross-check that an id-only join cannot make: the right name
    beside the wrong id would tweak somebody else's price, confidently."""
    player_id = a_real_player_id()

    code, output, _ = run(
        tmp_path, f"player_id,name,delta\n{player_id},Nobody At All,7\n"
    )

    assert code == 2
    assert "Nobody At All" in output
    assert "value sheet has that id as" in output


def test_ids_the_sheet_does_not_carry_are_counted_not_fatal(tmp_path):
    """An off-sheet rookie is a legitimate target. Refusing to start over
    one would cost the operator the whole lever on draft night -- but a
    swallowed count would hide a file keyed to the wrong id space
    entirely, so the number is printed."""
    code, output, server = run(
        tmp_path, "player_id,delta\nnot_in_2025,4\nalso_not_in_2025,2\n"
    )

    assert code == 0
    assert "2 of them are not on the value sheet" in output
    assert server.poller is not None


def test_no_overrides_flag_runs_exactly_as_before_and_says_nothing():
    """The default path. With no sheet supplied the startup banner gains
    no line at all and the poller carries an empty book -- the CLI half
    of "with no override file, nothing moves"."""
    out, server = io.StringIO(), CapturingServer()

    code = main(
        ["--replay", str(FIXTURE), "--config", str(CONFIG)], server=server, out=out
    )

    assert code == 0
    assert "override sheet" not in out.getvalue()
    state = server.poller.step()
    assert state["nomination"]["analysis"] is None or (
        state["nomination"]["analysis"]["override"] is None
    )


def test_a_missing_override_file_stops_startup(tmp_path):
    """A typo'd path must not start a dashboard whose overrides silently
    are not there -- indistinguishable, on screen, from a sheet of
    opinions the model happened to agree with."""
    out, server = io.StringIO(), CapturingServer()

    code = main(
        [
            "--replay",
            str(FIXTURE),
            "--config",
            str(CONFIG),
            "--overrides",
            str(tmp_path / "nope.csv"),
        ],
        server=server,
        out=out,
    )

    assert code == 2
    assert "cannot read override CSV" in out.getvalue()
    assert server.poller is None
