"""Destination-address validation.

A swap's payout goes to a user-supplied ``--dest`` on the destination chain, and
a plain ``send`` goes to a user-supplied recipient. A typo either place is
irreversible, so an address is checked twice before any network or keystore work
happens:

1. a *shape* rule per chain (prefix / alphabet / length), which catches the
   gross mistakes — empty, truncated, or an address for the wrong network; and
2. the address's own *checksum* — base58check, bech32/bech32m, cashaddr or
   EIP-55 — which is what catches the single-character typo the shape rules
   cannot see.

Only a checksum that positively fails is an error. Where an address carries no
checksum to verify (an all-lowercase EVM address, an unknown chain with no rule
yet) we still accept: the guiding rule is that a valid address is never
rejected. THORChain/Maya validate the address again when building the outbound,
so this is the early, friendly failure — not the last line of defence.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote

import base58

from swapsack import bech32

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


# BIP21-style payment URI schemes, mapped to the chain they name. Wallets and
# QR codes hand out `bitcoin:<address>?amount=…` rather than a bare address, so
# accept that spelling everywhere an address is taken.
_URI_SCHEMES: dict[str, str] = {
    "bitcoin": "BTC",
    "litecoin": "LTC",
    "dogecoin": "DOGE",
    "dash": "DASH",
    "zcash": "ZEC",
    "bitcoincash": "BCH",
    "ethereum": "ETH",  # EIP-681
    "tron": "TRON",
}
# 'bitcoincash:' doubles as the canonical cashaddr prefix, so the address is
# kept in its prefixed form; every other scheme is stripped.
_KEEP_SCHEME = {"bitcoincash"}


def parse_payment_uri(text: str) -> tuple[str, dict[str, str]]:
    """Split a payment URI into ``(address, query params)``.

    A bare address is returned unchanged with empty params, so callers can run
    everything through here. Unknown schemes are left alone rather than
    mangled — ``validate_destination_address`` then judges the result.
    """
    text = text.strip()
    scheme, sep, rest = text.partition(":")
    if not sep or scheme.lower() not in _URI_SCHEMES:
        return text, {}

    body, _, query = rest.partition("?")
    # EIP-681 chain-id / function suffixes (ethereum:0x…@1, …/transfer) are not
    # part of the address itself.
    body = body.split("@", 1)[0].split("/", 1)[0]
    address = unquote(body).strip()
    if scheme.lower() in _KEEP_SCHEME:
        address = f"{scheme.lower()}:{address}"
    return address, dict(parse_qsl(query))


def uri_chain(text: str) -> str | None:
    """The chain a payment URI names, or None if ``text`` carries no known scheme."""
    scheme = text.strip().partition(":")[0].lower()
    return _URI_SCHEMES.get(scheme)


# --- checksums --------------------------------------------------------------
#
# Dispatch is by address *form*, not purely by chain: BCH accepts both cashaddr
# and legacy base58, and they are checksummed by different algorithms. Nothing
# here imports a chain adapter at module scope — validating a recipient happens
# long before any chain library is needed, and should stay that cheap.

# Segwit chains, mapped to their bech32 human-readable part.
_SEGWIT_HRP: dict[str, str] = {"BTC": "bc", "LTC": "ltc"}
# Cosmos-SDK chains, mapped to their bech32 HRP. Always plain bech32: there is
# no witness version here to switch the checksum constant to bech32m.
_COSMOS_HRP: dict[str, str] = {"MAYA": "maya", "THOR": "thor"}

# cashaddr shares bech32's alphabet but uses a wider (40-bit) BCH code.
_CASHADDR_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CASHADDR_GENERATOR = (
    0x98F2BC8E61,
    0x79B76D99E2,
    0xF33E5FB3C4,
    0xAE2EABE2A8,
    0x1E4F43E470,
)


def _cashaddr_ok(address: str) -> bool:
    """Verify a BCH cashaddr checksum (with or without the ``bitcoincash:``)."""
    prefix, _, body = address.rpartition(":")
    # The prefix is part of the checksummed data even when it is left off the
    # written form, and 'bitcoincash' is the only one we accept.
    values = [ord(c) & 0x1F for c in prefix or "bitcoincash"] + [0]
    try:
        values += [_CASHADDR_CHARSET.index(c) for c in body]
    except ValueError:
        return False
    checksum = 1
    for value in values:
        top = checksum >> 35
        checksum = ((checksum & 0x07FFFFFFFF) << 5) ^ value
        for i, generator in enumerate(_CASHADDR_GENERATOR):
            if (top >> i) & 1:
                checksum ^= generator
    return checksum ^ 1 == 0


def _eip55_ok(address: str) -> bool:
    """Verify an EVM address's EIP-55 casing — if it claims one at all.

    The checksum lives in the *casing* of the hex letters, so an all-lowercase
    (or all-uppercase) address simply carries none and cannot be judged. Only a
    mixed-case address asserts a checksum, and then it must match exactly.
    """
    body = address[2:]
    if body == body.lower() or body == body.upper():
        return True
    from swapsack.chains.eth import to_checksum_address

    return to_checksum_address(address) == address


def _checksum_problem(chain: str, address: str) -> str | None:
    """Return a problem string if ``address`` fails its own checksum, else None.

    Only a checksum that positively fails is reported; an address form that
    carries no checksum yields None.
    """
    bad = (
        f"{address!r} has a bad checksum — a valid {chain} address would not, "
        "so this is a typo or a truncated paste"
    )

    hrp = _SEGWIT_HRP.get(chain)
    if hrp is not None and address.startswith(f"{hrp}1"):
        # segwit_ok picks bech32 vs bech32m from the witness version, so this
        # covers both v0 (bc1q…) and taproot (bc1p…) without a special case.
        return None if bech32.segwit_ok(address, hrp) else bad

    hrp = _COSMOS_HRP.get(chain)
    if hrp is not None:
        try:
            bech32.bech32_decode(address)
        except ValueError:
            return bad
        return None

    # BCH legacy addresses are base58check like BTC's; anything else on that
    # chain is a cashaddr, prefixed or bare.
    if chain == "BCH" and not address.startswith(("1", "3")):
        return None if _cashaddr_ok(address) else bad

    if chain == "ETH":
        return None if _eip55_ok(address) else bad

    # What is left is base58check: legacy BTC/LTC/DOGE, DASH, ZEC (a two-byte
    # version prefix, which base58check does not care about) and TRON.
    try:
        base58.b58decode_check(address)
    except ValueError:
        return bad
    return None


def validate_destination_address(chain: str, address: str) -> str | None:
    """Return a problem string if ``address`` is implausible for ``chain``, else None.

    ``chain`` is the THORChain chain prefix (``BTC``/``ETH``/``LTC``/…). An
    unknown chain yields no opinion (returns None) so new chains are not blocked
    before a rule exists. A BIP21-style payment URI is accepted for its own
    chain and rejected for any other — the scheme states which network the payee
    meant, and honouring that catches a cross-chain paste before it is spent.
    """
    if not address:
        return "destination address is empty"
    named = uri_chain(address)
    if named is not None and named != chain:
        scheme = address.partition(":")[0]
        return f"{scheme!r} URI is a {named} address, but this is a {chain} spend"
    address, _ = parse_payment_uri(address)
    if not address:
        return "destination address is empty"
    rule = _RULES.get(chain)
    if rule is None:
        return None
    if not rule.match(address):
        return f"{address!r} does not look like a valid {chain} address"
    return _checksum_problem(chain, address)
