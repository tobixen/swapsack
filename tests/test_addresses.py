"""Tests for the permissive destination-address sanity check."""

import pytest

from swapsack.addresses import validate_destination_address

# Real-format mainnet example addresses per chain. Every one of these carries a
# *valid* checksum — the validator now verifies them, so a made-up-looking
# address is no longer an acceptable fixture. Where no published address was at
# hand (DASH, ZEC) the vector is a synthetic hash160 encoded with the chain's
# real version byte, which is exactly as valid as a wallet-derived one.
VALID = {
    "BTC": [
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        # Taproot (BIP-350 test vector) — bech32*m*, not bech32: a witness
        # version >0 flips the checksum constant, so this is the case a
        # bech32-only verifier would wrongly reject.
        "bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297",
        "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
        "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
    ],
    "LTC": [
        "ltc1qjmxnz78nmc8nq77wuxh25n2es7rzm5c2rkk4wh",
        "LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL",
    ],
    "DOGE": ["DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L"],
    "DASH": [
        "XoRuP4dEMWNMY9ux4okvw6uZJ81axKDsgX",  # P2PKH ('X', version 0x4c)
        "7m6AYGt2Mt3h9fp1RxPofcjjfmrTFxcYko",  # P2SH ('7', version 0x10)
    ],
    "ZEC": [
        "t1VvJNmqYAPenpQanGwQzK68kutAnMCKHHW",  # transparent P2PKH ('t1', 0x1cb8)
        "t3LgmRCFBxtvfHUoudscbabPu4yshejUw9c",  # transparent P2SH ('t3', 0x1cbd)
    ],
    "BCH": [
        "bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a",
        "qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a",
        "1BpEi6DfDAUFd7GtittLSdBeYJvcoaVggu",
    ],
    "ETH": ["0x9858EfFD232B4033E47d90003D41EC34EcaEda94"],
    "TRON": ["TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"],
    # Arbitrum is EVM — the same address space and EIP-55 rule as ETH.
    "ARB": ["0x9858EfFD232B4033E47d90003D41EC34EcaEda94"],
    # Maya native chain (Cosmos-SDK bech32), for a CACAO destination.
    "MAYA": ["maya10sy79jhw9hw9sqwdgu0k4mw4qawzl7czewzs47"],
    # THORChain native chain (Cosmos-SDK bech32), for a RUNE destination.
    "THOR": ["thor1gm00vwsfcp48enm4uv9e5dhm37jtd0ye27wrx0"],
    # Cosmos Hub (THORChain names the chain GAIA, the HRP is 'cosmos').
    # Synthetic but checksum-valid, and accepted by a live THORChain quote.
    "GAIA": ["cosmos1tjjcfptfjmzm5zl9sr3r6n4dqmvqckl9a8nz3h"],
    # XRP Ledger classic address — the XRPL genesis ("blackhole") account.
    "XRP": ["rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"],
    # Cardano Shelley base address (header byte + payment hash + stake hash),
    # synthetic but checksum-valid; a live Maya quote built a memo for it.
    "ADA": [
        "addr1qxf4ppedwy4pylzff47uxhjxkz7fuchz3q3ewz89s80u8g05et60l9tnrg37f8"
        "9emhs5r6xxnq80tt6l0k5y0dlcqfcqtrftvm"
    ],
}


@pytest.mark.parametrize("chain", sorted(VALID))
def test_valid_addresses_accepted(chain):
    for addr in VALID[chain]:
        assert validate_destination_address(chain, addr) is None, addr


def test_empty_is_rejected():
    assert validate_destination_address("BTC", "") is not None


def test_wrong_network_rejected():
    # An ETH address is not a BTC address, and vice versa.
    assert validate_destination_address("BTC", VALID["ETH"][0]) is not None
    assert validate_destination_address("ETH", VALID["BTC"][1]) is not None
    # A DOGE address (starts D) is not LTC (starts L/M/3 or ltc1).
    assert validate_destination_address("LTC", VALID["DOGE"][0]) is not None
    # A DASH address (starts X) is not BTC, and a DOGE address is not DASH.
    assert validate_destination_address("BTC", VALID["DASH"][0]) is not None
    assert validate_destination_address("DASH", VALID["DOGE"][0]) is not None
    # A ZEC transparent address (starts t1/t3) is not BTC, and vice versa.
    assert validate_destination_address("BTC", VALID["ZEC"][0]) is not None
    assert validate_destination_address("ZEC", VALID["BTC"][1]) is not None
    # A Maya (maya1) address is not BTC, and a BTC address is not Maya.
    assert validate_destination_address("BTC", VALID["MAYA"][0]) is not None
    assert validate_destination_address("MAYA", VALID["BTC"][1]) is not None
    # A THOR (thor1) address is not a MAYA (maya1) one, and vice versa.
    assert validate_destination_address("MAYA", VALID["THOR"][0]) is not None
    assert validate_destination_address("THOR", VALID["MAYA"][0]) is not None


