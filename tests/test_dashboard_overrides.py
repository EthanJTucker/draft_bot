"""The override sheet as the dashboard serves it: ``/state`` and the page.

Every poller here runs on ``PERMUTED_SLOTS`` (roster id 7 drafting from
slot 4, slot 7 belonging to roster 11), so a rule that reaches for the
roster id where the draft slot is meant prices somebody else's money and
fails instead of passing by coincidence.

The page assertions are deliberately modest about what they prove. No
test in this repo executes the JavaScript in ``index.html``; these pin
that the delivery path exists (the token, the rule, the call site) and
that every SENTENCE the operator reads is composed in Python, where it is
pinned like any other value. What the browser actually paints was checked
by opening the page, and cannot be checked here.
"""

# pylint: disable=duplicate-code  # the analysis key set is pinned in
# BOTH directions here and in test_dashboard_state; that the two
# copies must be edited together is the pin working, not duplication
# to factor out - a shared constant would let one edit move both.

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from draftbot.dashboard import state as state_module
from draftbot.dashboard.app import create_app
from draftbot.dashboard.state import DashboardPoller
from draftbot.tracker import DraftTracker

from .helpers_dashboard import (
    ENTERED_BUDGETS,
    PERMUTED_SLOTS,
    ScriptedSource,
    make_tick,
)
from .helpers_engine import sheet_row
from .helpers_overrides import override_book

PAGE = Path(state_module.__file__).resolve().parent / "static" / "index.html"


def rows():
    """Three priced players; ``A`` is the nominee throughout."""
    return [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]


def override(**fields):
    """One override row for player ``A``, blank everywhere else."""
    return override_book("A", **fields)


def poller(config, *, overrides=None, offer=3):
    """A poller on the PERMUTED map, with real entered budgets so the
    verdict is not withheld for a reason this module is not about."""
    tick = make_tick(
        nominee="A",
        offer=offer,
        budgets=ENTERED_BUDGETS,
        slot_to_roster_id=PERMUTED_SLOTS,
    )
    return DashboardPoller(
        ScriptedSource([tick]),
        DraftTracker(config, clock=lambda: 0.0),
        rows(),
        config,
        keepers_by_slot={},
        clock=lambda: 1_000.0,
        overrides=overrides,
    )


def nomination(config, **kwargs):
    """One poll cycle's nomination block."""
    return poller(config, **kwargs).step()["nomination"]


def test_a_lot_with_no_override_row_serves_the_key_as_null(config):
    """The key is always present and the value is None on an un-listed
    player. A record that EXISTS is the page's signal to draw a chip, so
    an all-zero record here would claim a row nobody wrote."""
    assert nomination(config)["analysis"]["override"] is None
    assert nomination(config, overrides={})["analysis"]["override"] is None


def test_the_served_max_bid_already_has_the_override_in_it(config):
    """One number, decided once, in the engine. The page re-adds nothing,
    so the JSON, the verdict and the frozen record cannot disagree."""
    base = nomination(config)["analysis"]["max_bid"]

    tweaked = nomination(config, overrides=override(delta=9))["analysis"]

    assert tweaked["max_bid"] == base + 9
    assert tweaked["override"]["delta"] == 9
    assert tweaked["override"]["model_max_bid"] == base


def test_a_clamped_override_is_visible_in_the_payload_not_silently_eaten(config):
    """The subtle failure this feature has: a +$40 that the cap swallows
    shows the un-tweaked number, which reads as "my file did not load".
    The payload must say the override landed AND that the cap bound it."""
    served = nomination(config, overrides=override(delta=400))["analysis"]

    assert served["max_bid"] == served["my_cap"]
    assert served["override"]["clamped"] is True
    assert f"CAPPED at ${served['my_cap']}" in served["override"]["label"]


def test_an_avoid_row_serves_zero_and_withholds_the_verdict(config):
    """$0, not $1. And no BID/PASS: the verdict would read PASS by
    arithmetic, but its margin is ``0 - high_bid`` -- a plausible wrong
    number on a lot the operator has simply ruled out. The reason line
    says which, so the blank is explained rather than mysterious."""
    served = nomination(config, overrides=override(avoid=True))

    assert served["analysis"]["max_bid"] == 0
    assert served["verdict"] is None
    assert "avoid" in served["verdict_reason"].lower()


