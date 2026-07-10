"""Legacy P2PKH address derivation shared by the base58 UTXO chains (DASH, ZEC).

bitcoinlib has no network definitions for these chains, but BIP32 derivation is
network-independent — only the base58check version prefix differs (Zcash's is
two bytes, which bitcoinlib's single-byte ``prefix_address`` network field
couldn't express anyway). So the adapters derive the compressed pubkey with
bitcoinlib and encode the address here. The encoding is pinned to golden
vectors cross-checked against independent implementations (see test_dash.py /
test_zcash.py).
"""

from __future__ import annotations

from bitcoinlib.encoding import hash160, pubkeyhash_to_addr_base58
from bitcoinlib.keys import HDKey
from bitcoinlib.mnemonic import Mnemonic


def p2pkh_address(pubkey: bytes, prefix: bytes) -> str:
    """The base58check P2PKH address for a compressed pubkey, any prefix length."""
    return pubkeyhash_to_addr_base58(hash160(pubkey), prefix=prefix)


def derive_p2pkh_address(
    mnemonic: str, path: str, prefix: bytes, bip39_passphrase: str = ""
) -> str:
    """Derive ``path`` from the seed and return its P2PKH address."""
    seed = Mnemonic().to_seed(mnemonic, bip39_passphrase)
    key = HDKey.from_seed(seed).key_for_path(path)
    return p2pkh_address(key.public_byte, prefix)
