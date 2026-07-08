"""Tests for the BSC adapter (address + balance only; swaps unsupported)."""

import pytest

from swapsack.chains.bsc import BSC_TRACKED_TOKENS, BscAdapter
from swapsack.chains.eth import EthAdapter

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def test_bsc_address_equals_eth_address():
    # BSC is EVM (same m/44'/60' derivation), so the address is the ETH address.
    assert BscAdapter().derive_address(MNEMONIC) == EthAdapter().derive_address(
        MNEMONIC
    )
    assert BscAdapter().derive_address(MNEMONIC) == (
        "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    )


def test_bsc_chain_and_asset():
    adapter = BscAdapter()
    assert adapter.chain == "BSC"
    assert adapter.asset == "BSC.BNB"


def test_bsc_wallet_balance_reports_bnb(monkeypatch):
    adapter = BscAdapter()
    monkeypatch.setattr(
        adapter, "fetch_balance", lambda address: 2_580_000_000_000_000_000
    )
    report = adapter.wallet_balance(MNEMONIC)
    assert report.symbol == "BNB"
    assert report.decimals == 18
    assert report.format().startswith("BNB: 2.58")


def test_bsc_token_balances_are_18_decimals(monkeypatch):
    adapter = BscAdapter()
    # 2.5 of an 18-decimal token.
    monkeypatch.setattr(
        adapter, "fetch_token_balance", lambda token, address: 2_500_000_000_000_000_000
    )
    reports = adapter.token_balances(MNEMONIC)
    assert [r.symbol for r in reports] == ["USDT-BSC", "USDC-BSC"]
    assert all(r.decimals == 18 for r in reports)
    assert reports[0].format().startswith("USDT-BSC: 2.50")
    assert reports[1].format().startswith("USDC-BSC: 2.50")


def test_bsc_token_decimals_use_trusted_constant():
    # 18 (not 6) for BSC USDC, returned from the known table without any RPC call.
    adapter = BscAdapter()
    usdc_contract = BSC_TRACKED_TOKENS[1][1]
    assert adapter.token_decimals(usdc_contract) == 18
    # Case-insensitive (THORChain upper-cases contracts in asset strings).
    assert adapter.token_decimals(usdc_contract.upper()) == 18


def test_bsc_send_signs_for_chain_id_56():
    # The inherited EVM send path must sign with BSC's chain id — with
    # Ethereum's 1 the node rejects the tx, and worse, the emitted raw tx is a
    # fully valid *mainnet* transaction paying the same recipient in ETH.
    adapter = BscAdapter()
    assert adapter.chain_id == 56
    prepared = adapter.build_and_verify_send(
        recipient="0x1111111111111111111111111111111111111111",
        amount=100_000,  # 1e8 units -> 0.001 BNB
        asset="BSC.BNB",
        mnemonic=MNEMONIC,
        nonce=0,
        max_fee_per_gas=3_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == 56


def test_bsc_swaps_not_supported():
    with pytest.raises(NotImplementedError, match="BSC swaps are not supported"):
        BscAdapter().build_and_verify(quote=None, request=None, now=0, mnemonic="")


@pytest.mark.network
def test_bsc_balance_live():
    """Live native + BEP-20 balance against the public BSC RPC — guards the call
    encoding/decoding and the tracked-token contracts against drift. Asserts
    shape, not a (mutable) balance."""
    with BscAdapter() as adapter:
        native = adapter.wallet_balance(MNEMONIC)
        assert native.symbol == "BNB"
        assert native.decimals == 18
        assert native.confirmed >= 0
        reports = adapter.token_balances(MNEMONIC)
        assert [r.symbol for r in reports] == ["USDT-BSC", "USDC-BSC"]
        assert all(r.decimals == 18 and r.confirmed >= 0 for r in reports)
