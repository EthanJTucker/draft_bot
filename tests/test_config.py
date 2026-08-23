"""League config loads from the TOML file — data, not code."""

from pathlib import Path

import pytest

from draftbot.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def _config_copy_in(tmp_path: Path, replace: tuple[str, str] | None = None) -> Path:
    """Copy the checked-in TOML into ``tmp_path``, optionally edited."""
    text = (REPO_ROOT / "league_config.toml").read_text(encoding="utf-8")
    if replace is not None:
        assert replace[0] in text
        text = text.replace(*replace)
    config_file = tmp_path / "league_config.toml"
    config_file.write_text(text, encoding="utf-8")
    return config_file


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


def test_config_is_deeply_frozen():
    """The frozen config must not be mutable through its mapping fields."""
    cfg = load_config(REPO_ROOT / "league_config.toml")
    with pytest.raises(TypeError):
        cfg.roster_slots["QB"] = 5
    with pytest.raises(TypeError):
        cfg.historical_draft_ids["2022"] = "0"


def test_cache_dir_anchors_to_the_config_file_folder(tmp_path):
    """One resolution rule: cache_dir resolves against the config file's
    folder in load_config, never against the process CWD."""
    cfg = load_config(_config_copy_in(tmp_path))
    assert Path(cfg.cache_dir) == tmp_path / "data" / "cache"


def test_request_timeout_loads_from_config(tmp_path):
    """The HTTP timeout is data in the TOML, tunable without a code change."""
    checked_in = load_config(REPO_ROOT / "league_config.toml")
    assert checked_in.request_timeout_seconds == 15

    tuned = load_config(
        _config_copy_in(
            tmp_path,
            replace=("request_timeout_seconds = 15", "request_timeout_seconds = 3"),
        )
    )
    assert tuned.request_timeout_seconds == 3
