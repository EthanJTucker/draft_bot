"""League config loads from the TOML file — data, not code."""

from pathlib import Path

from draftbot.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_load_config_reads_league_facts():
    """The checked-in league_config.toml carries the 2026 league contract."""
    cfg = load_config(REPO_ROOT / "league_config.toml")
    assert cfg.league_id == "1389692302259138560"
    assert cfg.draft_id == "1389692302259138561"
    assert cfg.season == 2026
    assert cfg.teams == 12
    assert cfg.auction_budget == 200
    # Keeper constants: cost = prior price + $2, $5 floor, max 3, 2-year cap.
    assert cfg.keeper_cost_increment == 2
    assert cfg.keeper_cost_floor == 5
    assert cfg.max_keepers == 3
    assert cfg.max_consecutive_keep_years == 2
    # 15 drafted slots; IR is extra and not drafted.
    assert cfg.drafted_slots == 15
    assert cfg.roster_slots["IR"] == 2
    # Historical drafts keyed by season string.
    assert cfg.historical_draft_ids["2025"] == "1257407146123857920"
    assert cfg.my_user_id == "868914670222921728"
    assert cfg.my_roster_id == 7
