"""The static page as the ``/`` route serves it: the skeleton it binds
and the delivery path for every mark and banner on it.

Split out of ``test_dashboard_app.py``: these two tests are the whole
contract for ``static/index.html`` and they grow with every mark added to
the page, while that module had reached one line under pylint's
1000-line ceiling. Splitting beat a ``too-many-lines`` disable, the same
trade the state module made at the same ceiling.

WHAT A GREEN SUITE DOES NOT MEAN FOR ``static/index.html``. Nothing here
executes that file. Its ``<script>`` block is 292 lines and no test in
this repo runs a line of it: there is no jsdom, no node, no browser. The
entire guard is substring matching over the HTML the ``/`` route served,
so it is strong on the exact strings it names and blind to everything
else about the page. Specifically invisible to it:

* CSS APPENDED BELOW what is pinned. A later ``:root`` re-declaring a
  token, an equal-specificity duplicate of a pinned rule, a rule hiding
  a container or an item, a higher-specificity rule (``#topbar
  #max-bid`` beats ``#max-bid.guessed`` 2-0-0), or an ``!important``
  anywhere all leave every asserted declaration byte-identical and
  repaint the page.

  Counting is what closes any of these, and it closes exactly one
  shape: a SECOND rule spelled with a selector that is counted. The
  asserts below hold ``--amber``, ``#banners {``, ``.banner {``,
  ``.banner.amber`` and the three ``.guessed`` mark rules at one
  occurrence each. Appending duplicates of those three ``.guessed``
  selectors in ``var(--accent)`` repainted every amber mark on the page
  in confident blue at exit 0 — same selectors, no ``!important``, no
  added specificity, one cascade level below the token — and that is
  what the three ``.guessed`` counts now stop.

  WHAT COUNTING CANNOT REACH, all of it measured green today: a rule
  whose selector is not one of the counted ones. ``#topbar #max-bid
  { color: var(--accent); }`` wins on specificity; the same declaration
  with ``!important`` wins outright; and ``#max-bid``, ``#my-team`` or
  ``.needs`` set to ``display: none`` takes the marked figure, my whole
  panel or the roster's RESTART note off the screen entirely. Closing
  those by string would mean counting every selector this file spells
  and forbidding every one it does not, against an unbounded set of
  selectors reaching the same elements and an unbounded set of
  spellings for "hidden" (``visibility``, a zero size, a transparent
  colour). This suite cannot carry that check, so it does not claim to.
  Reading the computed styles in a browser is what catches them.
* A RETARGETED assignment. Sending an asserted class expression to a
  different element leaves the expression spelled exactly as pinned, and
  also survives a full green run today.
* CALL SITES, and separately the DOM SINKS they write into. Most render
  pins assert a loop BODY, which survives both deleting the call and
  retargeting the ``innerHTML`` or ``put`` it feeds. Two call sites and
  five sinks are pinned below; every other sink is not.
* ELEMENT COVERAGE. The script queries 26 ids; the id list below names
  12. ``my-remaining``, ``my-caps``, ``my-roster``, ``max-bid-sub`` and
  ``verdict-sub`` are among the fourteen it omits. The writes into
  ``my-roster``, ``max-bid-sub`` and ``verdict-sub`` are pinned as sinks
  below — named, not counted from either end of that list, so moving a
  name cannot quietly make this sentence false. ``my-remaining`` and
  ``my-caps`` are not pinned at all.

The disposition is deliberate: write the limit down rather than keep
adding pins that cannot reach it, and do not stand up a JavaScript test
framework for it. **Any change to that page must be verified by
rendering it in a browser and reading the computed styles.** Five review
rounds have done exactly that, and this docstring is why the next must.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from draftbot.dashboard.app import create_app

from .helpers_dashboard import make_poller, make_tick
from .helpers_engine import sheet_row


def _rows():
    return [
        sheet_row(1, "A", "WR", 37.0),
        sheet_row(2, "B", "WR", 20.0),
        sheet_row(3, "C", "QB", 11.0),
    ]


def test_index_page_carries_a_slot_for_every_required_element(config):
    """The static page is one self-contained file (no CDN, no npm) whose
    skeleton binds every element the issue names: nominee, high bid,
    worth, room price, max bid, verdict, profit, last-of-tier, the
    positional table, team budgets, and my pinned roster.

    The delivery path for the marks and banners named here — the tokens,
    the CSS rules, the render call sites — is the sibling test below."""
    poller = make_poller(config, [make_tick()], _rows())
    client = TestClient(create_app(poller))

    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    for element_id in (
        'id="nominee"',
        'id="high-bid"',
        'id="worth"',
        'id="room-price"',
        'id="max-bid"',
        'id="verdict"',
        'id="profit"',
        'id="tier-warning"',
        'id="players"',
        'id="teams"',
        'id="my-team"',
        'id="banners"',
        'id="foot-note"',
    ):
        assert element_id in page
    assert "http://" not in page.replace("http://localhost", "")
    assert "https://" not in page  # fully self-contained: no external assets
    assert "/state" in page
    # The honest-failure surfaces: a crashed render says so in red, and
    # the default-budget asterisk carries its legend.
    assert "PAGE RENDER FAILED" in page
    assert "budget not entered on Sleeper yet" in page
    # The defaulted-budget marks. No test in this suite executes the
    # page's JavaScript, so these assert the SOURCE EXPRESSIONS rather
    # than bare key names: `var guessed = false;` and `var guessedBudget
    # = false;` both mute a mark while leaving every bare key elsewhere
    # in the file, and both survived the suite before these lines. The
    # bare-name version cannot catch them either, because
    # `me.budget_is_default` is a substring of `s.me.budget_is_default`
    # and of `t.budget_is_default`. The rendering itself (amber on the
    # my-team money, the caps line, the max-bid figure and its sub;
    # plain when the budgets are real) was checked in a browser.
    assert "!!me.budget_is_default" in page
    assert "!!(s.me && s.me.budget_is_default)" in page
    # The ROOM's two faults, kept apart on purpose: money nobody entered,
    # and money somebody entered that a keeper roster provably cannot have
    # left. Only the second reaches the page through its own key, because
    # its provenance is real and every other mark therefore reads it as
    # correct. Both feed the same amber.
    assert "var roomDefault = (s.defaulted_keeper_slots || []).length;" in page
    assert "var roomImpossible = (s.impossible_keeper_slots || []).length;" in page
    assert "!!(roomDefault || roomImpossible)" in page
    # My own impossible money.
    assert "(s.impossible_keeper_slots || []).indexOf(me.slot) >= 0" in page
    assert " · BUDGET IMPOSSIBLE: above what my keepers can leave" in page
    assert "(room IMPOSSIBLE ×" in page
    # The THIRD fault behind the same amber, and the only board-wide one.
    # Pinned at its SOURCE on both readers: it arrives as a plain boolean
    # no other mark reads, so re-sourcing either from `s.paused` marks a
    # board that clears itself, at exit 0. The qualifier is counted at
    # three, and is its own: BUDGET NOT ENTERED would be false here.
    assert "var oldOrder = !!s.keeper_map_stale;" in page
    assert "var myOldOrder = !!s.keeper_map_stale;" in page
    assert page.count("OLD DRAFT ORDER — RESTART") == 3
    # The count above closes the UNDER-marking direction only. Dropping
    # the `myOldOrder ?` guard on the roster note leaves the count at 3
    # and renders a standing RESTART instruction over every operator's
    # roster on every board, at exit 0 — the over-marking direction, and
    # the failure the comment beside that line argues against. So the
    # roster note is pinned WITH its conditional, like the other four.
    assert '(myOldOrder ? \'<li class="needs">OLD DRAFT ORDER' in page
    # The teams table's second mark, its SOURCE, and the legend clause
    # that reads it. The `!` is the only mark in this family that routes
    # through an alias, and pinning the consumer alone leaves the binding
    # free: re-sourcing the alias from `s.defaulted_keeper_slots` puts the
    # `!` on exactly the rows it must never mark and none of the rows it
    # must (the two sets are disjoint by construction), at exit 0.
    assert "var impossibleSlots = s.impossible_keeper_slots || [];" in page
    assert "(impossibleSlots.indexOf(t.slot) >= 0 ? ' !' : '')" in page
    assert "above what that team's keepers can possibly leave" in page
    # The amber DECLARATIONS, not merely the selectors. Repointing any of
    # these three at var(--green) / var(--dim) / var(--accent) leaves the
    # selector spelled exactly as before and neuters the mark, which is
    # how the whole family survived the looser version of this check.
    my_team_marks = "#my-team .money.guessed, #my-team .caps.guessed"
    assert my_team_marks + " { color: var(--amber); }" in page
    assert ".stat .sub.guessed { color: var(--amber); }" in page
    # The id in that last one is load-bearing twice over: a bare
    # `.value.guessed` loses the cascade to `#max-bid`, and a
    # var(--accent) fill leaves the 30px figure in confident blue.
    assert "#max-bid.guessed { color: var(--amber); }" in page
    # The four class assignments that put the amber ON the max bid, its
    # sub-line, my money and my caps line. Whole expressions, not element
    # ids: dropping a conditional back to a bare 'value num', or one
    # disjunct out of it, survives every looser form of this check.
    for class_expression in (
        "'value num' + (guessedBudget || guessedRoom || oldOrder ? ' guessed' : '')",
        "'sub' + (analysis && (guessedBudget || guessedRoom || oldOrder)"
        " ? ' guessed' : '')",
        "'money num' + (guessed || myBudgetImpossible || myOldOrder ? ' guessed' : '')",
        "'caps' + (guessed || myBudgetImpossible || myOldOrder ? ' guessed' : '')",
    ):
        assert class_expression in page
    assert "room default" in page  # the sub-line's room qualifier
    # Each flag is assigned exactly once. A declaration assert cannot see
    # a re-assignment appended after it (`var guessed = ...; guessed =
    # false;`), which mutes every mark below while leaving all three
    # declarations above intact.
    for flag in (
        "guessed",
        "guessedBudget",
        "guessedRoom",
        "roomDefault",
        "roomImpossible",
        "oldOrder",
        "myBudgetImpossible",
        "myOldOrder",
        "impossibleSlots",
    ):
        assert page.count(flag + " = ") == 1
    # BUDGET NOT ENTERED appears TWICE in this file, on the max-bid sub
    # and on the my-team caps line, so the bare substring is satisfied by
    # either one and dropping the other is invisible. Both are pinned
    # here in the form they actually render, separator included.
    assert "'BUDGET NOT ENTERED · '" in page
    assert " · BUDGET NOT ENTERED: shown at the league default" in page
    # The my-team caps line leads with the DRAFT slot, which is the
    # number --budget is keyed by; 'roster ' + me.slot renders a
    # plausible wrong label for the identifier this whole rule turns on.
    assert "'draft slot ' + me.slot" in page
    # The teams table's first column, which the legend below points at.
    assert '\'"><td class="num">\' + t.slot' in page
    # The legend that tells the operator what the amber and the asterisk
    # mean, and which identifier to re-key by. Its two load-bearing
    # claims, so reverting it to the pre-fix wording fails here.
    assert "computed from all twelve budgets, not just mine" in page
    assert "SLOT is the DRAFT slot in the first column, not a roster id" in page


def _served_page(config):
    """The page exactly as the ``/`` route serves it."""
    poller = make_poller(config, [make_tick()], _rows())
    return TestClient(create_app(poller)).get("/").text


def test_index_page_delivers_the_marks_and_banners_it_spells(config):
    """The DELIVERY PATH for every mark and every banner pinned next door.

    Each of these silences work this file already does while leaving every
    assert in the sibling test spelled exactly as it is, so a green suite
    was compatible with an entirely unmarked, bannerless screen.

    Split out of that test rather than appended to it: the combined
    function crossed pylint's 50-statement ceiling, and splitting beat
    adding a disable — the same trade the state module made at its own
    1000-line ceiling.
    """
    page = _served_page(config)

    # 1. The token itself. Repointing --amber at the accent blue renders
    #    every amber mark on the page as a confident one, and all three
    #    `var(--amber)` declarations the sibling test pins stay
    #    byte-identical.
    #    A COUNT, not a presence check: an appended second `:root` block
    #    re-declaring `--amber` wins the cascade while leaving the first
    #    declaration byte-identical, which is the same attack the banner
    #    rule below is counted against.
    assert page.count("--amber:") == 1
    assert "--amber: #ffb454;" in page
    assert "--accent: #5aa9ff;" in page
    # 2. The rule that paints an amber banner. A second `.banner.amber`
    #    block setting `display: none` hides every one of them, so the
    #    count is what is pinned, not merely the declaration.
    assert page.count(".banner.amber") == 1
    assert (
        ".banner.amber { background: #3d2c10; color: var(--amber); "
        "border: 1px solid var(--amber); }" in page
    )
    # 3. The container those banners are painted into. An appended
    #    `#banners { display: none; }` hides every banner on the page
    #    while `.banner.amber` still counts exactly 1, so the container
    #    rule is counted too. (`#banners:not(:empty)` does not match.)
    assert page.count("#banners {") == 1
    # 4. The loop that turns settings_warnings into banners at all.
    #    Deleting it takes EVERY settings banner off the page, including
    #    the budget ones this module tests through /state.
    assert "(s.settings_warnings || []).forEach(function (w) {" in page
    assert "out.push(['amber', 'SETTINGS DIFFER — ' + esc(w.field)" in page
    # 5. The two CALL SITES in the normal render path. Everything above
    #    pins loop BODIES, which survive deleting the call: no banner and
    #    no team row would reach the page and every assert here would
    #    still pass. The two-space indent is what distinguishes them from
    #    `try { renderBanners(s); }` in the crash handler and from
    #    `renderBanners(lastState || {})` in the disconnected path.
    assert "  renderBanners(s);" in page
    assert "  renderTeams(s);" in page
    # 6. The teams-table asterisk, as a whole expression: the bare key
    #    name survives replacing the conditional with ''.
    assert "(t.budget_is_default ? ' *' : '')" in page
    # 7. The cache banner's condition. A snapshot key that is read but
    #    never branched on is the same as one never read.
    assert "if (s.stale_endpoints && s.stale_endpoints.length)" in page
    # 8. The DOM SINKS those renders write into, and the banner ITEM
    #    class. Everything above pins a loop BODY, a CALL SITE or a
    #    container, and none of that sees the assignment retargeted at a
    #    detached node, or an appended `.banner { display: none; }` that
    #    hides red and amber alike while `.banner.amber` and `#banners {`
    #    each still count 1. Five one-line edits took the banners, every
    #    team row with its * and ! marks, my whole roster with the RESTART
    #    note above it, the entire max-bid sub-line — cap figure, BUDGET
    #    NOT ENTERED, OLD DRAFT ORDER — RESTART and both room qualifiers —
    #    and the withheld verdict's reason off the page at exit 0, spelled
    #    above exactly as pinned. The roster and cap-sub sinks are here
    #    because they now carry marks of their own, not only their text.
    #    Each of these is a PRESENCE pin, and presence is all it proves:
    #    an appended line overwriting the same element still passes.
    assert "document.getElementById('banners').innerHTML = out.map(" in page
    assert "document.getElementById('teams').innerHTML = (s.teams || []).map(" in page
    assert "document.getElementById('my-roster').innerHTML =" in page
    assert "var capEl = document.getElementById('max-bid-sub');" in page
    assert "put('verdict-sub', nom.verdict_reason || '')" in page
    assert page.count(".banner {") == 1
    # 9. The three amber MARK rules, counted. The `--amber:` count above
    #    closes the token; this closes the same attack one cascade level
    #    lower, where it does not need the token at all. Appending
    #    equal-specificity duplicates of these three selectors in
    #    `var(--accent)` — same selectors, later in the file, no
    #    `!important` and no added specificity — repainted every amber
    #    mark on the page, including all four this branch added, in
    #    confident blue, at exit 0 with the declarations next door still
    #    byte-identical. These are the marks that say a 30px figure is a
    #    guess, so blue is the one colour they must never be.
    assert page.count("#max-bid.guessed") == 1
    assert page.count(".money.guessed") == 1
    assert page.count(".sub.guessed") == 1
