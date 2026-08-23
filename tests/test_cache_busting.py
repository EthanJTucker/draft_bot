"""Anti-cheat: every poll must bust Sleeper's CDN cache.

Sleeper serves the draft object with s-maxage=30 and picks with s-maxage=15,
so a naive 2s poll loop silently reads identical stale bytes. The FakeCDN
below models that: it caches the response for each exact URL (query string
included) forever. A client that does not change a query param per request
receives the first response again and again and fails these tests.
"""

from __future__ import annotations

import json

from draftbot.sleeper_client import SleeperClient


class FakeCDN:
    """Serves the origin payload but caches responses per exact request URL."""

    # pylint: disable=too-few-public-methods  # a fake transport: __call__ is
    # its entire interface, matching the http_get callable seam.

    def __init__(self, origin: dict):
        self.origin = origin
        self._url_cache: dict[str, bytes] = {}
        self.requests: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requests.append(url)
        if url not in self._url_cache:
            self._url_cache[url] = json.dumps(self.origin).encode("utf-8")
        return self._url_cache[url]


def test_draft_poll_loop_sees_fresh_data_through_cdn(config, tmp_path):
    """Two successive draft polls must not accept CDN-stale identical bytes."""
    cdn = FakeCDN({"status": "drafting", "metadata": {"highest_offer": "17"}})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=cdn, clock=lambda: 1_000.0
    )

    first = client.get_draft()
    cdn.origin["metadata"]["highest_offer"] = "23"
    second = client.get_draft()

    assert first["metadata"]["highest_offer"] == "17"
    assert second["metadata"]["highest_offer"] == "23"


def test_picks_poll_loop_sees_fresh_data_through_cdn(config, tmp_path):
    """Same guarantee for the picks feed (s-maxage=15 on the live CDN)."""
    cdn = FakeCDN({"picks": []})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=cdn, clock=lambda: 1_000.0
    )

    assert client.get_picks() == {"picks": []}
    cdn.origin["picks"] = [{"player_id": "4034"}]
    assert client.get_picks()["picks"] == [{"player_id": "4034"}]


def test_two_clients_in_the_same_millisecond_never_collide(config, tmp_path):
    """Bust tokens are per-instance: two clients constructed on the same
    clock reading must still emit disjoint URLs, or the CDN serves one
    client the other's cached bytes."""
    cdn = FakeCDN({"status": "drafting"})
    first = SleeperClient(
        config, cache_dir=tmp_path / "a", http_get=cdn, clock=lambda: 1_000.0
    )
    second = SleeperClient(
        config, cache_dir=tmp_path / "b", http_get=cdn, clock=lambda: 1_000.0
    )
    for _ in range(3):
        first.get_draft()
        second.get_draft()

    assert len(set(cdn.requests)) == len(cdn.requests)


def test_every_request_carries_a_changing_query_param(config, tmp_path):
    """No two poll URLs may be identical, and each must carry a query string."""
    cdn = FakeCDN({"status": "drafting"})
    client = SleeperClient(
        config, cache_dir=tmp_path, http_get=cdn, clock=lambda: 1_000.0
    )
    for _ in range(5):
        client.get_draft()

    assert len(set(cdn.requests)) == len(cdn.requests)
    assert all("?" in url for url in cdn.requests)
