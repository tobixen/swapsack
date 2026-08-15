"""EIP-55 address checksumming — a leaf module, deliberately.

`to_checksum_address` used to live in :mod:`swapsack.chains.eth`, which pulls
in eth-account, eth-abi and their dependency trees (~740 modules, ~0.4s) before
it can answer. The destination-address validator needs exactly this one
function and runs long before any of that is required, so it lives here with
only a keccak implementation behind it — the same reason
:mod:`swapsack.bech32` was split out of the Cosmos adapter.

`chains.eth` re-exports both names, so its callers are unchanged.
"""

from __future__ import annotations

from Crypto.Hash import keccak


def keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def to_checksum_address(addr: bytes | str) -> str:
    """EIP-55 checksum encoding of a 20-byte address (bytes or hex string)."""
    if isinstance(addr, str):
        if addr[:2].lower() == "0x":  # accept 0x or 0X (THORChain uppercases)
            addr = addr[2:]
        addr = bytes.fromhex(addr)
    lower = addr.hex()
    digest = keccak256(lower.encode()).hex()
    encoded = "".join(
        c.upper() if c.isalpha() and int(d, 16) >= 8 else c
        for c, d in zip(lower, digest, strict=False)
    )
    return "0x" + encoded
