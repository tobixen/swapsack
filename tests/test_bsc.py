"""Tests for the BSC adapter (hold/balance/send/swap; THORChain-only).

BSC is the one EVM chain whose dollar tokens are **18-decimal**, so the
assertions that matter are the ones a copy of the 6-decimal AVAX/ETH adapters
would get silently wrong: every path that rescales a 1e8 wallet amount into
token units, and the chain id.
"""

import dataclasses

import pytest

from swapsack.chains.bsc import BSC_CHAIN_ID, BSC_TRACKED_TOKENS, BscAdapter
from swapsack.chains.eth import EthAdapter
from swapsack.report import balance_row

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
    assert balance_row(report).amount == pytest.approx(2.58)
    # A subclass that overrides only native_symbol must still be labelled by it:
    # the chain-qualified label exists for chains whose native coin is *ether*
    # (Arbitrum), and must not leak Ethereum's name onto BNB.
    assert adapter.native_label == "BNB"


def test_bsc_token_balances_are_18_decimals(monkeypatch):
    adapter = BscAdapter()
    # 2.5 of an 18-decimal token.
    monkeypatch.setattr(
        adapter, "fetch_token_balance", lambda token, address: 2_500_000_000_000_000_000
    )
    reports = adapter.token_balances(MNEMONIC)
    assert [r.symbol for r in reports] == ["USDT-BSC", "USDC-BSC"]
    assert all(r.decimals == 18 for r in reports)
    assert balance_row(reports[0]).amount == pytest.approx(2.5)
    assert balance_row(reports[1]).amount == pytest.approx(2.5)


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


USDT_CONTRACT = BSC_TRACKED_TOKENS[0][1]
USDC_CONTRACT = BSC_TRACKED_TOKENS[1][1]


def test_bsc_token_contracts_match_the_asset_table():
    """The pool names one specific contract; the adapter must track the same one."""
    from swapsack.cli import ASSET

    assert ASSET["BNB"] == BscAdapter.asset
    assert ASSET["USDT-BSC"] == f"BSC.USDT-{USDT_CONTRACT.upper()}"
    assert ASSET["USDC-BSC"] == f"BSC.USDC-{USDC_CONTRACT.upper()}"


def test_bsc_token_send_rescales_by_18_decimals():
    """1e8 wallet units of USDT-BSC is 1e18 token units, not ETH's 1e6.

    The 6-decimal answer would send a millionth of a millionth of a dollar;
    the inverse mistake elsewhere would overspend by 1e12.
    """
    from swapsack.chains.eth import TRANSFER_SELECTOR, _decode_call

    prepared = BscAdapter().build_and_verify_send(
        recipient="0x1111111111111111111111111111111111111111",
        amount=100_000_000,  # 1e8 units -> 1.0 USDT
        asset=f"BSC.USDT-{USDT_CONTRACT.upper()}",
        mnemonic=MNEMONIC,
        nonce=0,
        max_fee_per_gas=3_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == BSC_CHAIN_ID
    _, amount = _decode_call(
        prepared.built.tx["data"], TRANSFER_SELECTOR, ["address", "uint256"]
    )
    assert amount == 10**18


BSC_VAULT = "0x1c0F3B21b7Bc25C3a4e5B0a3F6F0Fa1B4b9B0f2d"
BSC_ROUTER = "0xb30eC53F98ff5947EDe720D32aC2da7e52A5f56b"


def _bsc_swap_quote(dest):
    from swapsack.thorchain import Quote, SwapFees

    return Quote(
        inbound_address=BSC_VAULT,
        expected_amount_out=170000,
        memo=f"=:b:{dest}",
        fees=SwapFees("BTC.BTC", 1058, 0, 500, 1558, 20, 50),
        recommended_min_amount_in=1000,
        expiry=9_999_999_999,
        dust_threshold=100000,
        recommended_gas_rate=1,
        gas_rate_units="gwei",
        router=BSC_ROUTER,
        max_streaming_quantity=1,
        streaming_swap_blocks=1,
        total_swap_seconds=30,
        raw={},
    )


def test_bsc_native_swap_passes_its_own_chain_gate():
    """The plan must be stated for chain 56; EthSwapPlan defaults to 1."""
    from swapsack.swap import SwapRequest

    dest = "bc1qexampledest"
    prepared = BscAdapter().build_and_verify(
        quote=dataclasses.replace(_bsc_swap_quote(dest), router=None),
        request=SwapRequest(
            from_asset="BSC.BNB", to_asset="BTC.BTC", amount=100000, destination=dest
        ),
        now=0,
        mnemonic=MNEMONIC,
        nonce=0,
        gas=60000,
        max_fee_per_gas=3_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == BSC_CHAIN_ID


def test_bsc_token_swap_rescales_by_18_decimals_and_passes_its_gate():
    """USDT-BSC -> BTC: approve + router deposit, both on chain 56, 1e18 per unit."""
    from swapsack.chains.eth import DEPOSIT_SELECTOR, _decode_call
    from swapsack.swap import SwapRequest

    dest = "bc1qexampledest"
    prepared = BscAdapter().build_and_verify(
        quote=_bsc_swap_quote(dest),
        request=SwapRequest(
            from_asset=f"BSC.USDT-{USDT_CONTRACT.upper()}",
            to_asset="BTC.BTC",
            amount=100_000_000,  # 1e8 units -> 1.0 USDT
            destination=dest,
        ),
        now=0,
        mnemonic=MNEMONIC,
        nonce=7,
        gas=60000,
        max_fee_per_gas=3_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.approve_tx["chainId"] == BSC_CHAIN_ID
    assert prepared.built.deposit_tx["chainId"] == BSC_CHAIN_ID
    assert prepared.built.native_amount == 10**18
    _, _, deposited, _, _ = _decode_call(
        prepared.built.deposit_tx["data"],
        DEPOSIT_SELECTOR,
        ["address", "address", "uint256", "string", "uint256"],
    )
    assert deposited == 10**18
    assert prepared.built.router == prepared.built.deposit_tx["to"] == BSC_ROUTER


def test_bsc_swaps_are_supported():
    """The hand-written stub is gone: THORChain's BSC halt lifted 2026-09-02."""
    assert BscAdapter().build_and_verify.__qualname__.startswith("EthAdapter")


def test_bsc_lp_is_thorchain_only():
    """THORChain runs BSC pools; Maya has none."""
    assert BscAdapter().lp_backends == ("thorchain",)


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


@pytest.mark.network
def test_bsc_chain_id_matches_the_live_rpc():
    with BscAdapter() as adapter:
        assert int(adapter._rpc("eth_chainId", []), 16) == BSC_CHAIN_ID


@pytest.mark.network
def test_bsc_token_decimals_match_the_live_contracts():
    """The trusted 18 was read off the chain once; keep that reading runnable."""
    with BscAdapter() as adapter:
        for _, contract, decimals in BSC_TRACKED_TOKENS:
            onchain = int(
                adapter._rpc(
                    "eth_call",
                    [{"to": contract, "data": "0x313ce567"}, "latest"],  # decimals()
                ),
                16,
            )
            assert onchain == decimals == 18
