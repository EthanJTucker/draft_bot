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


def raw_keeper_pick(
    pick_no: int, player_id: str, slot: int, amount: str, position: str = "WR"
) -> dict:
    """A keeper entered by the commissioner as a PRICED draft pick.

    Same shape as :func:`raw_auction_pick` with ``is_keeper`` set, which is
    how this season's keepers actually reach the feed: a dollar
    ``metadata.amount`` (the chain price, not a market-clearing bid) rather
    than a ``draft.settings.budget_<slot>`` key.
    """
    return raw_auction_pick(pick_no, player_id, slot, amount, position) | {
        "is_keeper": True
    }


def config_with_a_wrong_typed_tunable(tmp_path):
    """The checked-in config with ``starter_pct`` quoted — the shape of a
    real TOML typo on a numeric knob.

    Shared because all five CLIs owe the same contract on it: a one-line
    error naming the key and exit 2, never a traceback out of the loader.
    """
    text = (REPO_ROOT / "league_config.toml").read_text(encoding="utf-8")
    assert "\nstarter_pct = 0.5\n" in text
    path = tmp_path / "typo.toml"
    path.write_text(
        text.replace("\nstarter_pct = 0.5\n", '\nstarter_pct = "0.5"\n'),
        encoding="utf-8",
    )
    return path
