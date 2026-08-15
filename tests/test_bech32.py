"""Tests for the shared bech32 / bech32m codec.

The Cosmos side (encode/decode round-trip against a real on-chain maya1 address)
is covered in ``test_maya.py``; what is tested here is the segwit side, where
the checksum constant depends on the witness version — the case that makes the
difference between accepting taproot and silently accepting a corrupted address.
"""

import pytest

from swapsack.bech32 import bech32_decode, segwit_ok, split

# BIP-173 / BIP-350 reference vectors.
P2WPKH = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
P2WSH = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
TAPROOT = "bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297"


@pytest.mark.parametrize("address", [P2WPKH, P2WSH, TAPROOT])
def test_segwit_ok_accepts_v0_and_v1(address):
    assert segwit_ok(address, "bc")


def test_segwit_ok_requires_the_expected_hrp():
    # The HRP is part of the checksummed data, so a testnet address is not
    # merely mis-prefixed — it is a different string entirely.
    assert not segwit_ok(P2WPKH, "ltc")
    assert not segwit_ok("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "bc")


@pytest.mark.parametrize("address", [P2WPKH, TAPROOT])
def test_segwit_ok_rejects_a_single_character_typo(address):
    typo = address[:-1] + ("q" if address[-1] != "q" else "p")
    assert not segwit_ok(typo, "bc")


def test_bech32_decode_refuses_a_bech32m_string():
    # Taproot's polymod is "valid" — under the *other* constant. Accepting it in
    # the plain-bech32 decoder would mean decoding a segwit v1 program as if it
    # were a Cosmos account hash.
    with pytest.raises(ValueError, match="bech32m"):
        bech32_decode(TAPROOT)


def test_split_reports_which_variant_matched():
    from swapsack.bech32 import BECH32, BECH32M

    assert split(P2WPKH)[2] == BECH32
    assert split(TAPROOT)[2] == BECH32M
    assert split("thor1gm00vwsfcp48enm4uv9e5dhm37jtd0ye27wrx0")[2] == BECH32


def test_split_rejects_a_string_that_is_not_bech32_at_all():
    for bad in ["", "1", "nodelimiter", "bc1", "bc1qqqqq"]:
        with pytest.raises(ValueError):
            split(bad)
