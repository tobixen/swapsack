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
    # Arbitrum is an EVM L2 — same 20-byte address space and EIP-55 as ETH.
    "ARB": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    # Avalanche has three chains and only the C-Chain is EVM (and pooled). The
    # X-/P-Chain's own 'X-avax1…'/'P-avax1…' bech32 form is therefore excluded
    # deliberately: a valid Avalanche address that a payout could never credit.
    "AVAX": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "TRON": re.compile(rf"^T{_B58}{{33}}$"),
    # Maya native chain (Cosmos-SDK bech32, 'maya' HRP) — for a CACAO payout.
    "MAYA": re.compile(rf"^maya1{_B32}{{37,58}}$"),
    # THORChain native chain (Cosmos-SDK bech32, 'thor' HRP) — for a RUNE payout.
    "THOR": re.compile(rf"^thor1{_B32}{{37,58}}$"),
    # Cosmos Hub — THORChain calls the chain GAIA, the address HRP is 'cosmos'.
    "GAIA": re.compile(rf"^cosmos1{_B32}{{37,58}}$"),
    # XRP Ledger classic addresses. Same alphabet *set* as BTC base58 (the XRP
    # ordering differs, which only matters when decoding), but note what is NOT
    # here: an 'X…' X-address, which is how the XRPL encodes a destination tag.
    # THORChain rejects those outright ("unable to parse address"), so a tag
    # cannot be expressed at all — see the warning in cli._resolve_destination.
    "XRP": re.compile(rf"^r{_B58}{{24,34}}$"),
    # Cardano Shelley addresses: bech32 with an 'addr' HRP, and far longer than
    # BIP-173's 90-char cap (Cardano deliberately ignores it) — a base address
    # is 103 chars. Byron-era 'Ae2…'/'DdzFF…' addresses are deliberately absent:
    # they are base58 over CBOR with a CRC32, not base58check, so this module
    # could not verify one, and they are legacy in every current wallet.
    "ADA": re.compile(rf"^addr1{_B32}{{50,110}}$"),
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
# Chains whose addresses are *plain* bech32 — Cosmos-SDK accounts and Cardano's
# Shelley addresses. No witness version, so nothing ever switches the checksum
# constant to bech32m; the HRP is the only thing that differs.
_PLAIN_BECH32_HRP: dict[str, str] = {
    "MAYA": "maya",
    "THOR": "thor",
    "GAIA": "cosmos",
    "ADA": "addr",
}
# EVM chains: one address space, one EIP-55 casing rule. Which is also the
# hazard — an address alone never says which of these it is meant for, so the
# CLI warns when a payout lands anywhere but Ethereum mainnet.
_EVM_CHAINS = frozenset({"ETH", "ARB", "AVAX"})

# Chains whose addresses are base58check, mapped to the alphabet they use —
# the XRPL permutes the same 58 characters, so only decoding tells them apart.
# Listed explicitly rather than reached by a catch-all: a chain that lands here
# by accident would have *every* valid address rejected (see _NO_CHECKSUM).
_BASE58CHECK_ALPHABET: dict[str, bytes] = {
    "BTC": base58.BITCOIN_ALPHABET,
    "LTC": base58.BITCOIN_ALPHABET,
    "DOGE": base58.BITCOIN_ALPHABET,
    # BCH's *legacy* '1…'/'3…' form; a cashaddr never reaches here.
    "BCH": base58.BITCOIN_ALPHABET,
    "DASH": base58.BITCOIN_ALPHABET,
    "ZEC": base58.BITCOIN_ALPHABET,
    "TRON": base58.BITCOIN_ALPHABET,
    "XRP": base58.XRP_ALPHABET,
}

# Chains whose address format carries no checksum at all, so there is nothing
# to verify and a shape rule is genuinely the whole guard. Declared rather than
# left implicit, so that "no branch" reads as a decision instead of an
# oversight — and so the invariant test can tell the two apart. Solana belongs
# here if its pool ever unhalts: an address is a bare 32-byte ed25519 pubkey.
_NO_CHECKSUM: frozenset[str] = frozenset()

# cashaddr shares bech32's alphabet but uses a wider (40-bit) BCH code.
_CASHADDR_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CASHADDR_GENERATOR = (
    0x98F2BC8E61,
    0x79B76D99E2,
    0xF33E5FB3C4,
    0xAE2EABE2A8,
    0x1E4F43E470,
)


def is_evm_chain(chain: str) -> bool:
    """Whether ``chain`` uses the EVM 20-byte address space.

    Public because the *sharing* of that address space is a user-facing hazard,
    not just a checksum detail: an address cannot say which EVM chain it is
    meant for, so the CLI warns before paying one that is not Ethereum mainnet.
    """
    return chain in _EVM_CHAINS


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
    from swapsack.evm import to_checksum_address

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

    hrp = _PLAIN_BECH32_HRP.get(chain)
    if hrp is not None:
        try:
            decoded_hrp, _ = bech32.bech32_decode(address)
        except ValueError:
            return bad
        # The HRP is checksummed, so a wrong one cannot survive the polymod —
        # but it can be an HRP we never asked about, so say so explicitly.
        return None if decoded_hrp == hrp else bad

    # BCH legacy addresses are base58check like BTC's; anything else on that
    # chain is a cashaddr, prefixed or bare.
    if chain == "BCH" and not address.startswith(("1", "3")):
        return None if _cashaddr_ok(address) else bad

    if chain in _EVM_CHAINS:
        return None if _eip55_ok(address) else bad

    # base58check: legacy BTC/LTC/DOGE, DASH, ZEC (a two-byte version prefix,
    # which base58check does not care about), TRON and XRP.
    alphabet = _BASE58CHECK_ALPHABET.get(chain)
    if alphabet is None:
        # No strategy for this chain, so there is nothing to verify. Accepting
        # is the only safe default: falling through to base58check here would
        # reject every valid address on a chain that carries no checksum.
        return None
    try:
        base58.b58decode_check(address, alphabet=alphabet)
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
