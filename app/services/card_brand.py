"""Card brand detection from BIN and Luhn validation."""
from __future__ import annotations


def detect_brand(number: str) -> str:
    """Return brand string from card number (digits only)."""
    n = "".join(c for c in number if c.isdigit())
    if not n:
        return "unknown"

    # Amex: 34, 37
    if n[:2] in ("34", "37"):
        return "amex"

    # Hipercard: 606282, 3841xx
    if n[:6] == "606282" or n[:4] == "3841":
        return "hipercard"

    # Elo — known BIN ranges (major prefixes)
    _ELO_PREFIXES = (
        "636368", "438935", "504175", "451416", "636297",
        "5067", "4576", "4011", "506699", "5090",
    )
    if any(n.startswith(p) for p in _ELO_PREFIXES):
        return "elo"

    # Mastercard: 51-55 or 2221-2720
    if len(n) >= 2:
        prefix2 = int(n[:2])
        if 51 <= prefix2 <= 55:
            return "mastercard"
    if len(n) >= 4:
        prefix4 = int(n[:4])
        if 2221 <= prefix4 <= 2720:
            return "mastercard"

    # Visa: starts with 4
    if n.startswith("4"):
        return "visa"

    return "unknown"


def luhn_valid(number: str) -> bool:
    """Return True if the card number passes the Luhn check."""
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
