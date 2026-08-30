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


def test_valuation_tunables_load_from_config(tmp_path):
    """The [valuation] knobs are data in the TOML; the checked-in file
    states exactly the decided model constants."""
    checked_in = load_config(REPO_ROOT / "league_config.toml")
    assert checked_in.band_ratio == 1.6
    assert checked_in.min_band_samples == 6
    assert checked_in.gamma == 0.8
    assert checked_in.curve_cap is True

    tuned = load_config(
        _config_copy_in(tmp_path, replace=("gamma = 0.8", "gamma = 0.5"))
    )
    assert tuned.gamma == 0.5


def test_missing_valuation_section_keeps_identical_defaults(tmp_path):
    """A TOML with no [valuation] section loads the same numbers the
    checked-in config states — the section is additive, so configs from
    before it existed keep pricing identically."""
    text = (REPO_ROOT / "league_config.toml").read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start = next(n for n, line in enumerate(lines) if line.startswith("[valuation]"))
    end = next(n for n in range(start + 1, len(lines)) if lines[n].startswith("["))
    config_file = tmp_path / "league_config.toml"
    config_file.write_text("".join(lines[:start] + lines[end:]), encoding="utf-8")

    stripped = load_config(config_file)
    checked_in = load_config(REPO_ROOT / "league_config.toml")
    assert stripped.band_ratio == checked_in.band_ratio
    assert stripped.min_band_samples == checked_in.min_band_samples
    assert stripped.gamma == checked_in.gamma
    assert stripped.curve_cap == checked_in.curve_cap
    assert stripped.starter_pct == checked_in.starter_pct
    assert stripped.bench_skill_slots == checked_in.bench_skill_slots


def test_bench_baseline_tunables_load_from_config(tmp_path):
    """The starter/bench blend weight and the bench baseline's depth target
    are data in the TOML. ``bench_skill_slots = 0`` means "derive the target
    from the roster shape"."""
    checked_in = load_config(REPO_ROOT / "league_config.toml")
    assert checked_in.starter_pct == 0.5
    assert checked_in.bench_skill_slots == 0

    tuned = load_config(
        _config_copy_in(tmp_path, replace=("starter_pct = 0.5", "starter_pct = 0.25"))
    )
    assert tuned.starter_pct == 0.25


@pytest.mark.parametrize(
    "edit",
    [
        ("starter_pct = 0.5", 'starter_pct = "half"'),
        ("starter_pct = 0.5", "starter_pct = 1.5"),
        ("starter_pct = 0.5", "starter_pct = -0.1"),
        # TOML bools are the typo a numeric knob attracts, and Python's
        # bool IS an int: without the explicit isinstance guard
        # `starter_pct = true` loads as 1.0, which puts every dollar back
        # on the starter baseline and silently disables the second one —
        # no error, no warning, a whole pricing model switched off.
        # `false` loads as 0.0, the bench-only extreme.
        ("starter_pct = 0.5", "starter_pct = true"),
        ("starter_pct = 0.5", "starter_pct = false"),
        ("bench_skill_slots = 0", 'bench_skill_slots = "156"'),
        ("bench_skill_slots = 0", "bench_skill_slots = -5"),
        ("bench_skill_slots = 0", "bench_skill_slots = true"),
    ],
)
def test_a_bad_bench_tunable_is_refused_by_name(tmp_path, edit):
    """A wrong-typed or out-of-range knob names itself in the error rather
    than tracebacking somewhere deep in the pricing math."""
    with pytest.raises(ValueError, match=edit[0].split(" =")[0]):
        load_config(_config_copy_in(tmp_path, replace=edit))


def test_expected_draft_timers_load_from_config(tmp_path):
    """The 10-second nomination and bid timers are league facts stated in
    the TOML, not literals buried in whichever module checks them."""
    checked_in = load_config(REPO_ROOT / "league_config.toml")
    assert checked_in.nomination_timer == 10
    assert checked_in.pick_timer == 10

    tuned = load_config(
        _config_copy_in(tmp_path, replace=("pick_timer = 10", "pick_timer = 60"))
    )
    assert tuned.pick_timer == 60
    assert tuned.nomination_timer == 10


def test_missing_timer_keys_keep_the_league_defaults(tmp_path):
    """A config written before the timer keys existed keeps the league's
    10-second expectations rather than crashing or expecting nothing."""
    text = (REPO_ROOT / "league_config.toml").read_text(encoding="utf-8")
    lines = [
        line
        for line in text.splitlines(keepends=True)
        if not line.startswith(("nomination_timer", "pick_timer"))
    ]
    config_file = tmp_path / "league_config.toml"
    config_file.write_text("".join(lines), encoding="utf-8")

    stripped = load_config(config_file)
    assert stripped.nomination_timer == 10
    assert stripped.pick_timer == 10


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
