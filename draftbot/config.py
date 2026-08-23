"""League configuration loaded from a TOML file.

Everything league-specific (IDs, budgets, roster shape, keeper constants,
the user's identifiers) lives in ``league_config.toml`` so that next season
is a config edit, not a code change.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("league_config.toml")


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
    roster_slots: dict[str, int]
    keeper_cost_increment: int
    keeper_cost_floor: int
    max_keepers: int
    max_consecutive_keep_years: int
    my_username: str
    my_user_id: str
    my_roster_id: int
    historical_draft_ids: dict[str, str] = field(default_factory=dict)
    cache_dir: str = "data/cache"
    player_map_max_age_seconds: int = 86400

    @property
    def drafted_slots(self) -> int:
        """Number of drafted roster slots (IR spots are extra, not drafted)."""
        return sum(count for slot, count in self.roster_slots.items() if slot != "IR")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> LeagueConfig:
    """Read ``league_config.toml`` (or another TOML file) into a LeagueConfig."""
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    league = raw["league"]
    keeper = raw["keeper"]
    me = raw["me"]
    cache = raw.get("cache", {})
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
        cache_dir=cache.get("dir", "data/cache"),
        player_map_max_age_seconds=cache.get("player_map_max_age_seconds", 86400),
    )
