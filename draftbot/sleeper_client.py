"""Read-only Sleeper API client with an on-disk JSON cache.

The transport is a plain callable ``http_get(url) -> bytes`` so unit tests
inject fakes; the default transport uses ``requests`` with a timeout.
"""

from __future__ import annotations

import functools
import itertools
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from draftbot.config import DEFAULT_REQUEST_TIMEOUT_SECONDS, LeagueConfig

API_BASE = "https://api.sleeper.app/v1"

# Undocumented but verified (2026-08-23): note the .com host, not .app.
PROJECTIONS_BASE = "https://api.sleeper.com/projections/nfl"
PROJECTION_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


class SleeperUnavailableError(RuntimeError):
    """A live endpoint failed and no cached copy exists to fall back on."""


# Sentinel for "no usable cache file" (absent OR corrupt): a cached JSON
# payload can legitimately be any value, including null.
_MISSING = object()


@dataclass
class SnapshotResult:
    """A best-effort full snapshot.

    ``data`` holds every endpoint that produced a payload (live or cached);
    ``degraded`` names the endpoints served from the disk cache because the
    live fetch failed; ``failures`` maps endpoints with neither a live
    response nor a cache to their error messages.
    """

    data: dict = field(default_factory=dict)
    degraded: set[str] = field(default_factory=set)
    failures: dict[str, str] = field(default_factory=dict)


def default_http_get(
    url: str, timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
) -> bytes:
    """Fetch a URL with requests; raises on HTTP errors."""
    # Imported here so unit tests (which always inject a fake transport)
    # never need the requests package on the hot path.
    import requests  # pylint: disable=import-outside-toplevel

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


class SleeperClient:
    """Fetches league data and snapshots every payload to the disk cache."""

    # Class-wide counter so two clients constructed in the same millisecond
    # still get distinct cache-bust prefixes.
    _instance_ids = itertools.count()

    def __init__(
        self,
        config: LeagueConfig,
        cache_dir: str | Path | None = None,
        http_get: Callable[[str], bytes] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.cache_dir = Path(cache_dir if cache_dir is not None else config.cache_dir)
        if http_get is None:
            http_get = functools.partial(
                default_http_get, timeout=config.request_timeout_seconds
            )
        self._http_get = http_get
        self._clock = clock
        # True iff the most recent fetch attempted the live API, failed,
        # and served the disk cache instead. Poll-loop consumers read it
        # right after each get_* call to tell a dead feed from live data
        # (a by-design daily player-map cache hit is NOT degradation).
        self.last_fetch_degraded: bool = False
        # Bust tokens are unique per request (counter), per instance and
        # process (prefix), and across restarts (clock seed).
        self._cache_bust_prefix = (
            f"{int(self._clock() * 1000)}"
            f"-{os.getpid()}-{next(SleeperClient._instance_ids)}"
        )
        self._cache_bust_counter = itertools.count()

    def get_league(self) -> dict:
        """The league object."""
        url = f"{API_BASE}/league/{self.config.league_id}"
        return self._fetch_json("league", url)

    def get_draft(self, draft_id: str | None = None) -> dict:
        """The draft object (carries live-auction state in ``metadata``).

        Defaults to the live draft; pass a historical id (see
        ``config.historical_draft_ids``) for a prior season. Each draft
        caches under its own id, so a historical fetch can never overwrite
        the live draft's fallback cache.
        """
        chosen = draft_id if draft_id is not None else self.config.draft_id
        url = f"{API_BASE}/draft/{chosen}"
        return self._fetch_json(f"draft_{chosen}", url)

    def get_picks(self, draft_id: str | None = None):
        """The draft's picks feed (completed auction purchases).

        ``draft_id`` works exactly as in :meth:`get_draft`, with the same
        per-draft cache isolation.
        """
        chosen = draft_id if draft_id is not None else self.config.draft_id
        url = f"{API_BASE}/draft/{chosen}/picks"
        return self._fetch_json(f"picks_{chosen}", url)

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

    def snapshot_all(self) -> SnapshotResult:
        """Fetch every endpoint once and snapshot each payload to disk.

        Run at startup so the draft-night dashboard can degrade to cached
        data if any live endpoint breaks later. Degrades per endpoint: one
        broken endpoint (recorded in ``failures``) never aborts the rest,
        and endpoints served from cache are recorded in ``degraded``.
        """
        fetchers: tuple[tuple[str, Callable[[], object]], ...] = (
            ("league", self.get_league),
            ("draft", self.get_draft),
            ("picks", self.get_picks),
            ("rosters", self.get_rosters),
            ("users", self.get_users),
            ("players", self.get_players),
            ("projections", self.get_projections),
        )
        result = SnapshotResult()
        for name, fetch in fetchers:
            try:
                result.data[name] = fetch()
            except SleeperUnavailableError as error:
                result.failures[name] = str(error)
                continue
            if self.last_fetch_degraded:
                result.degraded.add(name)
        return result

    def get_players(self) -> dict:
        """The full NFL player map (~14 MB), refetched at most once a day."""
        if self._player_map_is_fresh():
            cached = self._load_cache_json(self.cache_dir / "players.json")
            if cached is not _MISSING:
                self.last_fetch_degraded = False  # a by-design daily hit
                return cached
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
        meta = self._load_cache_json(meta_file)
        if meta is _MISSING or not isinstance(meta, dict):
            return False
        age = self._clock() - meta.get("fetched_at", float("-inf"))
        return 0 <= age < self.config.player_map_max_age_seconds

    def _cache_bust(self, url: str) -> str:
        """Append an always-changing query param so Sleeper's CDN cannot
        serve stale bytes (draft is cached s-maxage=30, picks 15)."""
        separator = "&" if "?" in url else "?"
        token = f"{self._cache_bust_prefix}-{next(self._cache_bust_counter)}"
        return f"{url}{separator}_cb={token}"

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
            payload = self._read_cache(cache_name, error)
            self.last_fetch_degraded = True
            return payload, False
        self._write_cache(cache_name, payload)
        self.last_fetch_degraded = False
        return payload, True

    def _write_cache(self, cache_name: str, payload) -> None:
        self._write_json_atomic(self.cache_dir / f"{cache_name}.json", payload)

    def _write_meta(self, cache_name: str, meta: dict) -> None:
        self._write_json_atomic(self.cache_dir / f"{cache_name}.meta.json", meta)

    def _write_json_atomic(self, path: Path, payload) -> None:
        """Write to a temp file in the cache dir, then atomically replace:
        a crash mid-write never leaves truncated JSON at the final name."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload))
            os.replace(tmp_name, path)
        finally:
            # A no-op after a successful replace; removes the temp file if
            # the serialization or the replace itself failed.
            Path(tmp_name).unlink(missing_ok=True)

    def _read_cache(self, cache_name: str, cause: Exception):
        cache_file = self.cache_dir / f"{cache_name}.json"
        payload = self._load_cache_json(cache_file)
        if payload is _MISSING:
            raise SleeperUnavailableError(
                f"live fetch of '{cache_name}' failed and no usable cached "
                f"copy exists at {cache_file}"
            ) from cause
        return payload

    @staticmethod
    def _load_cache_json(path: Path):
        """A cache file's payload, or ``_MISSING`` when the file is absent
        or corrupt — a truncated write must read as cache-absent so the
        degradation path degrades instead of raising."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # ValueError covers JSONDecodeError and UnicodeDecodeError.
            return _MISSING
