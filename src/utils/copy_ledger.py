"""On-disk record of manual existing-position copies.

The startup catch-up bought the same target position again on every restart
because nothing remembered that it already had: the only guard was "does the
follower currently hold this symbol", and the exchange needs a moment to
report a position that was just opened. Two confirmations a minute apart (or
a restart in between) therefore both passed the check and both bought.

This ledger closes that window. It records the last successful manual copy per
(symbol, side) with a timestamp, keeps the record across process restarts, and
lets the caller refuse a repeat inside a configurable window. It deliberately
does not replace the "already holds it" check -- the two answer different
questions: the ledger says "we just did this", the position check says "the
venue says we hold it".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger

LEDGER_VERSION = 1
# Entries older than this are only noise; the guard window is minutes.
MAX_AGE_SECONDS = 7 * 24 * 3600
MAX_ENTRIES = 500

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "copyloss_ledger.json"


def ledger_path() -> Path:
    """Path of the ledger file (``COPY_LEDGER_PATH`` overrides the default)."""
    override = os.getenv("COPY_LEDGER_PATH", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_PATH


def _key(symbol: str, side: str) -> str:
    return f"{symbol.upper()}|{side.upper()}"


def load() -> dict[str, dict[str, Any]]:
    """Read the ledger, returning an empty map for a missing/corrupt file."""
    path = ledger_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # A corrupt ledger must not block trading; it only weakens the guard.
        logger.warning(f"Cannot read copy ledger {path}: {exc} (ignoring it)")
        return {}

    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {
        str(key): value
        for key, value in entries.items()
        if isinstance(value, dict) and isinstance(value.get("at"), (int, float))
    }


def _save(entries: dict[str, dict[str, Any]]) -> None:
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"version": LEDGER_VERSION, "entries": entries},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        # Losing the ledger must never fail an order that already went out,
        # but it does mean the duplicate guard will not survive a restart.
        logger.error(
            f"Cannot write copy ledger {path}: {exc} "
            "(duplicate guard will not persist across restarts)"
        )


def record(
    symbol: str,
    side: str,
    notional: float,
    order_id: Optional[str] = None,
    simulated: bool = False,
) -> None:
    """Remember that a manual copy of ``symbol``/``side`` just went through."""
    entries = load()
    entries[_key(symbol, side)] = {
        "symbol": symbol,
        "side": side.upper(),
        "at": time.time(),
        "notional": float(notional),
        "order_id": str(order_id) if order_id else None,
        "simulated": bool(simulated),
    }

    cutoff = time.time() - MAX_AGE_SECONDS
    entries = {k: v for k, v in entries.items() if v["at"] >= cutoff}
    if len(entries) > MAX_ENTRIES:
        newest = sorted(entries.items(), key=lambda item: item[1]["at"], reverse=True)
        entries = dict(newest[:MAX_ENTRIES])
    _save(entries)


def recent(
    symbol: str,
    side: str,
    window_minutes: float,
    simulated: bool = False,
) -> Optional[dict[str, Any]]:
    """Last copy of this symbol/side inside the window, or ``None``.

    Simulated runs are kept apart from live ones so a dry run cannot arm the
    guard for a real order, and vice versa.
    """
    if window_minutes <= 0:
        return None
    entry = load().get(_key(symbol, side))
    if not entry:
        return None
    if bool(entry.get("simulated", False)) != bool(simulated):
        return None
    age_seconds = time.time() - float(entry["at"])
    if age_seconds > window_minutes * 60:
        return None
    return {**entry, "age_minutes": age_seconds / 60}
