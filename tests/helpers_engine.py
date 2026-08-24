"""Shared hand-fixture builders for the draft-engine test modules.

Plain builders imported explicitly (the ``helpers_valuation`` pattern):
a minimal value-sheet row and a board built the way the tracker would
build it, with per-team spend and open slots DERIVED from the sales so a
fixture can never drift out of internal consistency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from draftbot.tracker import BoardState, Sale, TeamState
from draftbot.valuation import SheetRow


def sheet_row(
    rank: int,
    player_id: str,
    position: str,
    worth: float,
    premium: float = 0.0,
    name: str | None = None,
) -> SheetRow:
    # pylint: disable=too-many-arguments,too-many-positional-arguments  # fixture
    # builder mirroring the sheet row's independent fields one-to-one.
    """One value-sheet row: value = worth + keeper premium, like the real
    sheet; ADP mirrors the rank (tests never price off it)."""
    return SheetRow(
        rank=rank,
        player_id=player_id,
        name=name or player_id,
        position=position,
        adp=float(rank),
        points=None,
        worth=float(worth),
        room_price=float(worth),
        price_source="band",
        keeper_premium=float(premium),
        value=round(float(worth) + float(premium), 10),
    )


def make_board(
    budgets: Mapping[int, int],
    sales: Sequence[Sale] = (),
    *,
    drafted_slots: int = 15,
    keeper_counts: Mapping[int, int] | None = None,
    off_model: Sequence[str] = (),
) -> BoardState:
    """A consistent BoardState: spend, purchase counts, and open slots per
    team all derive from the sales list, exactly as the tracker computes
    them, so a test cannot claim a sale without debiting the buyer."""
    keeper_counts = dict(keeper_counts or {})
    teams = []
    for slot in sorted(budgets):
        purchases = [sale for sale in sales if sale.draft_slot == slot]
        keepers = keeper_counts.get(slot, 0)
        teams.append(
            TeamState(
                slot=slot,
                roster_id=slot,
                budget=budgets[slot],
                budget_is_default=False,
                spent=sum(sale.amount or 0 for sale in purchases),
                keeper_count=keepers,
                purchase_count=len(purchases),
                open_slots=max(0, drafted_slots - keepers - len(purchases)),
                needs={},
            )
        )
    return BoardState(
        status="drafting",
        paused=False,
        teams=tuple(teams),
        sales=tuple(sales),
        off_model_player_ids=tuple(off_model),
    )
