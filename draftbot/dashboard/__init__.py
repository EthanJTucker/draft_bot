"""Draft-night dashboard: a FastAPI ``/state`` endpoint, one static
auto-refreshing page, and the poll loop that feeds them.

The dashboard is a renderer over existing seams: a source's
``poll() -> SourceTick`` feeds the tracker, the tracker's board feeds the
engine's ``analyze_player``, and this package only assembles what those
return into one JSON snapshot per poll cycle. Run it with
``python -m draftbot.dashboard`` (see ``app.main`` for the CLI).
"""
