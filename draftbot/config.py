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

# The league's 10-second nomination and bid timers (GAMEPLAN.md): stated in
# the [auction] section of league_config.toml; these are the fallbacks for
# configs written before the keys existed.
DEFAULT_NOMINATION_TIMER_SECONDS = 10
DEFAULT_PICK_TIMER_SECONDS = 10

# Valuation-model tunables (the decided values from GAMEPLAN.md; the
# [valuation] section of league_config.toml states them explicitly, and
# draftbot/valuation.py aliases these as its module defaults so config
# and code cannot drift apart).
DEFAULT_BAND_RATIO = 1.6
DEFAULT_MIN_BAND_SAMPLES = 6
DEFAULT_GAMMA = 0.8
DEFAULT_CURVE_CAP = True
# Share of the discretionary pool priced off the STARTER replacement
# baseline; the rest is priced off the deeper bench baseline derived from
# the league's own draft composition (issue #27).
DEFAULT_STARTER_PCT = 0.5
# 0 means "derive the bench baseline's depth target from [roster] and the
# team count" (drafted slots minus the K and DEF slots, times teams).
DEFAULT_BENCH_SKILL_SLOTS = 0


def _tunable_fraction(section: Mapping, key: str, default: float) -> float:
    """A [valuation] knob that must be a real number in 0..1 inclusive.

    A typo in the TOML is a config error, not a pricing mystery: it names
    the key and the offending value here, at load time, rather than
    surfacing as a TypeError deep inside the worth arithmetic.
    """
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"[valuation] {key} must be a number 0..1, got {value!r}")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"[valuation] {key} must be between 0 and 1, got {value!r}")
    return float(value)


def _tunable_count(section: Mapping, key: str, default: int, minimum: int = 0) -> int:
    """A [valuation] knob that must be a whole number at or above
    ``minimum`` (0 where zero is itself a meaningful setting)."""
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"[valuation] {key} must be an integer >= {minimum}, got {value!r}"
        )
    if value < minimum:
        raise ValueError(
            f"[valuation] {key} must not be below {minimum}, got {value!r}"
        )
    return value


def _tunable_ratio(section: Mapping, key: str, default: float) -> float:
    """A [valuation] knob that must be a real number at or above 1.

    Below 1 the ADP band spans ``adp/ratio .. adp*ratio`` backwards and so
    is empty for every player: every price falls through to the fitted
    curve, a whole pricing rule switched off with nothing printed.
    """
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"[valuation] {key} must be a number >= 1, got {value!r}")
    if float(value) < 1.0:
        raise ValueError(f"[valuation] {key} must not be below 1, got {value!r}")
    return float(value)


def _tunable_flag(section: Mapping, key: str, default: bool) -> bool:
    """A [valuation] knob that must be a real TOML boolean.

    The one knob where a wrong type used to produce no error at all: every
    non-empty string is truthy, so ``curve_cap = "no"`` loaded as ON and
    quietly priced a different sheet.
    """
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"[valuation] {key} must be true or false, got {value!r}")
    return value


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
    # Expected draft timers in seconds for the settings-differ check
    # ([auction] in the TOML, same key names as Sleeper's settings; defaults
    # keep configs written before the keys existed on the league's facts).
    nomination_timer: int = DEFAULT_NOMINATION_TIMER_SECONDS
    pick_timer: int = DEFAULT_PICK_TIMER_SECONDS
    # Valuation tunables ([valuation] in the TOML; defaults keep pricing
    # identical for configs written before the section existed).
    band_ratio: float = DEFAULT_BAND_RATIO
    min_band_samples: int = DEFAULT_MIN_BAND_SAMPLES
    gamma: float = DEFAULT_GAMMA
    curve_cap: bool = DEFAULT_CURVE_CAP
    # The two-baseline VBD blend: the share of the discretionary pool priced
    # off the STARTER baseline, and the bench baseline's depth target in
    # skill lots (0 = derive it from [roster] and the team count).
    starter_pct: float = DEFAULT_STARTER_PCT
    bench_skill_slots: int = DEFAULT_BENCH_SKILL_SLOTS

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
    auction = raw["auction"]
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
        auction_budget=auction["budget"],
        nomination_timer=auction.get(
            "nomination_timer", DEFAULT_NOMINATION_TIMER_SECONDS
        ),
        pick_timer=auction.get("pick_timer", DEFAULT_PICK_TIMER_SECONDS),
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
        band_ratio=_tunable_ratio(valuation, "band_ratio", DEFAULT_BAND_RATIO),
        min_band_samples=_tunable_count(
            valuation, "min_band_samples", DEFAULT_MIN_BAND_SAMPLES, minimum=1
        ),
        gamma=_tunable_fraction(valuation, "gamma", DEFAULT_GAMMA),
        curve_cap=_tunable_flag(valuation, "curve_cap", DEFAULT_CURVE_CAP),
        starter_pct=_tunable_fraction(valuation, "starter_pct", DEFAULT_STARTER_PCT),
        bench_skill_slots=_tunable_count(
            valuation, "bench_skill_slots", DEFAULT_BENCH_SKILL_SLOTS
        ),
    )
