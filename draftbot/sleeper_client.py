"""Read-only Sleeper API client with an on-disk JSON cache.

The transport is a plain callable ``http_get(url) -> bytes`` so unit tests
inject fakes; the default transport uses ``requests`` with a timeout.
"""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from typing import Callable

from draftbot.config import LeagueConfig

API_BASE = "https://api.sleeper.app/v1"

# Undocumented but verified (2026-08-23): note the .com host, not .app.
PROJECTIONS_BASE = "https://api.sleeper.com/projections/nfl"
PROJECTION_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

REQUEST_TIMEOUT_SECONDS = 15


class SleeperUnavailableError(RuntimeError):
    """A live endpoint failed and no cached copy exists to fall back on."""


def default_http_get(url: str) -> bytes:
    """Fetch a URL with requests; raises on HTTP errors."""
    # Imported here so unit tests (which always inject a fake transport)
    # never need the requests package on the hot path.
    import requests  # pylint: disable=import-outside-toplevel

    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.content


class SleeperClient:
    """Fetches league data and snapshots every payload to the disk cache."""

    def __init__(
        self,
        config: LeagueConfig,
        cache_dir: str | Path | None = None,
        http_get: Callable[[str], bytes] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.cache_dir = Path(cache_dir if cache_dir is not None else config.cache_dir)
        self._http_get = http_get if http_get is not None else default_http_get
        self._clock = clock
        # Monotonic per-request counter, seeded from the clock so restarts
        # of the process never reuse a recent value.
        self._cache_bust_counter = itertools.count(int(self._clock() * 1000))

    def get_league(self) -> dict:
        """The league object."""
        url = f"{API_BASE}/league/{self.config.league_id}"
        return self._fetch_json("league", url)

    def get_draft(self) -> dict:
        """The draft object (carries live-auction state in ``metadata``)."""
        url = f"{API_BASE}/draft/{self.config.draft_id}"
        return self._fetch_json("draft", url)

    def get_picks(self):
        """The draft's picks feed (completed auction purchases)."""
        url = f"{API_BASE}/draft/{self.config.draft_id}/picks"
        return self._fetch_json("picks", url)

    def get_rosters(self):
        """The league's rosters."""
        url = f"{API_BASE}/league/{self.config.league_id}/rosters"
        return self._fetch_json("rosters", url)

    def get_users(self):
        """The league's users (team names live in user metadata)."""
        url = f"{API_BASE}/league/{self.config.league_id}/users"
        return self._fetch_json("users", url)

    def get_projections(self, season: int | None = None):
        """Season projections (``pts_half_ppr`` + ``adp_half_ppr``)."""
        year = season if season is not None else self.config.season
        positions = "&".join(f"position[]={pos}" for pos in PROJECTION_POSITIONS)
        url = f"{PROJECTIONS_BASE}/{year}?season_type=regular&{positions}"
        return self._fetch_json(f"projections_{year}", url)

    def snapshot_all(self) -> dict:
        """Fetch every endpoint once and snapshot each payload to disk.

        Run at startup so the draft-night dashboard can degrade to cached
        data if any live endpoint breaks later.
        """
        return {
            "league": self.get_league(),
            "draft": self.get_draft(),
            "picks": self.get_picks(),
            "rosters": self.get_rosters(),
            "users": self.get_users(),
            "players": self.get_players(),
            "projections": self.get_projections(),
        }

    def get_players(self) -> dict:
        """The full NFL player map (~14 MB), refetched at most once a day."""
        if self._player_map_is_fresh():
            return json.loads(
                (self.cache_dir / "players.json").read_text(encoding="utf-8")
            )
        url = f"{API_BASE}/players/nfl"
        payload, fetched_live = self._fetch_with_fallback("players", url)
        if fetched_live:
            self._write_meta("players", {"fetched_at": self._clock()})
        return payload

    def _player_map_is_fresh(self) -> bool:
        cache_file = self.cache_dir / "players.json"
        meta_file = self.cache_dir / "players.meta.json"
        if not (cache_file.exists() and meta_file.exists()):
            return False
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        age = self._clock() - meta.get("fetched_at", float("-inf"))
        return 0 <= age < self.config.player_map_max_age_seconds

    def _cache_bust(self, url: str) -> str:
        """Append an always-changing query param so Sleeper's CDN cannot
        serve stale bytes (draft is cached s-maxage=30, picks 15)."""
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}_cb={next(self._cache_bust_counter)}"

    def _fetch_json(self, cache_name: str, url: str):
        """Fetch ``url`` and snapshot it to the cache; on a live-endpoint
        failure, serve the cached copy instead of crashing."""
        payload, _ = self._fetch_with_fallback(cache_name, url)
        return payload

    def _fetch_with_fallback(self, cache_name: str, url: str):
        """Like ``_fetch_json`` but also reports whether the fetch was live
        (as opposed to served from the disk cache after a failure)."""
        try:
            payload = json.loads(self._http_get(self._cache_bust(url)))
        except Exception as error:  # pylint: disable=broad-exception-caught
            # Any transport failure (connection, HTTP status, bad JSON) must
            # degrade to the cache, not crash mid-draft.
            return self._read_cache(cache_name, error), False
        self._write_cache(cache_name, payload)
        return payload, True

    def _write_cache(self, cache_name: str, payload) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / f"{cache_name}.json"
        cache_file.write_text(json.dumps(payload), encoding="utf-8")

    def _write_meta(self, cache_name: str, meta: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        meta_file = self.cache_dir / f"{cache_name}.meta.json"
        meta_file.write_text(json.dumps(meta), encoding="utf-8")

    def _read_cache(self, cache_name: str, cause: Exception):
        cache_file = self.cache_dir / f"{cache_name}.json"
        if not cache_file.exists():
            raise SleeperUnavailableError(
                f"live fetch of '{cache_name}' failed and no cached copy "
                f"exists at {cache_file}"
            ) from cause
        return json.loads(cache_file.read_text(encoding="utf-8"))
