"""Lightweight destination-address sanity checks.

A swap's payout goes to a user-supplied ``--dest`` on the destination chain. A
typo there is irreversible, so we guard against *gross* mistakes (empty, wrong
network, truncated) before quoting/broadcasting. These are deliberately NOT full
checksum validation — a passing address can still be wrong — and THORChain
validates the address again when it builds the outbound. Rules are kept
permissive so a valid address is never rejected; when in doubt we accept.
"""

from __future__ import annotations

import re

# Base58 alphabet (no 0, O, I, l) shared by legacy BTC-family and TRON addresses.
_B58 = r"[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]"
# bech32 / cashaddr bodies are lowercase alphanumeric.
_B32 = r"[a-z0-9]"

# Per-chain acceptance patterns, keyed by the THORChain chain prefix.
_RULES: dict[str, re.Pattern[str]] = {
    "BTC": re.compile(rf"^(bc1{_B32}{{11,71}}|[13]{_B58}{{24,34}})$"),
    "LTC": re.compile(rf"^(ltc1{_B32}{{11,71}}|[LM3]{_B58}{{24,34}})$"),
    "DOGE": re.compile(rf"^[DA9]{_B58}{{24,34}}$"),
    "BCH": re.compile(rf"^(bitcoincash:)?([qp]{_B32}{{40,60}}|[13]{_B58}{{24,34}})$"),
    # Dash: legacy base58 only (no segwit) — P2PKH 'X', P2SH '7'.
    "DASH": re.compile(rf"^[X7]{_B58}{{24,34}}$"),
    # Zcash: transparent base58 addresses only (Maya has no shielded support) —
    # P2PKH 't1', P2SH 't3'. Two-char prefix then base58 (35 chars total).
    "ZEC": re.compile(rf"^t[13]{_B58}{{32,34}}$"),
    "ETH": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "TRON": re.compile(rf"^T{_B58}{{33}}$"),
    # Maya native chain (Cosmos-SDK bech32, 'maya' HRP) — for a CACAO payout.
    "MAYA": re.compile(rf"^maya1{_B32}{{37,58}}$"),
    # THORChain native chain (Cosmos-SDK bech32, 'thor' HRP) — for a RUNE payout.
    "THOR": re.compile(rf"^thor1{_B32}{{37,58}}$"),
}


def validate_destination_address(chain: str, address: str) -> str | None:
    """Return a problem string if ``address`` is implausible for ``chain``, else None.

    ``chain`` is the THORChain chain prefix (``BTC``/``ETH``/``LTC``/…). An
    unknown chain yields no opinion (returns None) so new chains are not blocked
    before a rule exists.
    """
    if not address:
        return "destination address is empty"
    rule = _RULES.get(chain)
    if rule is None:
        return None
    if not rule.match(address):
        return f"{address!r} does not look like a valid {chain} address"
    return None
