"""Shared fixtures: a small in-memory league and fake HTTP transports.

No test in this suite touches the live network; the transport seam
(``http_get``) is replaced with fakes that serve canned payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from draftbot.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(name="config")
def config_fixture():
    """The real checked-in league config (static data, safe for tests)."""
    return load_config(REPO_ROOT / "league_config.toml")


class FakeTransport:
    """Serves canned JSON payloads by URL prefix and records every request."""

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
        base = url.split("?", 1)[0]
        for prefix, payload in self.payloads.items():
            if base.startswith(prefix) or prefix in base:
                return json.dumps(payload).encode("utf-8")
        raise ConnectionError(f"no canned payload for {url}")