def test_truncated_rejected():
    assert validate_destination_address("ETH", "0xdead") is not None
    assert validate_destination_address("TRON", "Tshort") is not None


def test_unknown_chain_has_no_opinion():
    assert validate_destination_address("DOT", "1no_rule_yet") is None


# --- destination-only chains added for `--to` --------------------------------


def test_xrp_x_addresses_are_rejected():
    """An X-address is how the XRPL encodes a destination tag — and THORChain
    cannot parse one ("unable to parse address"), so accepting it here would
    quote a swap that can never be built. Classic 'r…' only."""
    assert (
        validate_destination_address(
            "XRP", "X7AcgcsBL6XDcUb289X4mJ8djcdyKaB5hJDWMArnXr61cqZ"
        )
        is not None
    )


def test_ada_byron_addresses_are_rejected():
    # Byron-era addresses are base58 over CBOR with a CRC32 — not base58check,
    # so nothing here can verify one. Shelley 'addr1…' bech32 only.
    for byron in [
        "Ae2tdPwUPEZFRbyhz3cpfC2CumGzNkFBN2L42rcUc2yjQpEkxDbkPodpMAi",
        "DdzFFzCqrhstpwKc8WMvPwwBb5oabcTW9zc5ykA37wJR1T7yUJRdE4Nk1Vjkfvod"
        "wRWnbhBhqGGKvLDgHm7HKQMhcqRhqEqbLRQLKzKQzc",
    ]:
        assert validate_destination_address("ADA", byron) is not None


def test_a_cosmos_hub_address_is_not_a_thorchain_one():
    # All three are plain bech32; only the HRP separates them, and the HRP is
    # itself checksummed — so this has to be caught, not merely likely to be.
    assert validate_destination_address("GAIA", VALID["THOR"][0]) is not None
    assert validate_destination_address("THOR", VALID["GAIA"][0]) is not None
    assert validate_destination_address("MAYA", VALID["GAIA"][0]) is not None


# --- checksums ---------------------------------------------------------------
#
# The shape rules above catch a wrong network or a truncated paste, but not the
# single-character typo — which is the mistake an address checksum exists to
# catch, and the one that is irreversible if it reaches a vault.


def test_every_shape_rule_declares_how_it_is_checksummed():
    """A `_RULES` entry must not silently pick up a checksum strategy.

    The dispatch used to end in a bare base58check fallback, so adding a shape
    rule for a chain that carries *no* checksum (Solana addresses are bare
    ed25519 pubkeys) would have rejected every valid address on it. Each chain
    now has to say which strategy applies, or say explicitly that none does.
    """
    from swapsack.addresses import (
        _BASE58CHECK_ALPHABET,
        _EVM_CHAINS,
        _NO_CHECKSUM,
        _PLAIN_BECH32_HRP,
        _RULES,
        _SEGWIT_HRP,
    )

    covered = (
        set(_SEGWIT_HRP)
        | set(_PLAIN_BECH32_HRP)
        | set(_EVM_CHAINS)
        | set(_BASE58CHECK_ALPHABET)
        | set(_NO_CHECKSUM)
    )
    assert set(_RULES) - covered == set()


def test_a_chain_with_no_checksum_strategy_is_accepted_not_rejected():
    # The failure this guards against: a shape rule alone must mean "no
    # checksum to verify", never "verify it as base58check and reject".
    from swapsack.addresses import _checksum_problem

    solana = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    assert _checksum_problem("NOSUCHCHAIN", solana) is None


def _corrupt(address: str) -> str:
    """Swap the last two differing characters.

    Same alphabet, same length, same prefix — so the shape rules still pass and
    only the checksum can tell the result apart from the real address.
    """
    chars = list(address)
    for i in range(len(chars) - 1, 0, -1):
        if chars[i] != chars[i - 1]:
            chars[i], chars[i - 1] = chars[i - 1], chars[i]
            return "".join(chars)
    raise AssertionError(f"cannot corrupt {address!r}")


@pytest.mark.parametrize("chain", sorted(VALID))
def test_a_typo_that_keeps_the_shape_is_rejected(chain):
    for addr in VALID[chain]:
        typo = _corrupt(addr)
        assert typo != addr
        assert validate_destination_address(chain, typo) is not None, typo


