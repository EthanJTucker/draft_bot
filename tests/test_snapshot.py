"""The snapshot CLI's league summary."""

from __future__ import annotations

import io
from datetime import timedelta, timezone
from pathlib import Path

from draftbot.snapshot import build_summary, main

from .conftest import REPO_ROOT, FakeTransport


def _snapshot_data(draft_settings=None, keepers_for_roster_1=None, picks=None):
    """A compact but realistic snapshot: two known teams of the twelve."""
    settings = {"teams": 12, "budget": 200}
    settings.update(draft_settings or {})
    return {
        "league": {
            "name": "12th Week Campers",
            "season": "2026",
            "total_rosters": 12,
        },
        "draft": {
            "draft_id": "1389692302259138561",
            "status": "pre_draft",
            "type": "auction",
            "settings": settings,
            "metadata": {},
            "slot_to_roster_id": {"1": 1, "5": 7},
        },
        "picks": picks or [],
        "rosters": [
            {"roster_id": 1, "owner_id": "U1", "keepers": keepers_for_roster_1},
            {"roster_id": 7, "owner_id": "U7", "keepers": None},
        ],
        "users": [
            {
                "user_id": "U1",
                "display_name": "alice",
                "metadata": {"team_name": "Team Alice"},
            },
            {"user_id": "U7", "display_name": "firstrider55", "metadata": {}},
        ],
        "players": {"4034": {"full_name": "Tyreek Hill"}},
        "projections": [{"player_id": "4034", "stats": {"pts_half_ppr": 250.0}}],
    }


def test_summary_before_keepers_entered_falls_back_to_default_budget(config):
    """No budget_<slot> yet: every team shows the $200 default, labeled so."""
    out = build_summary(config, _snapshot_data())

    assert "12th Week Campers" in out
    assert "pre_draft" in out
    assert "auction" in out
    assert "budgets not yet entered" in out
    assert "Team Alice" in out
    assert "firstrider55" in out
    # Honest keeper reporting: nothing in the API yet, so say so.
    assert "keepers: n/a" in out

    team_line = next(line for line in out.splitlines() if "firstrider55" in line)
    assert "$200" in team_line


def test_summary_uses_entered_keeper_budgets_per_slot(config):
    """budget_<slot> attributes to the team in that draft slot, not any other."""
    data = _snapshot_data(
        draft_settings={"budget_1": 143, "budget_5": 96},
        keepers_for_roster_1=["4034", "1466"],
    )
    out = build_summary(config, data)

    assert "budgets not yet entered" not in out
    alice_line = next(line for line in out.splitlines() if "Team Alice" in line)
    my_line = next(line for line in out.splitlines() if "firstrider55" in line)
    # Roster 1 sits in slot 1 ($143); roster 7 sits in slot 5 ($96).
    assert "$143" in alice_line
    assert "$96" in my_line
    # Keeper counts come from the rosters' keepers lists when present.
    assert "keepers: 2" in alice_line
    assert "keepers: n/a" in my_line


def test_summary_reports_auction_spend_by_slot(config):
    """Completed purchases show up as spend against the buying slot."""
    picks = [
        {
            "round": 1,
            "pick_no": 1,
            "draft_slot": 5,
            "player_id": "4034",
            "picked_by": "",
            "is_keeper": None,
            "metadata": {"amount": "43"},
        }
    ]
    data = _snapshot_data(picks=picks)
    data["draft"]["status"] = "drafting"
    out = build_summary(config, data)

    my_line = next(line for line in out.splitlines() if "firstrider55" in line)
    assert "spent $43" in my_line
    assert "drafting" in out


def _transport_for(config, data):
    """A FakeTransport serving a full endpoint snapshot for this league."""
    return FakeTransport(
        {
            "/picks": data["picks"],
            "/rosters": data["rosters"],
            "/users": data["users"],
            "/players/nfl": data["players"],
            "/projections/nfl/2026": data["projections"],
            f"/draft/{config.draft_id}": data["draft"],
            f"/league/{config.league_id}": data["league"],
        }
    )


def test_main_snapshots_and_prints_summary(config, tmp_path):
    """The CLI fetches everything, caches to disk, and prints the summary."""
    data = _snapshot_data()
    transport = _transport_for(config, data)
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

    assert exit_code == 0
    printed = out.getvalue()
    assert "12th Week Campers" in printed
    assert "pre_draft" in printed
    assert Path(tmp_path / "league.json").exists()
    assert Path(tmp_path / "players.json").exists()
    assert Path(tmp_path / "projections_2026.json").exists()


def test_teams_print_in_draft_slot_order_not_roster_id_order(config):
    """Slot assignment is independent of roster id in a real draft; the
    "by draft slot" listing must actually sort by slot."""
    data = _snapshot_data(draft_settings={"budget_1": 143, "budget_5": 96})
    # Cross the mapping: roster 7 sits in slot 1, roster 1 in slot 5.
    data["draft"]["slot_to_roster_id"] = {"1": 7, "5": 1}
    out = build_summary(config, data)

    team_lines = [line for line in out.splitlines() if "budget $" in line]
    assert "firstrider55" in team_lines[0]  # roster 7 is in slot 1 now
    assert "Team Alice" in team_lines[1]


