"""Shared fixtures: a small in-memory league and fake HTTP transports.

No test in this suite touches the live network; the transport seam
(``http_get``) is replaced with fakes that serve canned payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from draftbot.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(name="config")
def config_fixture():
    """The real checked-in league config (static data, safe for tests)."""
    return load_config(REPO_ROOT / "league_config.toml")


def raw_auction_pick(
    pick_no: int, player_id: str, slot: int, amount: str, position: str = "WR"
) -> dict:
    """A minimal realistic raw auction pick: the winning bid is a STRING in
    ``metadata.amount`` and attribution runs by ``draft_slot``."""
    return {
        "round": 1 + (pick_no - 1) // 12,
        "pick_no": pick_no,
        "draft_slot": slot,
        "player_id": player_id,
        "picked_by": "",
        "is_keeper": None,
        "metadata": {"amount": amount, "position": position},
    }


class FakeTransport:
    """Serves canned JSON payloads by URL path suffix; records every request.

    Keys are path suffixes (query string ignored), so ``/picks`` matches the
    picks feed but never the draft object, and insertion order is irrelevant.
    """

    # pylint: disable=too-few-public-methods  # a fake transport: __call__ is
    # its entire interface, matching the http_get callable seam.

    def __init__(self, payloads: dict[str, object]):
        self.payloads = dict(payloads)
        self.requests: list[str] = []
        self.failing = False

    def __call__(self, url: str) -> bytes:
        self.requests.append(url)
        if self.failing:
            raise ConnectionError(f"simulated endpoint failure for {url}")
        path = urlsplit(url).path
        for suffix, payload in self.payloads.items():
            if path.endswith(suffix):
                return json.dumps(payload).encode("utf-8")
        raise ConnectionError(f"no canned payload for {url}")