def test_zec_typo_is_caught_before_the_builder():
    # Regression: a one-character typo in a t1… recipient passed the regex-only
    # check and then died deep in the bespoke signer with base58's
    # `ValueError: Invalid checksum` — a traceback where the user should have
    # seen "that is not a valid address".
    problem = validate_destination_address("ZEC", _corrupt(VALID["ZEC"][0]))
    assert problem is not None and "checksum" in problem


def test_a_typo_inside_a_payment_uri_is_rejected_too():
    typo = _corrupt(VALID["BTC"][0])
    assert validate_destination_address("BTC", f"bitcoin:{typo}?amount=0.1") is not None


def test_eth_is_only_checksummed_when_the_casing_says_so():
    # EIP-55 is carried in the letter casing, so an all-lowercase (or
    # all-uppercase) address carries no checksum at all and must stay accepted —
    # rejecting those would refuse addresses THORChain itself accepts.
    addr = VALID["ETH"][0]
    assert validate_destination_address("ETH", addr.lower()) is None
    assert validate_destination_address("ETH", "0x" + addr[2:].upper()) is None
    # A mixed-case address, though, claims a checksum: honour it.
    flipped = addr.replace("Ef", "eF", 1)
    assert flipped != addr
    assert validate_destination_address("ETH", flipped) is not None


# --- BIP21-style payment URIs -----------------------------------------------
#
# Wallets and QR codes hand out `bitcoin:<address>?amount=…`, not a bare
# address, so pasting one into `send`/`--dest` must work rather than be
# rejected as malformed.


def test_parse_payment_uri_strips_the_scheme():
    from swapsack.addresses import parse_payment_uri

    address, params = parse_payment_uri("bitcoin:1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert address == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    assert params == {}


def test_parse_payment_uri_keeps_a_bare_address_untouched():
    from swapsack.addresses import parse_payment_uri

    for chain, addresses in VALID.items():
        for address in addresses:
            assert parse_payment_uri(address) == (address, {}), chain


def test_parse_payment_uri_extracts_query_parameters():
    from swapsack.addresses import parse_payment_uri

    address, params = parse_payment_uri(
        "bitcoin:bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        "?amount=0.015&label=Coffee%20Shop&message=hi"
    )
    assert address == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert params["amount"] == "0.015"
    assert params["label"] == "Coffee Shop"


def test_parse_payment_uri_accepts_the_other_chain_schemes():
    from swapsack.addresses import parse_payment_uri

    assert parse_payment_uri("litecoin:LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL")[0] == (
        "LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL"
    )
    assert parse_payment_uri("dash:Xwm4fpRLuvyQY4wgcbffLTMkVFAJKrxs8k")[0] == (
        "Xwm4fpRLuvyQY4wgcbffLTMkVFAJKrxs8k"
    )
    assert parse_payment_uri("zcash:t1PZ6UUwARqz7pjkFbQh3M8bQ4rr5nHkPqM")[0] == (
        "t1PZ6UUwARqz7pjkFbQh3M8bQ4rr5nHkPqM"
    )
    # EIP-681 may carry a chain id suffix: ethereum:0x…@1
    assert parse_payment_uri("ethereum:0x9858EfFD232B4033E47d90003D41EC34EcaEda94@1")[
        0
    ] == ("0x9858EfFD232B4033E47d90003D41EC34EcaEda94")


def test_parse_payment_uri_keeps_the_cashaddr_prefix():
    # 'bitcoincash:' is the canonical cashaddr prefix, not merely a URI scheme:
    # keep the address in the form the network expects, minus any query.
    from swapsack.addresses import parse_payment_uri

    address, params = parse_payment_uri(
        "bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a?amount=1"
    )
    assert address == "bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a"
    assert params["amount"] == "1"
    assert validate_destination_address("BCH", address) is None


def test_validate_accepts_a_payment_uri():
    # The gross-mistake guard must see through the scheme, not reject it.
    assert (
        validate_destination_address(
            "BTC", "bitcoin:1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
        )
        is None
    )
    assert (
        validate_destination_address(
            "BTC", "bitcoin:bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4?amount=0.1"
        )
        is None
    )


def test_validate_rejects_a_uri_for_the_wrong_chain():
    # Pasting a litecoin: URI into a BTC send is exactly the irreversible
    # mistake this guard exists for — the scheme names the chain, so use it.
    problem = validate_destination_address(
        "BTC", "litecoin:LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL"
    )
    assert problem is not None and "litecoin" in problem.lower()
    # ...and the address inside a matching scheme still has to be plausible.
    assert validate_destination_address("BTC", "bitcoin:0xdeadbeef") is not None
