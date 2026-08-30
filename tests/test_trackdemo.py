"""The replay demo CLI: team state per pick plus the final-budgets check."""

# pylint: disable=duplicate-code  # the injectable-CLI invocation pattern
# (argv + fake transport + fixed clock + StringIO) legitimately mirrors
# test_snapshot's; the two CLIs share that seam by design.

from __future__ import annotations

import io
import json

import pytest

from draftbot.trackdemo import main

from .conftest import (
    REPO_ROOT,
    VALUATION_TUNABLE_KEYS,
    FakeTransport,
    config_with_a_wrong_typed_tunable,
)

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draft_2025.json"
DRAFT_ID_2025 = "1257407146123857920"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _transport(data: dict) -> FakeTransport:
    return FakeTransport(
        {f"/draft/{DRAFT_ID_2025}": data["draft"], "/picks": data["picks"]}
    )


def _run(transport: FakeTransport, tmp_path) -> tuple[int, str]:
    out = io.StringIO()
    exit_code = main(
        [
            "--config",
            str(REPO_ROOT / "league_config.toml"),
            "--cache-dir",
            str(tmp_path),
        ],
        http_get=transport,
        clock=lambda: 1_000.0,
        out=out,
    )
    return exit_code, out.getvalue()


def test_replay_demo_prints_per_pick_state_and_verified_final_budgets(tmp_path):
    """The 2025 replay runs end-to-end: each sale prints the buying team's
    updated state, and the final table checks every team's spend against
    its own slot budget from the draft object."""
    exit_code, printed = _run(_transport(_fixture()), tmp_path)

    assert exit_code == 0
    # Pick 1 is real: Amon-Ra St. Brown to slot 5 for $51.
    assert "St. Brown" in printed
    assert "$51" in printed
    assert "180 sales" in printed
    assert "Final budgets" in printed
    assert printed.count("[OK]") == 12
    assert "[OVERSPENT]" not in printed
    # Slot 2's recorded $8 leftover shows up as data, not as a failure.
    assert "left $8" in printed


def test_replay_demo_flags_an_overspending_team(tmp_path):
    """If the accounting ever puts a team over its own budget the demo must
    fail loudly, not print a reassuring table."""
    data = _fixture()
    victim = next(p for p in data["picks"] if p["draft_slot"] == 1)
    victim["metadata"]["amount"] = "999"

    exit_code, printed = _run(_transport(data), tmp_path)

    assert exit_code == 1
    assert "[OVERSPENT]" in printed


def test_replay_demo_reads_timer_expectations_from_config(tmp_path):
    """The expected timers are config data, not demo literals: a config
    expecting a 60-second pick timer must banner the fixture's real
    10-second timer as a mismatch (and the checked-in 10s config must not).
    Kills the hardcoded-timer mutant: a demo that states 10s in code shows
    a clean board no matter what the config says."""
    text = (REPO_ROOT / "league_config.toml").read_text(encoding="utf-8")
    config_file = tmp_path / "league_config.toml"
    config_file.write_text(
        text.replace("pick_timer = 10", "pick_timer = 60"), encoding="utf-8"
    )
    out = io.StringIO()
    exit_code = main(
        [
            "--config",
            str(config_file),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        http_get=_transport(_fixture()),
        clock=lambda: 1_000.0,
        out=out,
    )
    printed = out.getvalue()

    assert exit_code == 0
    assert "settings differ: pick_timer" in printed
    assert "expected 60" in printed

    # The checked-in config's 10-second expectations stay warning-free.
    _, clean = _run(_transport(_fixture()), tmp_path)
    assert "settings differ" not in clean


def test_replay_demo_with_missing_config_exits_cleanly(tmp_path):
    """A bad --config path exits 2 with a message, not a traceback."""
    out = io.StringIO()
    exit_code = main(
        ["--config", str(tmp_path / "missing.toml")],
        http_get=FakeTransport({}),
        clock=lambda: 1_000.0,
        out=out,
    )
    assert exit_code == 2
    assert "config" in out.getvalue().lower()


@pytest.mark.parametrize("key", VALUATION_TUNABLE_KEYS)
def test_wrong_typed_valuation_tunable_exits_2_by_name(tmp_path, key):
    """A wrong-typed ``[valuation]`` knob is a named one-line config
    error and exit 2 out of the track-demo CLI, not a ValueError traceback.

    Parametrized over EVERY key in the section rather than one of them:
    the typo lands wherever it lands, and a test that only ever quotes
    ``starter_pct`` would read as a closed contract while four other
    keys still tracebacked and a fifth loaded a truthy string clean.
    """
    out = io.StringIO()
    exit_code = main(
        ["--config", str(config_with_a_wrong_typed_tunable(tmp_path, key))],
        http_get=FakeTransport({}),
        clock=lambda: 1_000.0,
        out=out,
    )
    printed = out.getvalue()
    assert exit_code == 2
    assert key in printed
    assert "Traceback" not in printed