def test_partially_entered_budgets_label_defaulted_teams(config):
    """With budget_<slot> half-entered, a team lacking its key must show
    the $200 fallback explicitly labeled, not an unlabeled $200."""
    data = _snapshot_data(draft_settings={"budget_1": 143})
    out = build_summary(config, data)

    alice_line = next(line for line in out.splitlines() if "Team Alice" in line)
    my_line = next(line for line in out.splitlines() if "firstrider55" in line)
    assert "$143" in alice_line
    assert "(default)" not in alice_line
    assert "$200 (default)" in my_line


def test_start_time_renders_in_the_given_timezone(config):
    """A Sunday-evening Denver draft must not print as Monday UTC; the
    zone is injectable so the test is machine-independent."""
    data = _snapshot_data()
    # 2026-08-31 00:15 UTC == 2026-08-30 18:15 in UTC-6 (Denver in DST).
    data["draft"]["start_time"] = 1_788_135_300_000
    out = build_summary(config, data, tz=timezone(timedelta(hours=-6)))

    assert "starts 2026-08-30 18:15" in out
    assert "2026-08-31" not in out


def test_summary_labels_endpoints_served_from_cache(config):
    """Cached-not-live data is labeled: identical bytes must not read as a
    live feed on draft night."""
    out = build_summary(config, _snapshot_data(), degraded={"draft", "picks"})
    assert "served from disk cache" in out
    assert "draft, picks" in out


def test_main_with_missing_config_prints_error_not_traceback(tmp_path):
    """A bad --config path exits with a message, not a raw traceback."""
    out = io.StringIO()

    exit_code = main(
        ["--config", str(tmp_path / "missing.toml")],
        http_get=FakeTransport({}),
        clock=lambda: 1_000.0,
        out=out,
    )

    assert exit_code == 2
    assert "config" in out.getvalue().lower()


def test_main_with_malformed_toml_prints_error_not_traceback(tmp_path):
    """A config file that is not valid TOML exits 2 with a message, not a
    tomllib traceback."""
    bad = tmp_path / "league_config.toml"
    bad.write_text("[league\nname = broken", encoding="utf-8")
    out = io.StringIO()

    exit_code = main(
        ["--config", str(bad)],
        http_get=FakeTransport({}),
        clock=lambda: 1_000.0,
        out=out,
    )

    assert exit_code == 2
    assert "config" in out.getvalue().lower()


def test_main_with_missing_required_key_prints_error_not_traceback(tmp_path):
    """Valid TOML missing a required section exits 2 with a message naming
    the problem, not a KeyError traceback."""
    partial = tmp_path / "league_config.toml"
    partial.write_text('[league]\nname = "X"\n', encoding="utf-8")
    out = io.StringIO()

    exit_code = main(
        ["--config", str(partial)],
        http_get=FakeTransport({}),
        clock=lambda: 1_000.0,
        out=out,
    )

    assert exit_code == 2
    assert "config" in out.getvalue().lower()


def test_main_first_run_partial_failure_still_prints_a_summary(config, tmp_path):
    """Fresh machine, projections host down: the documented endpoints still
    snapshot and print, the failure is labeled, and the exit is nonzero."""
    transport = _transport_for(config, _snapshot_data())
    del transport.payloads["/projections/nfl/2026"]
    out = io.StringIO()

    exit_code = main(
        ["--cache-dir", str(tmp_path)],
        http_get=transport,
        clock=lambda: 1_000.0,
        out=out,
    )

    assert exit_code == 1
    printed = out.getvalue()
    assert "12th Week Campers" in printed
    assert "unavailable" in printed
    assert "projections" in printed
    assert (tmp_path / "league.json").exists()


def test_main_total_first_run_failure_exits_cleanly(tmp_path):
    """No cache and every endpoint down: labeled failures and a nonzero
    exit, never an uncaught exception."""
    transport = FakeTransport({})
    transport.failing = True
    out = io.StringIO()

    exit_code = main(
        ["--cache-dir", str(tmp_path)],
        http_get=transport,
        clock=lambda: 1_000.0,
        out=out,
    )

    assert exit_code == 1
    assert "unavailable" in out.getvalue()


def test_cli_default_cache_dir_follows_the_config_file(config, tmp_path):
    """Without --cache-dir the cache lands next to the given config file,
    never in the process CWD."""
    config_text = (REPO_ROOT / "league_config.toml").read_text(encoding="utf-8")
    config_file = tmp_path / "league_config.toml"
    config_file.write_text(config_text, encoding="utf-8")
    transport = _transport_for(config, _snapshot_data())

    exit_code = main(
        ["--config", str(config_file)],
        http_get=transport,
        clock=lambda: 1_000.0,
        out=io.StringIO(),
    )

    assert exit_code == 0
    assert (tmp_path / "data" / "cache" / "league.json").exists()
