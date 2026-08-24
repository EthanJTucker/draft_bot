"""Value-sheet CLI: ranked CSV + printed table, offline and deterministic.

The transport is canned (no network); the CLI's own cache degradation is
exercised by priming the disk cache and then failing every live fetch.
"""

from __future__ import annotations

import io

import pytest

from draftbot.valuesheet import main
from tests.conftest import FakeTransport
from tests.helpers_valuation import projection_row

CONFIG_TOML = """\
[league]
name = "Test League"
season = 2026
league_id = "L1"
draft_id = "D2026"
teams = 2

[league.historical_drafts]
2023 = "D23"
2024 = "D24"
2025 = "D25"

[auction]
budget = 12

[roster]
QB = 1
RB = 1
BN = 1

[keeper]
cost_increment = 2
cost_floor = 5
max_keepers = 3
max_consecutive_years = 2

[me]
username = "t"
user_id = "u"
roster_id = 1
"""


def _proj(player_id, position, adp, pts, years_exp=1):
    """One canned projections row (named "P <id>" so tables show the id)."""
    return projection_row(
        player_id, position, adp, pts=pts, years_exp=years_exp, name=f"P {player_id}"
    )


def _pick(player_id, position, amount, slot=1):
    """One canned picks-feed entry (string amount, like the live API)."""
    return {
        "player_id": player_id,
        "draft_slot": slot,
        "metadata": {"amount": str(amount), "position": position},
    }


def _draft(draft_id):
    """One canned draft object (six slots owned by rosters 1-6)."""
    return {
        "draft_id": draft_id,
        "status": "complete",
        "type": "auction",
        "settings": {},
        "metadata": {},
        "slot_to_roster_id": {str(slot): slot for slot in range(1, 7)},
    }


def _payloads():
    """A tiny but complete world: six 2023 RB bids ($5 x3, $15 x3, all ADP
    10) pin room(RB, 10) at the $10 band median, and the 2026 pool ranks
    qb1 > qb2 > rb1 > rb2 > qb3/qb4/rb3 by worth (id ties the floor trio).

    The 2024 feed carries ONE row: h0's owner (roster 1 both years) holds
    him at exactly max(5+2, 5) = $7 — an unflagged keeper entry, exactly
    as verified in the league's real 2025 feed. Counted as a bid it would
    drag the band median from $10 to $7, so the assertion on rb1's room
    price fails any implementation that skips keeper detection.
    """
    amounts = [5, 5, 5, 15, 15, 15]
    proj_2023 = [_proj(f"h{i}", "RB", 10.0, 100.0, years_exp=1) for i in range(6)]
    picks_2023 = [_pick(f"h{i}", "RB", amounts[i], slot=i + 1) for i in range(6)]
    proj_2024 = [_proj("h0", "RB", 10.0, None, years_exp=1)]
    picks_2024 = [_pick("h0", "RB", 7, slot=1)]
    proj_2026 = [
        _proj("qb1", "QB", 40.0, 300.0),
        _proj("qb2", "QB", 60.0, 200.0),
        _proj("qb3", "QB", None, 100.0),
        # 999.0 is Sleeper's no-ADP sentinel: qb4 prices at the floor and
        # the CSV blanks his ADP; the magic number never reaches output.
        _proj("qb4", "QB", 999.0, 90.0),
        _proj("rb1", "RB", 10.0, 30.0),
        _proj("rb2", "RB", None, 20.0),
        _proj("rb3", "RB", None, 10.0),
    ]
    return {
        "/draft/D23/picks": picks_2023,
        "/draft/D24/picks": picks_2024,
        "/draft/D25/picks": [],
        "/draft/D23": _draft("D23"),
        "/draft/D24": _draft("D24"),
        "/draft/D25": _draft("D25"),
        "/projections/nfl/2023": proj_2023,
        "/projections/nfl/2024": proj_2024,
        "/projections/nfl/2025": [],
        "/projections/nfl/2026": proj_2026,
    }


