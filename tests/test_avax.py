"""Tests for the Avalanche C-Chain adapter (hold/balance/send/swap; no LP).

The third EVM adapter, so the interesting assertions are the ones that catch a
*copy* rather than the ones that prove EVM works. Three things differ per chain
and all three are silent when wrong: the chain id (a tx signed for 1 is a valid
Ethereum *mainnet* transaction paying the same recipient in real ETH), the token
decimals (BSC's are 18, Ethereum's and Arbitrum's 6), and which backend has the
pools (Arbitrum is Maya-only, Avalanche is THORChain-only — the exact inverse,
so inheriting ARB's answer refuses every AVAX liquidity op).

The two ARB overrides that must *not* be copied here are pinned below:
``native_label`` (Avalanche's coin is AVAX, which names its own chain, so it
needs no chain qualifier) and ``native_send_gas`` (Avalanche is an L1 and bills
no L1-calldata surcharge, so Ethereum's 21000 floor is correct).
"""

import dataclasses

import pytest

from swapsack.chains.avax import AVAX_CHAIN_ID, AVAX_TRACKED_TOKENS, AvaxAdapter
from swapsack.chains.eth import EthAdapter
from swapsack.report import balance_row

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

USDC_CONTRACT = "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e"
USDT_CONTRACT = "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7"


