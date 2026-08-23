"""SleeperClient: fetch, on-disk cache, and degradation behavior."""

from __future__ import annotations

import dataclasses
import json
import sys
import types

import pytest

from draftbot.sleeper_client import SleeperClient, SleeperUnavailableError

from .conftest import FakeTransport


def test_get_league_fetches_and_snapshots_to_disk(config, tmp_path):
    """A fetch returns parsed JSON and leaves a cache file on disk."""
    league_payload = {"league_id": config.league_id, "name": "12th Week Campers"}
    transport = FakeTransport({f"/league/{config.league_id}": league_payload})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )

    league = client.get_league()

    assert league["name"] == "12th Week Campers"
    cached = json.loads((tmp_path / "league.json").read_text(encoding="utf-8"))
    assert cached == league_payload


def test_endpoint_failure_serves_cached_copy(config, tmp_path):
    """Simulated endpoint failure: the client falls back to the disk cache."""
    payload = {"league_id": config.league_id, "name": "12th Week Campers"}
    transport = FakeTransport({f"/league/{config.league_id}": payload})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )
    assert client.get_league() == payload

    transport.failing = True

    assert client.get_league() == payload


def test_endpoint_failure_without_cache_raises(config, tmp_path):
    """With no cached copy to fall back on, the failure is surfaced."""
    transport = FakeTransport({})
    transport.failing = True
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )

    with pytest.raises(SleeperUnavailableError):
        client.get_league()


def test_player_map_cached_once_daily(config, tmp_path):
    """The ~14 MB player map is fetched at most once per day."""
    now = {"t": 1_000_000.0}
    transport = FakeTransport({"/players/nfl": {"4034": {"last_name": "Hill"}}})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: now["t"]
    )

    assert client.get_players()["4034"]["last_name"] == "Hill"
    assert len(transport.requests) == 1

    now["t"] += 3_600  # one hour later: cache still fresh, no network
    assert client.get_players()["4034"]["last_name"] == "Hill"
    assert len(transport.requests) == 1

    # A new client instance (fresh process) must honor the same freshness.
    second = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: now["t"]
    )
    assert second.get_players()["4034"]["last_name"] == "Hill"
    assert len(transport.requests) == 1

    now["t"] += config.player_map_max_age_seconds  # past a day: refetch
    assert client.get_players()["4034"]["last_name"] == "Hill"
    assert len(transport.requests) == 2


def test_stale_player_map_survives_endpoint_failure(config, tmp_path):
    """A stale on-disk player map is still served if the refetch fails."""
    now = {"t": 1_000_000.0}
    transport = FakeTransport({"/players/nfl": {"4034": {"last_name": "Hill"}}})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: now["t"]
    )
    client.get_players()

    now["t"] += config.player_map_max_age_seconds + 1
    transport.failing = True

    assert client.get_players()["4034"]["last_name"] == "Hill"


def test_historical_picks_fetch_never_corrupts_the_live_cache(config, tmp_path):
    """Anti-cheat (issue #3 seam): after fetching a historical draft's
    picks, a live-endpoint outage must serve the LIVE cache, never last
    season's picks masquerading as the live feed."""
    live_picks = [{"player_id": "L", "metadata": {"amount": "10"}}]
    hist_id = config.historical_draft_ids["2025"]
    hist_picks = [{"player_id": "H", "metadata": {"amount": "99"}}]
    transport = FakeTransport(
        {
            f"/draft/{config.draft_id}/picks": live_picks,
            f"/draft/{hist_id}/picks": hist_picks,
        }
    )
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )
    assert client.get_picks() == live_picks
    assert client.get_picks(draft_id=hist_id) == hist_picks

    transport.failing = True

    fallback = client.get_picks()
    assert fallback == live_picks
    assert fallback != hist_picks


def test_historical_cache_alone_cannot_serve_as_the_live_fallback(config, tmp_path):
    """With only a historical fetch cached, a live failure raises instead of
    silently serving 2025 picks as the 2026 feed."""
    hist_id = config.historical_draft_ids["2025"]
    transport = FakeTransport({f"/draft/{hist_id}/picks": [{"player_id": "H"}]})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )
    client.get_picks(draft_id=hist_id)

    transport.failing = True

    with pytest.raises(SleeperUnavailableError):
        client.get_picks()


def test_draft_and_picks_caches_are_keyed_by_draft_id(config, tmp_path):
    """Each draft snapshots under its own id on disk."""
    hist_id = config.historical_draft_ids["2025"]
    transport = FakeTransport(
        {
            f"/draft/{config.draft_id}": {"status": "drafting"},
            f"/draft/{hist_id}": {"status": "complete"},
            f"/draft/{config.draft_id}/picks": [],
            f"/draft/{hist_id}/picks": [],
        }
    )
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )
    assert client.get_draft()["status"] == "drafting"
    assert client.get_draft(draft_id=hist_id)["status"] == "complete"
    client.get_picks()
    client.get_picks(draft_id=hist_id)

    for name in (
        f"draft_{config.draft_id}",
        f"draft_{hist_id}",
        f"picks_{config.draft_id}",
        f"picks_{hist_id}",
    ):
        assert (tmp_path / f"{name}.json").exists(), name


