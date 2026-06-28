"""Tests for the chain-agnostic swap orchestrator (prepare_swap / execute_swap).

A fake adapter supplies build_and_verify so these exercise only the
chain-agnostic flow; the real build+verify integration lives in the per-chain
adapter tests.
"""

from types import SimpleNamespace

import pytest

from cryptoswap.swap import (
    Prepared,
    SwapAborted,
    SwapRequest,
    execute_swap,
    prepare_swap,
)
from cryptoswap.thorchain import ChainStatus, Quote, SwapFees

VAULT = "bc1qvault"


def make_quote(
    min_in: int = 7761, memo: str | None = "=:e:0xdest", expiry: int = 10**12
):
    return Quote(
        inbound_address=VAULT,
        expected_amount_out=6768430,
        memo=memo,
        fees=SwapFees("ETH.ETH", 15820, 0, 13590, 29410, 19, 43),
        recommended_min_amount_in=min_in,
        expiry=expiry,
        dust_threshold=1000,
        recommended_gas_rate=4,
        gas_rate_units="satsperbyte",
        router=None,
        max_streaming_quantity=1,
        streaming_swap_blocks=1,
        total_swap_seconds=600,
        raw={},
    )


def make_status(chain="BTC", tradable=True):
    return ChainStatus(
        chain=chain,
        gas_rate=3,
        gas_rate_units="satsperbyte",
        outbound_fee=1058,
        dust_threshold=1000,
        halted=not tradable,
        global_trading_paused=False,
        chain_trading_paused=False,
    )


class FakeThor:
    def __init__(self, quote=None, tradable=True, chain="BTC"):
        self._quote = quote or make_quote()
        self._tradable = tradable
        self._chain = chain

    def inbound_addresses(self):
        return {self._chain: make_status(self._chain, self._tradable)}

    def quote_swap(self, *args, **kwargs):
        return self._quote


class FakeAdapter:
    def __init__(self, chain="BTC", problems=None):
        self.chain = chain
        self._problems = problems or []
        self.signed = False
        self.broadcasted = False
        self.build_kwargs = None

    def build_and_verify(self, *, quote, request, now, **kwargs):
        self.build_kwargs = kwargs
        plan = SimpleNamespace(expiry=quote.expiry, destination=request.destination)
        built = SimpleNamespace(fee=400)
        return Prepared(
            quote=quote, built=built, plan=plan, problems=list(self._problems)
        )

    def sign(self, built):
        self.signed = True
        return "deadbeef"

    def broadcast(self, raw_hex):
        self.broadcasted = True
        return "txid123"


def make_request(amount=178100, from_asset="BTC.BTC", to_asset="ETH.ETH"):
    return SwapRequest(
        from_asset=from_asset, to_asset=to_asset, amount=amount, destination="0xdest"
    )


def prepare(thor=None, adapter=None, amount=178100):
    return prepare_swap(
        thorchain=thor or FakeThor(),
        adapter=adapter or FakeAdapter(),
        request=make_request(amount),
        now=0,
        mnemonic="m",  # forwarded via build_kwargs, ignored by the fake
    )


def test_prepare_clean_passes_gate():
    p = prepare()
    assert p.safe
    assert p.quote.inbound_address == VAULT


def test_prepare_forwards_build_kwargs_to_adapter():
    adapter = FakeAdapter()
    prepare(adapter=adapter)
    assert adapter.build_kwargs == {"mnemonic": "m"}


def test_prepare_aborts_when_chain_halted():
    with pytest.raises(SwapAborted):
        prepare(thor=FakeThor(tradable=False))


def test_prepare_aborts_below_recommended_min():
    with pytest.raises(SwapAborted):
        prepare(thor=FakeThor(quote=make_quote(min_in=999999)))


def test_prepare_aborts_without_memo():
    with pytest.raises(SwapAborted):
        prepare(thor=FakeThor(quote=make_quote(memo=None)))


def test_prepare_works_for_eth_chain_too():
    p = prepare(thor=FakeThor(chain="ETH"), adapter=FakeAdapter(chain="ETH"))
    assert p.safe


def test_prepare_surfaces_adapter_problems():
    p = prepare(adapter=FakeAdapter(problems=["vault mismatch"]))
    assert not p.safe


def test_execute_dry_run_does_not_sign_or_broadcast():
    adapter = FakeAdapter()
    result = execute_swap(prepare(adapter=adapter), adapter, confirm=False)
    assert result.broadcast is False
    assert result.txid is None
    assert adapter.signed is False


def test_execute_confirm_signs_and_broadcasts():
    adapter = FakeAdapter()
    result = execute_swap(prepare(adapter=adapter), adapter, confirm=True)
    assert result.broadcast is True
    assert result.txid == "txid123"
    assert adapter.signed is True


def test_execute_blocks_unsafe_transaction():
    adapter = FakeAdapter(problems=["bad"])
    with pytest.raises(SwapAborted):
        execute_swap(prepare(adapter=adapter), adapter, confirm=True)
    assert adapter.broadcasted is False
