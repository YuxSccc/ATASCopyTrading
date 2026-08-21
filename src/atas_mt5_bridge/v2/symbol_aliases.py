"""Stable source-instrument names used by the ATAS/MT5 router.

ATAS exposes a human readable security name and that name is connector- and
contract-dependent.  For example, the same gold contract has appeared as
``COMEX Gold Futures`` and ``COMEX Gold December 2026`` while the NASDAQ
future has appeared as ``NQ`` and ``E-mini NASDAQ-100 September 2026``.
Routing on those display strings makes an account or contract roll
look like a new, unknown instrument.

Only the two instruments currently configured for this bridge have a
cross-name alias family.  Unknown symbols are deliberately left unchanged so
an unsupported instrument cannot be guessed into a live pair.
"""

from __future__ import annotations

import re


_NON_ALPHANUMERIC = re.compile(r"[^A-Z0-9]+")


def canonical_source_symbol(value: object) -> str:
    """Return the stable logical name for a known ATAS source instrument.

    The returned value is suitable for the bridge's configured source symbol.
    Unknown values retain their trimmed spelling rather than being silently
    mapped to a known product.
    """

    text = str(value or "").strip()
    family = source_symbol_family(text)
    return family if family in {"GC", "NQ"} else text


def source_symbol_matches(configured: object, emitted: object) -> bool:
    """Whether two source names identify the same logical instrument."""

    left = str(configured or "").strip()
    right = str(emitted or "").strip()
    if not left or not right:
        return False
    if left.casefold() == right.casefold():
        return True
    left_family = source_symbol_family(left)
    right_family = source_symbol_family(right)
    return left_family in {"GC", "NQ"} and left_family == right_family


def source_symbol_family(value: object) -> str:
    """Return ``GC``/``NQ`` for known aliases, otherwise a safe key.

    Matching is intentionally token-aware for short futures codes.  A random
    symbol containing the letters ``GC`` or ``NQ`` must not be treated as a
    gold or NASDAQ future merely because of a substring match.
    """

    text = str(value or "").strip().upper()
    if not text:
        return ""
    compact = _NON_ALPHANUMERIC.sub("", text)

    # Gold futures: COMEX display names, contract codes such as GCZ6@COMEX,
    # and the short logical code GC.
    if "GOLD" in text or text == "GC" or compact.startswith("GC"):
        return "GC"

    # E-mini Nasdaq futures.  USTEC is deliberately *not* an alias here: it
    # is the MT5 target symbol, not the ATAS source future.  The explicit
    # NASDAQ check covers descriptive ATAS names without relying on a
    # particular contract month/year.
    if "NASDAQ" in text or text == "NQ" or compact.startswith("NQ"):
        return "NQ"

    return compact
