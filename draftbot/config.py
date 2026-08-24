"""League configuration loaded from a TOML file.

Everything league-specific (IDs, budgets, roster shape, keeper constants,
the user's identifiers) lives in ``league_config.toml`` so that next season
is a config edit, not a code change.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

DEFAULT_CONFIG_PATH = Path("league_config.toml")

# Fallbacks for optional TOML keys — defined once here; the checked-in
# league_config.toml states its own (possibly different) explicit values.
DEFAULT_CACHE_DIR = "data/cache"
DEFAULT_PLAYER_MAP_MAX_AGE_SECONDS = 86400
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0

# Valuation-model tunables (the decided values from GAMEPLAN.md; the
# [valuation] section of league_config.toml states them explicitly, and
# draftbot/valuation.py aliases these as its module defaults so config
# and code cannot drift apart).
DEFAULT_BAND_RATIO = 1.6
DEFAULT_MIN_BAND_SAMPLES = 6
DEFAULT_GAMMA = 0.8
DEFAULT_CURVE_CAP = True


@dataclass(frozen=True)
class LeagueConfig:
    """Static facts about the league, its auction, and its keeper rules."""

    # pylint: disable=too-many-instance-attributes  # flat config record: one
    # field per league constant is the point; nesting adds only indirection.

    league_name: str
    season: int
    league_id: str
    draft_id: str
    teams: int
    auction_budget: int
    roster_slots: Mapping[str, int]
    keeper_cost_increment: int
    keeper_cost_floor: int
    max_keepers: int
    max_consecutive_keep_years: int
    my_username: str
    my_user_id: str
    my_roster_id: int
    historical_draft_ids: Mapping[str, str] = field(default_factory=dict)
    cache_dir: str = DEFAULT_CACHE_DIR
    player_map_max_age_seconds: int = DEFAULT_PLAYER_MAP_MAX_AGE_SECONDS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    # Valuation tunables ([valuation] in the TOML; defaults keep pricing
    # identical for configs written before the section existed).
    band_ratio: float = DEFAULT_BAND_RATIO
    min_band_samples: int = DEFAULT_MIN_BAND_SAMPLES
    gamma: float = DEFAULT_GAMMA
    curve_cap: bool = DEFAULT_CURVE_CAP

    def __post_init__(self):
        # frozen=True alone leaves the mapping fields mutable; wrap them in
        # read-only proxies so shared config cannot be silently mutated.
        object.__setattr__(
            self, "roster_slots", MappingProxyType(dict(self.roster_slots))
        )
        object.__setattr__(
            self,
            "historical_draft_ids",
            MappingProxyType(dict(self.historical_draft_ids)),
        )

    @property
    def drafted_slots(self) -> int:
        """Number of drafted roster slots (IR spots are extra, not drafted)."""
        return sum(count for slot, count in self.roster_slots.items() if slot != "IR")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> LeagueConfig:
    """Read ``league_config.toml`` (or another TOML file) into a LeagueConfig.

    ``cache_dir`` is resolved here, against the config file's folder — the
    single resolution rule for every consumer (client and CLI alike).
    """
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    league = raw["league"]
    keeper = raw["keeper"]
    me = raw["me"]
    cache = raw.get("cache", {})
    http = raw.get("http", {})
    valuation = raw.get("valuation", {})
    config_dir = Path(path).resolve().parent
    return LeagueConfig(
        league_name=league["name"],
        season=league["season"],
        league_id=league["league_id"],
        draft_id=league["draft_id"],
        teams=league["teams"],
        auction_budget=raw["auction"]["budget"],
        roster_slots=dict(raw["roster"]),
        keeper_cost_increment=keeper["cost_increment"],
        keeper_cost_floor=keeper["cost_floor"],
        max_keepers=keeper["max_keepers"],
        max_consecutive_keep_years=keeper["max_consecutive_years"],
        my_username=me["username"],
        my_user_id=me["user_id"],
        my_roster_id=me["roster_id"],
        historical_draft_ids=dict(league.get("historical_drafts", {})),
        cache_dir=str(config_dir / cache.get("dir", DEFAULT_CACHE_DIR)),
        player_map_max_age_seconds=cache.get(
            "player_map_max_age_seconds", DEFAULT_PLAYER_MAP_MAX_AGE_SECONDS
        ),
        request_timeout_seconds=http.get(
            "request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
        band_ratio=valuation.get("band_ratio", DEFAULT_BAND_RATIO),
        min_band_samples=valuation.get("min_band_samples", DEFAULT_MIN_BAND_SAMPLES),
        gamma=valuation.get("gamma", DEFAULT_GAMMA),
        curve_cap=valuation.get("curve_cap", DEFAULT_CURVE_CAP),
    )
