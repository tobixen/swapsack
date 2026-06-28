"""Tests for ETH address derivation (destination for BTC->ETH swaps)."""

import pytest

pytest.importorskip("bitcoinlib")

from cryptoswap.chains.eth import EthAdapter, to_checksum_address  # noqa: E402

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def test_derive_eth_address_matches_vector():
    # m/44'/60'/0'/0/0 for the canonical test mnemonic
    assert EthAdapter().derive_address(MNEMONIC) == (
        "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    )


def test_eip55_checksum():
    raw = bytes.fromhex("5aaeb6053f3e94c9b9a09f33669435e7ef1beaed")
    assert to_checksum_address(raw) == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


VAULT = "0x85034887f6656d610c38ef1710208495791fb146"
BTC_MEMO = "=:BTC.BTC:bc1qexampledest:123"


def _build(nonce=0):
    return EthAdapter().build_unsigned_swap(
        mnemonic=MNEMONIC,
        vault_address=VAULT,
        amount=100000,  # 1e8 units -> 1e15 wei
        memo=BTC_MEMO,
        nonce=nonce,
        gas=60000,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
    )


def test_build_eth_swap_tx_fields():
    built = _build(nonce=3)
    assert built.value == 100000 * 10**10
    assert built.data == "0x" + BTC_MEMO.encode().hex()
    assert built.chain_id == 1
    assert built.to.lower() == VAULT
    assert built.tx["nonce"] == 3
    assert built.fee == 60000 * 20_000_000_000


def test_eth_sign_produces_typed_raw():
    raw = EthAdapter().sign(_build())
    assert raw.startswith("0x02")  # EIP-1559 typed transaction


def test_eth_sweep_amount_leaves_gas_reserve():
    from cryptoswap.chains.eth import eth_sweep_amount

    amount = eth_sweep_amount(10**18, gas=60000, max_fee_per_gas=20_000_000_000)
    expected = (10**18 - 60000 * 20_000_000_000) // 10**10
    assert amount == expected


def test_eth_sweep_amount_insufficient():
    import pytest

    from cryptoswap.chains.coins import InsufficientFunds
    from cryptoswap.chains.eth import eth_sweep_amount

    with pytest.raises(InsufficientFunds):
        eth_sweep_amount(1000, gas=60000, max_fee_per_gas=20_000_000_000)


def test_eth_build_and_verify_clean():
    from cryptoswap.swap import SwapRequest
    from cryptoswap.thorchain import Quote, SwapFees

    a = EthAdapter()
    dest = "bc1qexampledest"
    quote = Quote(
        inbound_address=VAULT,
        expected_amount_out=170000,
        memo=f"=:b:{dest}",
        fees=SwapFees("BTC.BTC", 1058, 0, 500, 1558, 20, 50),
        recommended_min_amount_in=1000,
        expiry=9_999_999_999,
        dust_threshold=1000,
        recommended_gas_rate=15,
        gas_rate_units="gwei",
        router=None,
        max_streaming_quantity=1,
        streaming_swap_blocks=1,
        total_swap_seconds=30,
        raw={},
    )
    request = SwapRequest(
        from_asset="ETH.ETH", to_asset="BTC.BTC", amount=100000, destination=dest
    )
    prepared = a.build_and_verify(
        quote=quote,
        request=request,
        now=0,
        mnemonic=MNEMONIC,
        nonce=0,
        gas=60000,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**17,
    )
    assert prepared.problems == []
