"""Value-sheet inputs for the dashboard.

Two sources, one output shape (:class:`~draftbot.valuation.SheetRow`):

- :func:`read_sheet_csv` reads the CSV that ``python -m draftbot.valuesheet``
  emits — the real sheet for draft night.
- :func:`replay_sheet` derives a self-contained demo sheet from a replay
  fixture's own picks: each player's worth IS the price the room actually
  paid. Honest by construction (no fabricated projections) and fully
  offline, at the cost that price-minus-value profit reads $0 on every
  replayed lot; pass a real sheet CSV to see profit vary.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from draftbot.models import parse_picks
from draftbot.valuation import SheetRow


def read_sheet_csv(path: str | Path) -> list[SheetRow]:
    """Read the value-sheet CSV that ``draftbot.valuesheet`` writes.

    Row order is preserved; blank ADP/points cells come back as None
    (matching the writer, which emits "" for None — 0.0 would be a real,
    different number).
    """
    rows = []
    with open(path, encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            rows.append(
                SheetRow(
                    rank=int(record["rank"]),
                    player_id=record["player_id"],
                    name=record["name"],
                    position=record["position"],
                    adp=float(record["adp"]) if record["adp"] else None,
                    points=float(record["points"]) if record["points"] else None,
                    worth=float(record["worth"]),
                    room_price=float(record["room_price"]),
                    price_source=record["price_source"],
                    keeper_premium=float(record["keeper_premium"]),
                    value=float(record["value"]),
                )
            )
    return rows


def replay_sheet(raw_picks: Sequence[dict]) -> list[SheetRow]:
    """A demo sheet priced from the replay itself: worth = the winning bid.

    Ranked richest first with player-id tie-breaks (deterministic), keeper
    premium zero (so value = worth), and ``price_source`` labeled
    ``"replay"`` so nothing downstream can mistake it for a fitted model.
    Picks with no parseable amount or no position are skipped; their sales
    then surface through the tracker's off-model flagging, exactly like a
    live sale the sheet does not price.
    """
    priced = [
        pick
        for pick in parse_picks(list(raw_picks))
        if pick.amount is not None and pick.metadata.get("position")
    ]
    priced.sort(key=lambda pick: (-pick.amount, pick.player_id))
    rows = []
    for rank, pick in enumerate(priced, start=1):
        parts = (pick.metadata.get("first_name"), pick.metadata.get("last_name"))
        name = " ".join(part for part in parts if part) or pick.player_id
        worth = float(pick.amount)
        rows.append(
            SheetRow(
                rank=rank,
                player_id=pick.player_id,
                name=name,
                position=str(pick.metadata["position"]),
                adp=None,
                points=None,
                worth=worth,
                room_price=worth,
                price_source="replay",
                keeper_premium=0.0,
                value=worth,
            )
        )
    return rows