def test_target_tier_and_note_ride_through_without_moving_a_number(config):
    """Display-only columns stay display only: the page needs them to draw
    chips, and a personal tier must never become a price."""
    base = nomination(config)["analysis"]["max_bid"]

    served = nomination(
        config, overrides=override(target=True, tier=2, note="floor is high")
    )["analysis"]

    assert served["max_bid"] == base
    assert served["override"]["target"] is True
    assert served["override"]["tier"] == 2
    assert served["override"]["note"] == "floor is high"


def test_the_analysis_key_set_is_pinned_in_both_directions(config):
    """The ``/state`` contract. ``override`` is the one key this slice
    adds; the exact-set assertion is what makes a later stray field a
    failure rather than a surprise on the page."""
    served = nomination(config, overrides=override(delta=1))["analysis"]

    assert set(served) == {
        "rank",
        "worth",
        "room_price",
        "keeper_premium",
        "value",
        "inflation",
        "inflation_adjusted",
        "marginal_worth",
        "need_bump",
        "spend_margin",
        "spend_boost",
        "spend_adjusted",
        "tier",
        "my_cap",
        "max_bid",
        "override",
    }
    assert set(served["override"]) == {
        "delta",
        "avoid",
        "target",
        "tier",
        "note",
        "model_max_bid",
        "pre_cap",
        "clamped",
        "label",
    }


def test_the_verdict_docstring_names_every_withholding_rule_it_has(config):
    """The standing instruction on ``_verdict``: every ``return None``
    is one fail-closed rule and the docstring enumerates them all. The
    count is derived from the AST, never read off the prose -- reading
    the prose is exactly how the count has been mis-stated before.

    This slice adds the ninth (an avoid-marked lot), so the docstring
    must now say NINE and must name it."""
    tree = ast.parse(inspect.getsource(state_module))
    verdict = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_verdict"
    )
    withholding = [
        node
        for node in ast.walk(verdict)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Tuple)
        and isinstance(node.value.elts[0], ast.Constant)
        and node.value.elts[0].value is None
    ]

    assert len(withholding) == 9
    assert "ALL NINE" in ast.get_docstring(verdict)
    assert "avoid" in ast.get_docstring(verdict).lower()
    # And the rule is genuinely last: a preference must not pre-empt a
    # data problem, or the page would blame the operator's own file for
    # a feed the tool could not trust.
    assert withholding[-1].lineno == max(node.lineno for node in withholding)
    assert nomination(config, overrides=override(avoid=True))["verdict"] is None


@pytest.mark.parametrize(
    "fragment",
    [
        ".chip.tweak",  # the override token, distinct from the amber banner
        ".chip.target",
        "renderOverride",  # the call site
        "override.label",  # the sentence comes from Python, not from JS
    ],
)
def test_the_page_carries_the_delivery_path_for_the_override_marks(fragment):
    """Substring presence only. The whole guard over this file in this
    repo is substring matching -- a green assertion here is near-zero
    evidence that anything RENDERS, which is why the page was opened in a
    browser before this landed."""
    assert fragment in PAGE.read_text(encoding="utf-8")


def test_the_page_never_recomputes_a_dollar_from_the_override(config):
    """Every sentence and every figure the operator reads about an
    override is composed in Python. The page prints ``label`` and the
    already-resolved ``max_bid``; if it added the delta itself, the two
    could disagree and no test in this repo would see it."""
    page = PAGE.read_text(encoding="utf-8")
    assert "override.delta" not in page
    assert "override.pre_cap" not in page

    served = nomination(config, overrides=override(delta=9))["analysis"]
    assert str(served["max_bid"]) not in ("", None)
    assert served["override"]["label"].startswith("override +$9")


def test_the_served_page_and_the_state_route_agree_on_an_override(config):
    """End to end through the real app: the same poller feeds ``/state``
    and ``/``, so what the page fetches is what the engine decided."""
    live = poller(config, overrides=override(delta=9))
    live.step()
    client = TestClient(create_app(live))
    assert client.get("/").status_code == 200  # the page is served at all

    payload = client.get("/state").json()

    assert payload["nomination"]["analysis"]["override"]["delta"] == 9
    assert payload["nomination"]["analysis"]["override"]["label"].startswith(
        "override +$9"
    )
