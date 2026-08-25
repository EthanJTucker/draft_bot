"""The operator's manual override sheet: one CSV he maintains by hand.

The model is right about most players and wrong about a few, and on draft
night there is no time to argue with it. This file is the lever: a signed
dollar ``delta`` on the model's own max bid, a hard ``avoid`` that forces
the bid to $0, a personal ``tier`` and ``note`` for the chips on screen,
and a ``target`` flag marking the players the draft is actually for.

One file, not two. The target list lives here as a column rather than in
a sheet of its own: the loader, the join key and the validation already
exist, and one file is one thing to keep straight under time pressure.

Everything here fails loud, following ``--budget`` (``dashboard/app.py``).
An override the operator believes he set but which quietly failed to
parse leaves exactly the wrong number the lever exists to remove, so a
malformed row stops startup rather than being skipped. The one deliberate
exception is a ``player_id`` the value sheet does not carry: an off-sheet
rookie is a legitimate target, so those are counted and reported, never
fatal.

The file itself is runtime input named by ``--overrides`` and is never
committed -- it is a record of one operator's opinions about one draft.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from draftbot.valuation import SheetRow

#: Every column the sheet may carry. A header outside this set is a typo
#: (``avoids``, ``player id``), and a typo'd header is invisible: the
#: column reads as data nobody consumes and the row silently does
#: nothing, which is the exact failure this module exists to prevent.
COLUMNS = ("player_id", "name", "tier", "target", "avoid", "delta", "note")

#: The only accepted spellings of the two flag columns. Deliberately
#: short: "y", "Y", "TRUE" and "x" all mean yes to somebody, and guessing
#: which is what turns a marked player into an unmarked one.
TRUE_WORDS = ("1", "true", "yes")
FALSE_WORDS = ("", "0", "false", "no")


@dataclass(frozen=True)
class PlayerOverride:
    """One row of the sheet: the operator's opinion about one player.

    ``delta`` is dollars added to (or taken off) the model's max bid,
    never an absolute price -- an absolute would go stale the moment the
    board moved, which is the whole reason the engine reprices. ``avoid``
    is the hard kill and outranks ``delta``. ``name``, ``tier`` and
    ``note`` never touch a number: the name is a cross-check against the
    sheet and the other two are display only.
    """

    player_id: str
    name: str | None
    tier: int | None
    target: bool
    avoid: bool
    delta: int
    note: str | None


def _bad(line: int, message: str) -> ValueError:
    """The refusal, built and returned for the caller to raise. Returning
    rather than raising keeps every refusal site a visible ``raise``, so
    no reader has to know that a helper call ends the function."""
    return ValueError(f"override CSV line {line}: {message}")


def _flag(line: int, column: str, raw: str) -> bool:
    text = raw.strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise _bad(
        line,
        f"{column}={raw.strip()!r} is not a yes/no flag; use 1 (or blank "
        "for no). A spelling this reader does not know would silently "
        f"leave {column} off",
    )


def _whole_number(line: int, column: str, raw: str, *, signed: bool) -> int | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as error:
        example = "-4 or 8" if signed else "2"
        raise _bad(
            line,
            f"{column}={text!r} is not a whole number (for example: "
            f"{example}); a dollar figure that does not parse is a "
            "tweak that never lands",
        ) from error


def _text(raw: str | None) -> str | None:
    text = (raw or "").strip()
    return text or None


def read_overrides_csv(path: str | Path) -> dict[str, PlayerOverride]:
    """Read the override sheet into {player_id: :class:`PlayerOverride`}.

    Raises ``ValueError``, naming the CSV line, on an unreadable header,
    an unknown column, a blank or duplicated ``player_id``, a flag
    spelling this reader does not know, a non-numeric ``tier`` or
    ``delta``, or ``avoid`` set together with a positive ``delta`` (two
    contradictory instructions about the same player -- guessing which
    one the operator meant is how a "never bid" becomes a raise).

    Does NOT check the ids against the value sheet; that needs the sheet
    and is :func:`reconcile_overrides`.
    """
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(
                "override CSV is empty; it needs at least a header row "
                f"({','.join(COLUMNS)})"
            )
        header = [name.strip() for name in reader.fieldnames]
        if "player_id" not in header:
            raise ValueError(
                f"override CSV header {header} has no player_id column; "
                "that is the only join key (names are never matched on)"
            )
        unknown = [name for name in header if name not in COLUMNS]
        if unknown:
            raise ValueError(
                f"override CSV header carries unknown column(s) {unknown}; "
                f"the schema is {','.join(COLUMNS)}. A misspelled header "
                "is a column the operator fills in that nothing reads"
            )
        return _read_rows(reader, header)


def _read_rows(
    reader: csv.DictReader, header: Sequence[str]
) -> dict[str, PlayerOverride]:
    book: dict[str, PlayerOverride] = {}
    for line, record in enumerate(reader, start=2):
        clean = {name: (record.get(name) or "") for name in header}
        player_id = clean["player_id"].strip()
        if not player_id:
            if not any(value.strip() for value in clean.values()):
                continue  # a blank spacer line, not a row
            raise _bad(line, "a row with no player_id cannot be joined to anything")
        if player_id in book:
            raise _bad(
                line,
                f"player_id {player_id!r} appears twice; one of them is a "
                "typo and guessing which would price the wrong opinion",
            )
        avoid = _flag(line, "avoid", clean.get("avoid", ""))
        delta = _whole_number(line, "delta", clean.get("delta", ""), signed=True) or 0
        if avoid and delta > 0:
            raise _bad(
                line,
                f"player_id {player_id!r} is marked avoid AND carries "
                f"delta +{delta}; those are opposite instructions about "
                "the same player",
            )
        book[player_id] = PlayerOverride(
            player_id=player_id,
            name=_text(clean.get("name")),
            tier=_whole_number(line, "tier", clean.get("tier", ""), signed=False),
            target=_flag(line, "target", clean.get("target", "")),
            avoid=avoid,
            delta=delta,
            note=_text(clean.get("note")),
        )
    return book


def reconcile_overrides(
    book: Mapping[str, PlayerOverride], rows: Sequence[SheetRow]
) -> list[str]:
    """Cross-check the book against the value sheet; return the ids the
    sheet does not carry, in sorted order.

    Raises ``ValueError`` when a row's ``name`` disagrees with the sheet's
    name for that ``player_id``. The join runs on the id alone -- names
    collide, change between seasons, and are unicode-fragile on a Windows
    console -- but a copy-paste that grabbed the right NAME and the wrong
    ID is silent under an id-only join and lands a confident tweak on
    somebody else's price. So a name that is present must agree.

    An unmatched id is NOT an error and is returned instead: a rookie the
    sheet never priced is a legitimate target, and refusing to start over
    one would cost the operator the whole lever.
    """
    by_id = {row.player_id: row for row in rows}
    unmatched = []
    for player_id in sorted(book):
        rule = book[player_id]
        row = by_id.get(player_id)
        if row is None:
            unmatched.append(player_id)
            continue
        if (
            rule.name is not None
            and rule.name.strip().casefold() != (row.name or "").strip().casefold()
        ):
            raise ValueError(
                f"override for player_id {player_id!r} names "
                f"{rule.name!r}, but the value sheet has that id as "
                f"{row.name!r}. One of the two was pasted from the wrong "
                "row; an id-only join would silently tweak the wrong "
                "player's price"
            )
    return unmatched
