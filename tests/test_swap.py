"""Tests for the swap orchestrator, using fake THORChain client and adapter."""

from types import SimpleNamespace

import pytest

from cryptoswap.chains.coins import Utxo
from cryptoswap.swap import SwapAborted, SwapRequest, execute_swap, prepare_btc_swap
from cryptoswap.thorchain import ChainStatus, Quote, SwapFees
from cryptoswap.verify import TxOutput

VAULT = "bc1qvault"
CHANGE = "bc1qchange"
MINE = "bc1qmine"
MEMO = "=:ETH.ETH:0xdest:0"


def make_quote(
    min_in: int = 7761, memo: str | None = MEMO, expiry: int = 10**12
) -> Quote:
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


def make_status(tradable: bool = True) -> ChainStatus:
    return ChainStatus(
        chain="BTC",
        gas_rate=3,
        gas_rate_units="satsperbyte",
        outbound_fee=1058,
        dust_threshold=1000,
        halted=not tradable,
        global_trading_paused=False,
        chain_trading_paused=False,
    )


class FakeThor:
    def __init__(self, quote=None, tradable=True):
        self._quote = quote or make_quote()
        self._tradable = tradable

    def inbound_addresses(self):
        return {"BTC": make_status(self._tradable)}

    def quote_swap(self, *args, **kwargs):
        return self._quote


class FakeAdapter:
    chain = "BTC"

    def __init__(self, outputs=None, fee=400):
        self._outputs = outputs
        self._fee = fee
        self.signed = False
        self.broadcasted = False

    def build_unsigned_swap(
        self,
        *,
        mnemonic,
        utxos,
        vault_address,
        amount,
        memo,
        fee_rate,
        change_address,
        sweep=False,
    ):
        outputs = self._outputs
        if outputs is None:
            outputs = [
                TxOutput(address=vault_address, value=amount),
                TxOutput(address=None, value=0, op_return_data=memo.encode()),
                TxOutput(address=change_address, value=10000),
            ]
        return SimpleNamespace(
            outputs=outputs, fee=self._fee, change_address=change_address
        )

    def sign(self, built):
        self.signed = True
        return "deadbeef"

    def broadcast(self, raw_hex):
        self.broadcasted = True
        return "txid123"


def make_request(amount: int = 178100) -> SwapRequest:
    return SwapRequest(
        from_asset="BTC.BTC", to_asset="ETH.ETH", amount=amount, destination="0xdest"
    )


def make_utxos():
    return [
        Utxo(txid="aa" * 32, vout=0, value=200000, address=MINE, path="m/84'/0'/0'/0/0")
    ]


def prepare(thor=None, adapter=None, amount=178100, now=0):
    return prepare_btc_swap(
        thorchain=thor or FakeThor(),
        adapter=adapter or FakeAdapter(),
        mnemonic="m",
        request=make_request(amount),
        scanned_utxos=make_utxos(),
        fee_rate=2,
        change_address=CHANGE,
        now=now,
        max_fee=100000,
    )


def test_prepare_clean_passes_gate():
    p = prepare()
    assert p.safe
    assert p.problems == []
    assert p.quote.inbound_address == VAULT


def test_prepare_aborts_when_chain_halted():
    with pytest.raises(SwapAborted):
        prepare(thor=FakeThor(tradable=False))


def test_prepare_aborts_below_recommended_min():
    with pytest.raises(SwapAborted):
        prepare(thor=FakeThor(quote=make_quote(min_in=999999)))


def test_prepare_aborts_without_memo():
    with pytest.raises(SwapAborted):
        prepare(thor=FakeThor(quote=make_quote(memo=None)))


def test_prepare_flags_mismatched_build():
    bad = [
        TxOutput(address=VAULT, value=999),  # wrong amount
        TxOutput(address=None, value=0, op_return_data=MEMO.encode()),
        TxOutput(address=CHANGE, value=10000),
    ]
    p = prepare(adapter=FakeAdapter(outputs=bad))
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
    adapter = FakeAdapter(outputs=[TxOutput(address=VAULT, value=999)])
    prepared = prepare(adapter=adapter)
    with pytest.raises(SwapAborted):
        execute_swap(prepared, adapter, confirm=True)
    assert adapter.broadcasted is False