def test_avax_address_equals_eth_address():
    # The C-Chain is EVM (same m/44'/60' derivation), so it IS the ETH address —
    # which is why `--dest` for AVAX can be auto-derived.
    assert AvaxAdapter().derive_address(MNEMONIC) == EthAdapter().derive_address(
        MNEMONIC
    )
    assert AvaxAdapter().derive_address(MNEMONIC) == (
        "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    )


def test_avax_chain_and_asset():
    adapter = AvaxAdapter()
    assert adapter.chain == "AVAX"
    assert adapter.asset == "AVAX.AVAX"
    assert adapter.native_symbol == "AVAX"


def test_avax_wallet_balance_reports_avax(monkeypatch):
    adapter = AvaxAdapter()
    monkeypatch.setattr(
        adapter, "fetch_balance", lambda address: 2_580_000_000_000_000_000
    )
    report = adapter.wallet_balance(MNEMONIC)
    assert report.symbol == "AVAX"
    assert report.decimals == 18
    assert balance_row(report).amount == pytest.approx(2.58)


def test_avax_native_row_needs_no_chain_qualifier():
    """ARB overrides ``native_label`` because its coin is *ether* and an
    unqualified row is indistinguishable from Ethereum's. AVAX must not copy
    that: the coin names its own chain, and "AVAX-AVAX" would be noise — and
    worse, would not be a string `--asset` accepts."""
    from swapsack.cli import ASSET

    adapter = AvaxAdapter()
    assert adapter.native_label == "AVAX"
    assert ASSET[adapter.native_label] == AvaxAdapter.asset


def test_avax_tokens_are_six_decimals(monkeypatch):
    # 6 like Ethereum's and Arbitrum's, NOT 18 like BSC's. Getting this wrong
    # misreports (and misspends) by a factor of 1e12.
    adapter = AvaxAdapter()
    monkeypatch.setattr(
        adapter, "fetch_token_balance", lambda token, address: 2_500_000
    )
    reports = adapter.token_balances(MNEMONIC)
    assert [r.symbol for r in reports] == ["USDC-AVAX", "USDT-AVAX"]
    assert all(r.decimals == 6 for r in reports)
    assert all(balance_row(r).amount == pytest.approx(2.5) for r in reports)


def test_avax_token_decimals_use_trusted_constant():
    adapter = AvaxAdapter()
    for _, contract, _ in AVAX_TRACKED_TOKENS:
        assert adapter.token_decimals(contract) == 6
        # Case-insensitive (THORChain upper-cases contracts in asset strings).
        assert adapter.token_decimals(contract.upper()) == 6


def test_avax_token_contracts_match_the_asset_table():
    """The pool names one specific contract; the adapter must track the same one.

    A memo naming a contract the wallet does not hold, or a deposit of a
    look-alike token, either fails to route or pays out something else.
    """
    from swapsack.cli import ASSET

    assert ASSET["USDC-AVAX"] == f"AVAX.USDC-{USDC_CONTRACT.upper()}"
    assert ASSET["USDT-AVAX"] == f"AVAX.USDT-{USDT_CONTRACT.upper()}"
    assert [c for _, c, _ in AVAX_TRACKED_TOKENS] == [USDC_CONTRACT, USDT_CONTRACT]


def test_avax_send_signs_for_chain_id_43114():
    # With Ethereum's chain id 1 the emitted raw tx is a fully valid *mainnet*
    # transaction paying the same recipient in real ETH. Same trap as BSC/ARB.
    adapter = AvaxAdapter()
    assert adapter.chain_id == AVAX_CHAIN_ID == 43114
    prepared = adapter.build_and_verify_send(
        recipient="0x1111111111111111111111111111111111111111",
        amount=100_000,  # 1e8 units -> 0.001 AVAX
        asset="AVAX.AVAX",
        mnemonic=MNEMONIC,
        nonce=0,
        max_fee_per_gas=3_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == 43114


def test_avax_token_send_signs_for_chain_id_43114():
    adapter = AvaxAdapter()
    prepared = adapter.build_and_verify_send(
        recipient="0x1111111111111111111111111111111111111111",
        amount=100_000_000,  # 1e8 units -> 1.0 USDC
        asset=f"AVAX.USDC-{USDC_CONTRACT.upper()}",
        mnemonic=MNEMONIC,
        nonce=0,
        max_fee_per_gas=3_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
    assert prepared.built.tx["chainId"] == 43114


AVAX_VAULT = "0x1c0F3B21b7Bc25C3a4e5B0a3F6F0Fa1B4b9B0f2d"


def _avax_swap_quote(dest):
    from swapsack.thorchain import Quote, SwapFees

    return Quote(
        inbound_address=AVAX_VAULT,
        expected_amount_out=170000,
        memo=f"=:b:{dest}",
        fees=SwapFees("BTC.BTC", 1058, 0, 500, 1558, 20, 50),
        recommended_min_amount_in=1000,
        expiry=9_999_999_999,
        dust_threshold=100000,
        recommended_gas_rate=15,
        gas_rate_units="nAVAX",
        router=None,
        max_streaming_quantity=1,
        streaming_swap_blocks=1,
        total_swap_seconds=30,
        raw={},
    )


def test_avax_native_swap_passes_its_own_chain_gate():
    """The swap plan must be stated for chain 43114, not Ethereum's default 1.

    ``EthSwapPlan.chain_id`` defaults to 1 while the gate is handed the built
    tx's real chain id, so a builder that omits ``chain_id=self.chain_id`` from
    the plan refuses every native-AVAX swap with "chainId 43114 != 1". That
    fails closed rather than losing money, but it silently disables the path.
    """
    from swapsack.swap import SwapRequest

    adapter = AvaxAdapter()
    dest = "0x1111111111111111111111111111111111111111"
    prepared = adapter.build_and_verify(
        quote=_avax_swap_quote(dest),
        request=SwapRequest(
            from_asset="AVAX.AVAX", to_asset="BTC.BTC", amount=100000, destination=dest
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
    assert prepared.built.tx["chainId"] == AVAX_CHAIN_ID


AVAX_ROUTER = "0x00dc6100103BC402d490aEE3F9a5560cBd91f1d4"


def test_avax_token_swap_rescales_by_this_chains_decimals_and_passes_its_gate():
    """The only AVAX path where a per-chain constant rescales the *amount*.

    A token source is an approve + ``router.depositWithExpiry`` pair, and the
    deposit's value is ``request.amount * 10**decimals // 10**8``. Every other
    AVAX path moves either the 18-decimal native coin or a fixed number of
    token units; this one multiplies by the tracked-token table. Inherit BSC's
    18 by mistake and a 1 USDC swap deposits 1e12 USDC — which the contract
    would simply fail, but a *smaller* wrong exponent would silently overspend.

    Pinned here because the native-swap test above cannot reach this branch:
    ``build_and_verify`` dispatches on ``"-" in request.from_asset``.
    """
    from swapsack.chains.eth import DEPOSIT_SELECTOR, _decode_call
    from swapsack.swap import SwapRequest

    adapter = AvaxAdapter()
    dest = "0x1111111111111111111111111111111111111111"
    quote = _avax_swap_quote(dest)
    quote = dataclasses.replace(quote, router=AVAX_ROUTER)
    prepared = adapter.build_and_verify(
        quote=quote,
        request=SwapRequest(
            from_asset=f"AVAX.USDC-{USDC_CONTRACT.upper()}",
            to_asset="BTC.BTC",
            amount=100_000_000,  # 1e8 units -> 1.0 USDC
            destination=dest,
        ),
        now=0,
        mnemonic=MNEMONIC,
        nonce=7,
        gas=60000,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**17,
    )
    assert prepared.problems == []
    assert prepared.built.approve_tx["chainId"] == AVAX_CHAIN_ID
    assert prepared.built.deposit_tx["chainId"] == AVAX_CHAIN_ID
    # 1e8 wallet units of a 6-decimal token = 1_000_000 native units, not 1e18.
    assert prepared.built.native_amount == 1_000_000
    _, _, deposited, _, _ = _decode_call(
        prepared.built.deposit_tx["data"],
        DEPOSIT_SELECTOR,
        ["address", "address", "uint256", "string", "uint256"],
    )
    assert deposited == 1_000_000
    # The approve must be for the same router the deposit is sent to, or the
    # transferFrom inside depositWithExpiry reverts after the approve is live.
    assert prepared.built.router == prepared.built.deposit_tx["to"] == AVAX_ROUTER


def test_avax_native_lp_deposit_passes_its_own_chain_gate():
    """Same gate, the liquidity path — the one that can strand funds.

    THORChain has LP deposits globally paused today, so nothing can *enter*;
    this is here because a *withdraw* is also a native dust trigger, and a
    position added by any other tool has to be exitable by this one.
    """
    adapter = AvaxAdapter()
    prepared = adapter.build_and_verify_deposit(
        vault=AVAX_VAULT,
        memo=f"-:AVAX.USDC-{USDC_CONTRACT.upper()}:10000",
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
    assert prepared.built.tx["chainId"] == AVAX_CHAIN_ID


def test_avax_native_send_gas_stays_at_ethereums_floor():
    """ARB raises ``native_send_gas`` to 30000 because Arbitrum bills the L1
    calldata cost as extra gas consumed. Avalanche is an L1 with no such
    surcharge, so the inherited 21000 is the whole cost of a value transfer and
    raising it would only be cargo-culting ARB. (A limit is refunded when
    unused, so this is about the comment, not the money — but the *reason* is
    what a future L2 adapter needs to read here.)"""
    from swapsack.chains.eth import NATIVE_SEND_GAS

    assert AvaxAdapter().native_send_gas == NATIVE_SEND_GAS == 21_000


def test_avax_swaps_are_supported():
    """BSC stubs ``build_and_verify`` out because nothing trades it. AVAX must
    NOT inherit that stub — `AVAX.AVAX` is Available and unhalted."""
    adapter = AvaxAdapter()
    assert adapter.build_and_verify.__qualname__.startswith("EthAdapter")


def test_avax_lp_is_thorchain_only():
    """The exact inverse of ARB, which is Maya-only. Maya has no AVAX pools at
    all, so `balance` must not probe it for positions that cannot exist, and an
    `add-liquidity --backend maya` must be refused up front."""
    assert AvaxAdapter().lp_backends == ("thorchain",)


@pytest.mark.network
def test_avax_balance_live():
    """Live native + ERC-20 balance against the public Avalanche RPC — guards
    the call encoding and the tracked-token contracts against drift. Asserts
    shape, not a (mutable) balance."""
    with AvaxAdapter() as adapter:
        native = adapter.wallet_balance(MNEMONIC)
        assert native.symbol == "AVAX"
        assert native.decimals == 18
        assert native.confirmed >= 0
        reports = adapter.token_balances(MNEMONIC)
        assert [r.symbol for r in reports] == ["USDC-AVAX", "USDT-AVAX"]
        assert all(r.decimals == 6 and r.confirmed >= 0 for r in reports)


@pytest.mark.network
def test_avax_chain_id_matches_the_live_rpc():
    """The one constant that cannot be wrong. Asks the node rather than trusting
    the literal — an RPC pointed at the wrong network would otherwise only show
    up as a rejected (or worse, valid-elsewhere) broadcast."""
    with AvaxAdapter() as adapter:
        assert int(adapter._rpc("eth_chainId", []), 16) == AVAX_CHAIN_ID


@pytest.mark.network
def test_avax_token_decimals_match_the_live_contracts():
    """The trusted table is trusted *because* it was read off the chain once.

    This is that reading, kept runnable: a wrong entry here misspends by 1e12,
    and the whole point of `known_token_decimals` is to not ask an RPC at spend
    time.
    """
    with AvaxAdapter() as adapter:
        for _, contract, decimals in AVAX_TRACKED_TOKENS:
            onchain = int(
                adapter._rpc(
                    "eth_call",
                    [
                        {
                            "to": contract,
                            "data": "0x313ce567",  # decimals()
                        },
                        "latest",
                    ],
                ),
                16,
            )
            assert onchain == decimals == 6
