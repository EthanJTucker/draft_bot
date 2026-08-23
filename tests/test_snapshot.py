"""The snapshot CLI's league summary."""

from __future__ import annotations

import io
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


def test_main_snapshots_and_prints_summary(tmp_path):
    """The CLI fetches everything, caches to disk, and prints the summary."""
    data = _snapshot_data()
    transport = FakeTransport(
        {
            "/picks": data["picks"],
            "/rosters": data["rosters"],
            "/users": data["users"],
            "/players/nfl": data["players"],
            "projections/nfl/2026": data["projections"],
            "/draft/": data["draft"],
            "/league/": data["league"],
        }
    )
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