@pytest.fixture(name="world")
def world_fixture(tmp_path):
    """Config file, cache dir, transport, and a runner for the CLI."""
    config_path = tmp_path / "league.toml"
    config_path.write_text(CONFIG_TOML, encoding="utf-8")
    transport = FakeTransport(_payloads())

    def run(*extra_args):
        out = io.StringIO()
        code = main(
            [
                "--config",
                str(config_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                *extra_args,
            ],
            http_get=transport,
            out=out,
        )
        return code, out.getvalue()

    return tmp_path, transport, run


def test_cli_writes_the_ranked_priced_pool(world):
    """Exit 0; CSV ranked by value desc with worth, room price, and
    NPV-adjusted value per player; exact dollars from the tiny league:
    $18 discretionary over 330 points of value over replacement."""
    tmp_path, _, run = world
    out_csv = tmp_path / "sheet.csv"
    code, printed = run("--out", str(out_csv))
    assert code == 0
    lines = out_csv.read_text(encoding="utf-8").strip().split("\n")
    header, *rows = lines
    assert header == (
        "rank,player_id,name,position,adp,points,worth,"
        "room_price,price_source,keeper_premium,value"
    )
    cells = [line.split(",") for line in rows]
    assert [c[1] for c in cells] == ["qb1", "qb2", "rb1", "rb2", "qb3", "qb4", "rb3"]
    by_id = {c[1]: c for c in cells}
    assert by_id["qb1"][6] == f"{1 + 200 * 18 / 330:.2f}"  # worth
    assert by_id["rb1"][7] == "10.00"  # room price: the 6-bid band median
    assert by_id["rb1"][8] == "band"
    assert by_id["qb1"][8] == "floor"  # no QB bid history in this world
    assert [c[0] for c in cells] == [str(n) for n in range(1, 8)]
    assert "qb1" in printed  # the printed table shows the top of the pool


def test_no_adp_sentinel_is_blanked_in_the_csv(world):
    """qb4's feed ADP is the 999.0 sentinel: the CSV blanks it exactly
    like a missing ADP, the magic number appears nowhere in the output,
    and the player still prices at the $1 floor."""
    tmp_path, _, run = world
    out_csv = tmp_path / "sentinel.csv"
    code, printed = run("--out", str(out_csv))
    assert code == 0
    text = out_csv.read_text(encoding="utf-8")
    assert "999" not in text
    assert "999" not in printed
    by_id = {
        c[1]: c for c in (line.split(",") for line in text.strip().split("\n")[1:])
    }
    assert by_id["qb4"][4] == ""  # ADP blank, exactly like missing points
    assert by_id["qb4"][7] == "1.00"  # still prices at the floor
    assert by_id["qb4"][8] == "floor"


def test_valuation_tunables_flow_from_the_config_file(tmp_path):
    """A [valuation] section with min_band_samples = 7 starves rb1's 6-bid
    band; this world's RB bids share one ADP, so no curve can fit and rb1
    lands on the $1 floor instead of the $10 band median — proof the
    config file's knobs reach the fitted models."""
    config_path = tmp_path / "league.toml"
    config_path.write_text(
        CONFIG_TOML + "\n[valuation]\nmin_band_samples = 7\n", encoding="utf-8"
    )
    out = io.StringIO()
    out_csv = tmp_path / "sheet.csv"
    code = main(
        [
            "--config",
            str(config_path),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--out",
            str(out_csv),
        ],
        http_get=FakeTransport(_payloads()),
        out=out,
    )
    assert code == 0
    lines = out_csv.read_text(encoding="utf-8").strip().split("\n")
    by_id = {c[1]: c for c in (line.split(",") for line in lines[1:])}
    assert by_id["rb1"][7] == "1.00"  # the default config prices this 10.00
    assert by_id["rb1"][8] == "floor"


def test_csv_is_byte_identical_across_runs(world):
    """Determinism gate: same cached inputs, two runs, identical bytes."""
    tmp_path, _, run = world
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    assert run("--out", str(first))[0] == 0
    assert run("--out", str(second))[0] == 0
    assert first.read_bytes() == second.read_bytes()


def test_default_out_path_lands_under_the_configs_data_dir(world):
    """No --out: the sheet writes to <config dir>/data/value_sheet_2026.csv."""
    tmp_path, _, run = world
    code, _ = run()
    assert code == 0
    assert (tmp_path / "data" / "value_sheet_2026.csv").exists()


def test_degraded_endpoints_still_produce_the_sheet_with_a_warning(world):
    """Once the cache is primed, a dead API degrades every endpoint: the
    sheet still builds (exit 0) and the output says cache, not live."""
    tmp_path, transport, run = world
    assert run("--out", str(tmp_path / "live.csv"))[0] == 0
    transport.failing = True
    code, printed = run("--out", str(tmp_path / "cached.csv"))
    assert code == 0
    assert "WARNING" in printed
    assert "cache" in printed
    assert (tmp_path / "live.csv").read_bytes() == (
        tmp_path / "cached.csv"
    ).read_bytes()


def test_dead_api_with_no_cache_exits_2(world):
    """Nothing live and nothing cached: a clear error, exit 2."""
    tmp_path, transport, run = world
    transport.failing = True
    code, printed = run("--out", str(tmp_path / "never.csv"))
    assert code == 2
    assert "error" in printed.lower()
    assert not (tmp_path / "never.csv").exists()


def test_missing_config_exits_2(tmp_path):
    """A bad --config path is a usage error, not a traceback."""
    out = io.StringIO()
    code = main(
        ["--config", str(tmp_path / "nope.toml")],
        http_get=lambda url: b"{}",
        out=out,
    )
    assert code == 2
    assert "config" in out.getvalue().lower()


def test_malformed_toml_config_exits_2(tmp_path):
    """Unparseable TOML is a one-line error and exit 2, not a traceback."""
    bad = tmp_path / "bad.toml"
    bad.write_text("[league\nname =", encoding="utf-8")
    out = io.StringIO()
    code = main(["--config", str(bad)], http_get=lambda url: b"{}", out=out)
    assert code == 2
    printed = out.getvalue()
    assert "error" in printed.lower()
    assert "toml" in printed.lower()
    assert "Traceback" not in printed


def test_config_missing_required_section_exits_2(tmp_path):
    """Valid TOML missing a required table ([keeper]) names the missing
    key in a one-line error and exits 2, not a traceback."""
    partial = tmp_path / "partial.toml"
    partial.write_text(
        CONFIG_TOML.replace("[keeper]", "[keeper_typo]"), encoding="utf-8"
    )
    out = io.StringIO()
    code = main(["--config", str(partial)], http_get=lambda url: b"{}", out=out)
    assert code == 2
    printed = out.getvalue()
    assert "error" in printed.lower()
    assert "keeper" in printed
    assert "Traceback" not in printed
