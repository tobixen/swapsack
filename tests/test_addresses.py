"""Tests for the permissive destination-address sanity check."""

import pytest

from swapsack.addresses import validate_destination_address

# Real-format mainnet example addresses per chain.
VALID = {
    "BTC": [
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
        "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
    ],
    "LTC": [
        "ltc1qjmxnz78nmc8nq77wuxh25n2es7rzm5c2rkk4wh",
        "LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL",
    ],
    "DOGE": ["DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L"],
    "DASH": [
        "Xwm4fpRLuvyQY4wgcbffLTMkVFAJKrxs8k",  # P2PKH ('X')
        "7gnwGHt17heGpG9Crfeh4KGpYNFugPhJdh",  # P2SH ('7')
    ],
    "ZEC": [
        "t1PZ6UUwARqz7pjkFbQh3M8bQ4rr5nHkPqM",  # transparent P2PKH ('t1')
        "t3Vz22vK5z2LcKEdg16Yv4FFneEL1zg9ojd",  # transparent P2SH ('t3')
    ],
    "BCH": [
        "bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a",
        "qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a",
        "1BpEi6DfDAUFd7GtittLSdBeYJvcoaVggu",
    ],
    "ETH": ["0x9858EfFD232B4033E47d90003D41EC34EcaEda94"],
    "TRON": ["TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"],
    # Maya native chain (Cosmos-SDK bech32), for a CACAO destination.
    "MAYA": ["maya10sy79jhw9hw9sqwdgu0k4mw4qawzl7czewzs47"],
    # THORChain native chain (Cosmos-SDK bech32), for a RUNE destination.
    "THOR": ["thor1gm00vwsfcp48enm4uv9e5dhm37jtd0ye27wrx0"],
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
    assert validate_destination_address("XRP", "rno_rule_yet") is None


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