def test_clock_rollback_invalidates_player_map_freshness(config, tmp_path):
    """A wall clock that rolls backward (cache stamped in the future) must
    refetch rather than trust the future-stamped cache forever."""
    now = {"t": 1_000_000.0}
    transport = FakeTransport({"/players/nfl": {"4034": {"last_name": "Hill"}}})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: now["t"]
    )
    client.get_players()
    assert len(transport.requests) == 1

    now["t"] -= 3_600  # clock rolled back one hour

    client.get_players()
    assert len(transport.requests) == 2


def test_truncated_player_map_with_fresh_meta_refetches_live(config, tmp_path):
    """A corrupt players.json under a fresh meta stamp (crash mid-write)
    must refetch live, not raise JSONDecodeError at startup forever."""
    transport = FakeTransport({"/players/nfl": {"4034": {"last_name": "Hill"}}})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000_000.0
    )
    client.get_players()
    (tmp_path / "players.json").write_text('{"4034": {"last_na', encoding="utf-8")

    assert client.get_players()["4034"]["last_name"] == "Hill"
    assert len(transport.requests) == 2


def test_corrupt_player_meta_reads_as_not_fresh(config, tmp_path):
    """An unreadable meta file means "not fresh": refetch, don't crash."""
    transport = FakeTransport({"/players/nfl": {"4034": {"last_name": "Hill"}}})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000_000.0
    )
    client.get_players()
    (tmp_path / "players.meta.json").write_text("not json at all", encoding="utf-8")

    assert client.get_players()["4034"]["last_name"] == "Hill"
    assert len(transport.requests) == 2


def test_corrupt_endpoint_cache_degrades_as_cache_absent(config, tmp_path):
    """A corrupt cache file on the fallback path raises the clean
    SleeperUnavailableError, never a raw JSONDecodeError mid-degradation."""
    transport = FakeTransport({f"/league/{config.league_id}": {"name": "x"}})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )
    client.get_league()
    (tmp_path / "league.json").write_text("{truncated", encoding="utf-8")

    transport.failing = True

    with pytest.raises(SleeperUnavailableError):
        client.get_league()


def test_cache_writes_leave_only_the_final_files(config, tmp_path):
    """Atomic writes: the temp file is always replaced or removed, so the
    cache dir holds only the final JSON files."""
    transport = FakeTransport({f"/league/{config.league_id}": {"name": "x"}})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )
    client.get_league()

    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["league.json"]


def test_snapshot_all_writes_every_endpoint_to_disk(config, tmp_path):
    """Startup snapshot: every endpoint fetched and persisted in one call."""
    transport = FakeTransport(
        {
            "/picks": [{"player_id": "4034"}],
            "/rosters": [{"roster_id": 7}],
            "/users": [{"user_id": config.my_user_id}],
            "/players/nfl": {"4034": {"last_name": "Hill"}},
            "/projections/nfl/2026": [{"player_id": "4034", "stats": {}}],
            f"/draft/{config.draft_id}": {"status": "pre_draft"},
            f"/league/{config.league_id}": {"name": "12th Week Campers"},
        }
    )
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )

    snapshot = client.snapshot_all()

    assert snapshot["league"]["name"] == "12th Week Campers"
    assert snapshot["draft"]["status"] == "pre_draft"
    assert snapshot["picks"] == [{"player_id": "4034"}]
    assert snapshot["rosters"] == [{"roster_id": 7}]
    assert snapshot["users"] == [{"user_id": config.my_user_id}]
    assert snapshot["players"]["4034"]["last_name"] == "Hill"
    assert snapshot["projections"][0]["player_id"] == "4034"
    for name in (
        "league",
        f"draft_{config.draft_id}",
        f"picks_{config.draft_id}",
        "rosters",
        "users",
        "players",
        "projections_2026",
    ):
        assert (tmp_path / f"{name}.json").exists(), name


def test_default_transport_uses_the_config_timeout(config, tmp_path, monkeypatch):
    """The requests transport takes its timeout from the config, so draft
    night can tune it in the TOML without a code change."""
    captured = {}

    def fake_get(url, timeout=None):  # pylint: disable=unused-argument  # the
        # url is the transport's business; this fake only captures the timeout.
        captured["timeout"] = timeout
        return types.SimpleNamespace(content=b"{}", raise_for_status=lambda: None)

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=fake_get))
    tuned = dataclasses.replace(config, request_timeout_seconds=3)
    client = SleeperClient(tuned, cache_dir=tmp_path, clock=lambda: 1_000.0)

    client.get_league()

    assert captured["timeout"] == 3


def test_projections_url_uses_com_host_with_positions(config, tmp_path):
    """Projections live on api.sleeper.com (not .app) with position params."""
    transport = FakeTransport({"/projections/nfl/2026": []})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=transport, clock=lambda: 1_000.0
    )
    client.get_projections()

    url = transport.requests[-1]
    assert url.startswith("https://api.sleeper.com/projections/nfl/2026")
    assert "season_type=regular" in url
    assert "position[]=QB" in url
    assert "position[]=DEF" in url
