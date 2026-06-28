"""Tests for swap-backend gathering and lowest-price selection."""

from cryptoswap_wallet.backends import Backend, best_quote, gather_quotes
from cryptoswap_wallet.thorchain import Quote, SwapFees, ThorchainError


def make_quote(out, *, min_in=1000, memo="=:e:0xdest"):
    return Quote(
        inbound_address="vault",
        expected_amount_out=out,
        memo=memo,
        fees=SwapFees("ETH.ETH", 0, 0, 0, 0, 0, 0),
        recommended_min_amount_in=min_in,
        expiry=10**12,
        dust_threshold=0,
        recommended_gas_rate=1,
        gas_rate_units="x",
        router=None,
        max_streaming_quantity=1,
        streaming_swap_blocks=1,
        total_swap_seconds=1,
        raw={},
    )


class FakeClient:
    def __init__(self, quote=None, exc=None):
        self._quote = quote
        self._exc = exc

    def quote_swap(self, *args, **kwargs):
        if self._exc:
            raise self._exc
        return self._quote


def test_best_quote_picks_highest_output():
    results = [
        (Backend("a", FakeClient()), make_quote(100)),
        (Backend("b", FakeClient()), make_quote(200)),
    ]
    backend, quote = best_quote(results)
    assert backend.name == "b"
    assert quote.expected_amount_out == 200


def test_gather_skips_errors_below_min_and_no_memo():
    backends = [
        Backend("ok", FakeClient(make_quote(100))),
        Backend("err", FakeClient(exc=ThorchainError("no pool"))),
        Backend("toolow", FakeClient(make_quote(100, min_in=999_999))),
        Backend("nomemo", FakeClient(make_quote(100, memo=None))),
    ]
    results = gather_quotes(backends, "BTC.BTC", "ETH.ETH", 178100, "0xdest")
    assert [b.name for b, _ in results] == ["ok"]
