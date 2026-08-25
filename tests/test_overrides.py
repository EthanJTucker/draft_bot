"""The operator's override CSV: parsing, and every way it refuses.

The file is runtime operator input (a ``--overrides`` path), so every
fixture here writes its own CSV into ``tmp_path``; nothing is committed.

The whole module is about failing loud. An override is the only lever the
operator has when the model is wrong about a player, and a row that
silently fails to parse leaves exactly the wrong number the lever exists
to remove -- so a malformed row stops startup instead of being skipped,
mirroring what ``--budget`` does in ``dashboard/app.py``.
"""

from __future__ import annotations

import pytest

from draftbot.overrides import PlayerOverride, read_overrides_csv, reconcile_overrides

from .helpers_engine import sheet_row


def write_csv(tmp_path, text: str):
    """One override CSV on disk, dedented by the caller."""
    path = tmp_path / "overrides.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_column_parses_into_one_record(tmp_path):
    """The documented schema, one row carrying all of it: the join key,
    the display-only name and tier, both booleans, a signed delta, and a
    free-text note."""
    path = write_csv(
        tmp_path,
        "player_id,name,tier,target,avoid,delta,note\n"
        "rb40,Bijan Robinson,1,1,,8,volume bump\n",
    )

    book = read_overrides_csv(path)

    assert book == {
        "rb40": PlayerOverride(
            player_id="rb40",
            name="Bijan Robinson",
            tier=1,
            target=True,
            avoid=False,
            delta=8,
            note="volume bump",
        )
    }


def test_blank_optional_columns_and_a_header_only_file_are_both_fine(tmp_path):
    """The ordinary sheet: mostly blanks. A header with no rows is an
    operator who has not decided anything yet, not an error."""
    assert not read_overrides_csv(write_csv(tmp_path, "player_id,delta\n"))

    book = read_overrides_csv(
        write_csv(tmp_path, "player_id,name,tier,target,avoid,delta,note\nwr9,,,,,,\n")
    )
    assert book["wr9"] == PlayerOverride(
        player_id="wr9",
        name=None,
        tier=None,
        target=False,
        avoid=False,
        delta=0,
        note=None,
    )


def test_a_negative_delta_is_kept_signed(tmp_path):
    """A discount is the same lever pointed the other way; the minus sign
    must survive the parse rather than being read as magnitude."""
    book = read_overrides_csv(write_csv(tmp_path, "player_id,delta\nte3,-6\n"))
    assert book["te3"].delta == -6


def test_avoid_with_a_negative_delta_is_allowed(tmp_path):
    """Only a POSITIVE delta contradicts avoid. Marking a player never-bid
    while also discounting him is redundant, not contradictory, and
    refusing it would reject a harmless sheet at startup."""
    book = read_overrides_csv(write_csv(tmp_path, "player_id,avoid,delta\nk1,1,-3\n"))
    assert (book["k1"].avoid, book["k1"].delta) == (True, -3)


def test_avoid_with_a_positive_delta_is_refused(tmp_path):
    """Two opposite instructions about one player. Either answer the
    reader could pick is the operator's other instruction ignored."""
    path = write_csv(tmp_path, "player_id,avoid,delta\nrb2,1,5\n")
    with pytest.raises(ValueError, match="line 2.*avoid AND carries delta"):
        read_overrides_csv(path)


def test_a_duplicate_player_id_is_refused(tmp_path):
    """Last-wins and first-wins are both plausible, so neither is safe."""
    path = write_csv(tmp_path, "player_id,delta\nrb2,5\nwr1,2\nrb2,-9\n")
    with pytest.raises(ValueError, match="line 4.*'rb2' appears twice"):
        read_overrides_csv(path)


def test_an_unparseable_delta_is_refused_not_dropped(tmp_path):
    """The trap: a swallowed delta reads on screen as the un-tweaked
    model number, which is indistinguishable from the file never loading."""
    path = write_csv(tmp_path, "player_id,delta\nrb2,+ten\n")
    with pytest.raises(
        ValueError, match=r"line 2.*delta='\+ten' is not a whole number"
    ):
        read_overrides_csv(path)


def test_a_flag_spelling_this_reader_does_not_know_is_refused(tmp_path):
    """``y`` means yes to a human. Reading it as no would leave a player
    the operator marked never-bid fully biddable, silently."""
    path = write_csv(tmp_path, "player_id,avoid\nrb2,y\n")
    with pytest.raises(ValueError, match="line 2.*avoid='y' is not a yes/no flag"):
        read_overrides_csv(path)


def test_a_misspelled_header_is_refused(tmp_path):
    """A column nothing reads is a column the operator fills in for
    nothing, and it looks identical to one that works."""
    path = write_csv(tmp_path, "player_id,avoids\nrb2,1\n")
    with pytest.raises(ValueError, match=r"unknown column\(s\) \['avoids'\]"):
        read_overrides_csv(path)


def test_a_file_with_no_player_id_column_is_refused(tmp_path):
    """Names are never matched on, so a sheet keyed only by name has
    nothing to join with and cannot be silently half-applied."""
    path = write_csv(tmp_path, "name,delta\nBijan Robinson,8\n")
    with pytest.raises(ValueError, match="no player_id column"):
        read_overrides_csv(path)


def test_a_row_with_no_player_id_is_refused_but_a_blank_line_is_not(tmp_path):
    """A trailing newline from a spreadsheet export is not an error; a
    row carrying an opinion and no join key is."""
    assert set(
        read_overrides_csv(write_csv(tmp_path, "player_id,delta\nrb2,5\n\n"))
    ) == {"rb2"}

    path = write_csv(tmp_path, "player_id,delta\n,5\n")
    with pytest.raises(ValueError, match="line 2.*no player_id"):
        read_overrides_csv(path)


def test_reconcile_returns_off_sheet_ids_instead_of_refusing(tmp_path):
    """An unmatched id is a rookie the sheet never priced -- a legitimate
    target. Reported, never fatal: refusing here costs the whole lever."""
    book = read_overrides_csv(
        write_csv(tmp_path, "player_id,delta\nrb40,8\nrookie99,4\nzz1,1\n")
    )
    rows = [sheet_row(1, "rb40", "RB", 40.0)]

    assert reconcile_overrides(book, rows) == ["rookie99", "zz1"]


def test_a_name_that_disagrees_with_the_sheet_is_refused(tmp_path):
    """The silent-under-an-id-join mistake: the right name pasted next to
    the wrong id lands a confident tweak on somebody else's price."""
    book = read_overrides_csv(
        write_csv(tmp_path, "player_id,name,delta\nrb40,Saquon Barkley,8\n")
    )
    rows = [sheet_row(1, "rb40", "RB", 40.0, name="Bijan Robinson")]

    with pytest.raises(ValueError, match="names 'Saquon Barkley'.*'Bijan Robinson'"):
        reconcile_overrides(book, rows)


def test_a_matching_name_differing_only_in_case_and_padding_is_accepted(tmp_path):
    """The cross-check must catch a wrong player, not a spreadsheet's
    capitalisation -- a false alarm here stops startup for nothing."""
    book = read_overrides_csv(
        write_csv(tmp_path, "player_id,name,delta\nrb40,  bijan ROBINSON ,8\n")
    )
    rows = [sheet_row(1, "rb40", "RB", 40.0, name="Bijan Robinson")]

    assert not reconcile_overrides(book, rows)
