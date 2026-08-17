"""Tests for the Arbitrum adapter (a full EVM chain: hold/balance/send/swap/LP).

Unlike BSC — which is address+balance only because nothing trades it — ARB has
live Maya pools and a router, so this adapter carries the whole money path. The
things that can silently go wrong on a copied EVM adapter are the chain id (a
tx signed for chain 1 is a *valid mainnet* transaction), the token decimals, and
the token contract, so those are what these pin.
"""

import pytest

from swapsack.chains.arb import ARB_CHAIN_ID, ARB_TRACKED_TOKENS, ArbAdapter
from swapsack.chains.eth import EthAdapter
from swapsack.report import balance_row

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def test_arb_address_equals_eth_address():
    # Arbitrum is EVM (same m/44'/60' derivation), so it IS the ETH address —
    # which is exactly why `--dest` for ARB could be auto-derived.
    assert ArbAdapter().derive_address(MNEMONIC) == EthAdapter().derive_address(
        MNEMONIC
    )
    assert ArbAdapter().derive_address(MNEMONIC) == (
        "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    )


def test_arb_chain_and_asset():
    adapter = ArbAdapter()
    assert adapter.chain == "ARB"
    # Maya's native-ETH-on-Arbitrum pool. NOT "ARB.ARB": the ARB *token* pool is
    # Staged, not tradeable, so the native asset is the one that means anything.
    assert adapter.asset == "ARB.ETH"
    assert adapter.native_symbol == "ETH"


def test_arb_wallet_balance_reports_eth(monkeypatch):
    adapter = ArbAdapter()
    monkeypatch.setattr(
        adapter, "fetch_balance", lambda address: 2_580_000_000_000_000_000
    )
    report = adapter.wallet_balance(MNEMONIC)
    # The coin is ether (native_symbol, what a fee is quoted in), but the
    # *balance row* is chain-qualified — see the next test for why.
    assert report.symbol == "ETH-ARB"
    assert report.decimals == 18
    assert balance_row(report).amount == pytest.approx(2.58)


def test_arb_native_row_is_distinguishable_from_ethereums(monkeypatch):
    """Regression: both adapters call the native coin ETH *and* share one
    address, so `balance` printed two identical-looking rows and there was
    nothing on screen to say which chain held what (docs/TODO.md)."""
    arb, eth = ArbAdapter(), EthAdapter()
    for adapter in (arb, eth):
        monkeypatch.setattr(adapter, "fetch_balance", lambda address: 10**18)
    assert (
        balance_row(arb.wallet_balance(MNEMONIC)).label
        != balance_row(eth.wallet_balance(MNEMONIC)).label
    )
    assert eth.wallet_balance(MNEMONIC).symbol == "ETH"  # unchanged


def test_arb_native_label_is_the_name_you_would_spend_it_by():
    """What `balance` prints must be what `--asset` accepts, or the user cannot
    act on the row."""
    from swapsack.cli import ASSET

    assert ArbAdapter().native_label == "ETH-ARB"
    assert ASSET[ArbAdapter().native_label] == ArbAdapter.asset
    assert EthAdapter().native_label == "ETH"
    assert ASSET[EthAdapter().native_label] == EthAdapter.asset


def test_arb_usdc_is_six_decimals(monkeypatch):
    # 6 like Ethereum's USDC, NOT 18 like BSC's. Getting this wrong misreports
    # (and misspends) by 1e12.
    adapter = ArbAdapter()
    monkeypatch.setattr(
        adapter, "fetch_token_balance", lambda token, address: 2_500_000
    )
    reports = adapter.token_balances(MNEMONIC)
    assert [r.symbol for r in reports] == ["USDC-ARB"]
    assert reports[0].decimals == 6
    assert balance_row(reports[0]).amount == pytest.approx(2.5)


def test_arb_token_decimals_use_trusted_constant():
    adapter = ArbAdapter()
    usdc_contract = ARB_TRACKED_TOKENS[0][1]
    assert adapter.token_decimals(usdc_contract) == 6
    # Case-insensitive (THORChain/Maya upper-case contracts in asset strings).
    assert adapter.token_decimals(usdc_contract.upper()) == 6


def test_arb_usdc_contract_matches_the_asset_table():
    """The ARB.USDC pool names one specific contract; the adapter must track the
    same one. Arbitrum also carries a *bridged* USDC.e at a different address —
    sending that to a pool expecting native USDC loses the funds."""
    from swapsack.cli import ASSET

    contract = ARB_TRACKED_TOKENS[0][1]
    assert ASSET["USDC-ARB"] == f"ARB.USDC-{contract.upper()}"
    assert contract == "0xaf88d065e77c8cc2239327c5edb3a432268e5831"


def test_arb_send_signs_for_chain_id_42161():
    # With Ethereum's chain id 1 the emitted raw tx is a fully valid *mainnet*
    # transaction paying the same recipient in real ETH. Same trap as BSC's.
    adapter = ArbAdapter()
    assert adapter.chain_id == ARB_CHAIN_ID == 42161
    prepared = adapter.build_and_verify_send(
        recipient="0x1111111111111111111111111111111111111111",
        amount=100_000,  # 1e8 units -> 0.001 ETH
        asset="ARB.ETH",
        mnemonic=MNEMONIC,
        nonce=0,
        max_fee_per_gas=3_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == 42161


def test_arb_token_send_signs_for_chain_id_42161():
    adapter = ArbAdapter()
    prepared = adapter.build_and_verify_send(
        recipient="0x1111111111111111111111111111111111111111",
        amount=100_000_000,  # 1e8 units -> 1.0 USDC
        asset=f"ARB.USDC-{ARB_TRACKED_TOKENS[0][1].upper()}",
        mnemonic=MNEMONIC,
        nonce=0,
        max_fee_per_gas=3_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == 42161


ARB_VAULT = "0xe3985E6b61b814F7Cdb188766562ba71b446B46d"


def _arb_swap_quote(dest):
    from swapsack.thorchain import Quote, SwapFees

    return Quote(
        inbound_address=ARB_VAULT,
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


def test_arb_native_swap_passes_its_own_chain_gate():
    """The swap plan must be stated for chain 42161, not Ethereum's default 1.

    EthSwapPlan.chain_id defaults to 1 while the gate is handed the built tx's
    real chain id, so an unqualified plan makes verify_eth_swap refuse every
    native-ARB swap with "chainId 42161 != 1" — the feature fails closed and
    never broadcasts.
    """
    from swapsack.swap import SwapRequest

    adapter = ArbAdapter()
    dest = "0x1111111111111111111111111111111111111111"
    prepared = adapter.build_and_verify(
        quote=_arb_swap_quote(dest),
        request=SwapRequest(
            from_asset="ARB.ETH", to_asset="BTC.BTC", amount=100000, destination=dest
        ),
        now=0,
        mnemonic=MNEMONIC,
        nonce=0,
        gas=60000,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**17,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == ARB_CHAIN_ID


def test_arb_native_lp_deposit_passes_its_own_chain_gate():
    """Same gate, the liquidity path — and the one that can strand funds.

    A *withdraw* of an ARB.USDC position is a native dust trigger, so this path
    is how an ARB.USDC single-sided position is exited. Refused here, a position
    added with this tool would be unexitable by this tool.
    """
    adapter = ArbAdapter()
    prepared = adapter.build_and_verify_deposit(
        vault=ARB_VAULT,
        memo="-:ARB.USDC-0XAF88D065E77C8CC2239327C5EDB3A432268E5831:10000",
        amount=10000,
        now=1000,
        mnemonic=MNEMONIC,
        nonce=4,
        gas=60000,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == ARB_CHAIN_ID


def test_arb_native_send_gas_covers_the_measured_l1_surcharge():
    """Arbitrum bills the L1 calldata cost as extra gas consumed.

    The repo's own measurement (docs/TODO.md) puts a native transfer at 21,345
    against Ethereum's 21,000, so the inherited budget is *below* the floor: the
    tx runs out of gas, reverts, and burns the whole limit delivering nothing.
    A gas limit is not a fee — unused gas is refunded — so headroom is free.
    """
    assert ArbAdapter().native_send_gas >= 21_345


def test_arb_swaps_are_supported():
    """BSC stubs build_and_verify out because nothing trades it. ARB must NOT
    inherit that stub — it has live Maya pools."""
    adapter = ArbAdapter()
    assert adapter.build_and_verify.__qualname__.startswith("EthAdapter")


def test_arb_lp_is_maya_only():
    # THORChain has no ARB pools at all; only Maya does.
    assert ArbAdapter().lp_backends == ("maya",)


@pytest.mark.network
def test_arb_balance_live():
    """Live native + ERC-20 balance against the public Arbitrum RPC — guards the
    call encoding and the tracked-token contract against drift. Asserts shape,
    not a (mutable) balance."""
    with ArbAdapter() as adapter:
        native = adapter.wallet_balance(MNEMONIC)
        assert native.symbol == "ETH-ARB"
        assert native.decimals == 18
        assert native.confirmed >= 0
        reports = adapter.token_balances(MNEMONIC)
        assert [r.symbol for r in reports] == ["USDC-ARB"]
        assert all(r.decimals == 6 and r.confirmed >= 0 for r in reports)


@pytest.mark.network
def test_arb_chain_id_matches_the_live_rpc():
    """The one constant that cannot be wrong. Asks the node rather than trusting
    the literal — an RPC pointed at the wrong network would otherwise only show
    up as a rejected (or worse, valid-elsewhere) broadcast."""
    with ArbAdapter() as adapter:
        assert int(adapter._rpc("eth_chainId", []), 16) == ARB_CHAIN_ID
