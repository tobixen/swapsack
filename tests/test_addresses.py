"""Tests for the permissive destination-address sanity check."""

import pytest

from cryptoswap_wallet.addresses import validate_destination_address

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


def test_truncated_rejected():
    assert validate_destination_address("ETH", "0xdead") is not None
    assert validate_destination_address("TRON", "Tshort") is not None


def test_unknown_chain_has_no_opinion():
    assert validate_destination_address("XRP", "rno_rule_yet") is None
