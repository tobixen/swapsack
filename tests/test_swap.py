"""Tests for the chain-agnostic swap orchestrator (prepare_swap / execute_swap).

A fake adapter supplies build_and_verify so these exercise only the
chain-agnostic flow; the real build+verify integration lives in the per-chain
adapter tests.
"""

from types import SimpleNamespace

import pytest

from swapsack.swap import (
    Prepared,
    SwapAborted,
    SwapRequest,
    execute_swap,
    prepare_liquidity,
    prepare_swap,
)
from swapsack.thorchain import ChainStatus, Quote, SwapFees

VAULT = "bc1qvault"


def make_quote(
    min_in: int = 7761,
    memo: str | None = "=:e:0xdest",
    expiry: int = 10**12,
    inbound_address: str = VAULT,
):
    return Quote(
        inbound_address=inbound_address,
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


def make_status(chain="BTC", tradable=True, dust_threshold=1000):
    return ChainStatus(
        chain=chain,
        gas_rate=3,
        gas_rate_units="satsperbyte",
        outbound_fee=1058,
        dust_threshold=dust_threshold,
        halted=not tradable,
        global_trading_paused=False,
        chain_trading_paused=False,
        address=VAULT,
    )


class FakeThor:
    def __init__(
        self,
        quote=None,
        tradable=True,
        chain="BTC",
        mimir=None,
        dust_threshold=1000,
        path_prefix="thorchain",
    ):
        self._quote = quote or make_quote()
        self._tradable = tradable
        self._chain = chain
        self._mimir = mimir or {}
        self._dust = dust_threshold
        self.path_prefix = path_prefix

    def inbound_addresses(self):
        return {self._chain: make_status(self._chain, self._tradable, self._dust)}

    def quote_swap(self, *args, **kwargs):
        return self._quote

    def mimir(self):
        return self._mimir


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

    def build_and_verify_deposit(self, *, vault, memo, amount, now, **kwargs):
        plan = SimpleNamespace(
            expiry=now + 3600, inbound_address=vault, amount=amount, memo=memo
        )
        return Prepared(
            quote=None,
            built=SimpleNamespace(fee=400),
            plan=plan,
            problems=list(self._problems),
        )

    def sign(self, built):
        self.signed = True
        return ["deadbeef"]

    def broadcast(self, raws):
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


def _native_adapter(chain="THOR", home_path_prefix="thorchain"):
    adapter = FakeAdapter(chain=chain)
    adapter.native_source = True
    adapter.home_path_prefix = home_path_prefix
    return adapter


def test_prepare_native_source_aborts_on_foreign_network():
    # A native MsgDeposit executes on the adapter's own chain regardless of the
    # quoting network, so a THOR.RUNE source quoted on the maya backend
    # (path_prefix "mayachain") would carry a Maya-priced memo. The guard is a
    # LOCAL identity comparison (no inbound_addresses() I/O).
    with pytest.raises(SwapAborted, match="home network"):
        prepare_swap(
            thorchain=FakeThor(path_prefix="mayachain"),
            adapter=_native_adapter("THOR", home_path_prefix="thorchain"),
            request=make_request(from_asset="THOR.RUNE"),
            now=0,
            mnemonic="m",
        )


def test_prepare_native_source_passes_on_home_network():
    p = prepare_swap(
        thorchain=FakeThor(path_prefix="thorchain"),
        adapter=_native_adapter("THOR", home_path_prefix="thorchain"),
        request=make_request(from_asset="THOR.RUNE"),
        now=0,
        mnemonic="m",
    )
    assert p.safe


def test_prepare_native_source_makes_no_inbound_addresses_call():
    # The native guard must not hit the network (an inbound_addresses() call
    # added a per-swap round trip and an uncaught-HTTP crash mode for a deposit
    # that needs no vault data at all).
    class ExplodingInbound(FakeThor):
        def inbound_addresses(self):
            raise AssertionError("native path must not call inbound_addresses()")

    p = prepare_swap(
        thorchain=ExplodingInbound(path_prefix="thorchain"),
        adapter=_native_adapter("THOR", home_path_prefix="thorchain"),
        request=make_request(from_asset="THOR.RUNE"),
        now=0,
        mnemonic="m",
    )
    assert p.safe


class _RaisingThor(FakeThor):
    def __init__(self, message, **kw):
        super().__init__(**kw)
        self._message = message

    def quote_swap(self, *args, **kwargs):
        from swapsack.thorchain import ThorchainError

        raise ThorchainError(self._message)


def test_prepare_translates_price_limit_error_into_tolerance_abort():
    # THORChain rejects when fees/slippage exceed tolerance; the raw error must
    # become a clean SwapAborted that points at --tolerance-bps (no traceback).
    thor = _RaisingThor(
        "failed to simulate swap: emit asset 2425906900 less than price "
        "limit 2707570991: invalid request"
    )
    with pytest.raises(SwapAborted, match="tolerance"):
        prepare(thor=thor)


def test_prepare_translates_generic_thorchain_error_into_abort():
    thor = _RaisingThor("pool suspended")
    with pytest.raises(SwapAborted, match="THORChain rejected"):
        prepare(thor=thor)


def test_prepare_aborts_when_quote_omits_inbound_address():
    # parse_quote tolerates a missing inbound_address (native quotes have
    # none), but for an external-chain source an empty vault must abort loudly
    # before any money-path work — not crash inside signing or, worse, build a
    # tx around "".
    with pytest.raises(SwapAborted, match="inbound"):
        prepare(thor=FakeThor(quote=make_quote(inbound_address="")))


def test_prepare_native_source_tolerates_missing_inbound_address():
    # A native MsgDeposit has no inbound vault; the empty field is legitimate.
    p = prepare_swap(
        thorchain=FakeThor(chain="BTC", quote=make_quote(inbound_address="")),
        adapter=_native_adapter("THOR"),
        request=make_request(from_asset="THOR.RUNE"),
        now=0,
        mnemonic="m",
    )
    assert p.safe


def test_prepare_surfaces_adapter_problems():
    p = prepare(adapter=FakeAdapter(problems=["vault mismatch"]))
    assert not p.safe


def test_prepare_threads_streaming_params_into_quote():
    captured = {}

    class CapturingThor(FakeThor):
        def quote_swap(self, *args, **kwargs):
            captured.update(kwargs)
            return self._quote

    prepare_swap(
        thorchain=CapturingThor(),
        adapter=FakeAdapter(),
        request=make_request(),
        now=0,
        streaming_interval=2,
        streaming_quantity=0,
        mnemonic="m",
    )
    assert captured.get("streaming_interval") == 2
    assert captured.get("streaming_quantity") == 0


def test_prepare_streaming_drops_tolerance_limit():
    # Streaming manages slippage itself, so prepare_swap must send tolerance_bps
    # as None (LIM=0) rather than a tight limit that would defeat streaming.
    captured = {}

    class CapturingThor(FakeThor):
        def quote_swap(self, *args, **kwargs):
            captured.update(kwargs)
            return self._quote

    prepare_swap(
        thorchain=CapturingThor(),
        adapter=FakeAdapter(),
        request=make_request(),
        now=0,
        tolerance_bps=300,
        streaming_interval=1,
        mnemonic="m",
    )
    assert captured.get("streaming_interval") == 1
    assert captured.get("tolerance_bps") is None


def test_prepare_defaults_streaming_to_none():
    captured = {}

    class CapturingThor(FakeThor):
        def quote_swap(self, *args, **kwargs):
            captured.update(kwargs)
            return self._quote

    prepare_swap(
        thorchain=CapturingThor(),
        adapter=FakeAdapter(),
        request=make_request(),
        now=0,
        mnemonic="m",
    )
    # Passed through as None (the client turns None into "omit the param").
    assert captured.get("streaming_interval") is None
    assert captured.get("streaming_quantity") is None


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


# --- liquidity ---


def test_prepare_liquidity_uses_vault_and_memo():
    p = prepare_liquidity(
        thorchain=FakeThor(),
        adapter=FakeAdapter(),
        memo="+:BTC.BTC",
        amount=50000,
        now=0,
    )
    assert p.safe
    assert p.quote is None
    assert p.plan.inbound_address == VAULT
    assert p.plan.memo == "+:BTC.BTC"
    assert p.plan.amount == 50000


def test_prepare_liquidity_defaults_to_dust_when_amount_none():
    p = prepare_liquidity(
        thorchain=FakeThor(),
        adapter=FakeAdapter(),
        memo="-:BTC.BTC:10000",
        amount=None,
        now=0,
    )
    assert p.plan.amount == 1000  # dust_threshold from make_status


def test_prepare_liquidity_withdraw_allows_zero_dust_threshold():
    # dust_threshold == 0 is legitimate on EVM chains (Maya reports "0" for
    # ETH/ARB/KUJI/THOR — verified live): a 0-value native trigger tx IS the
    # withdraw mechanism there. prepare_liquidity must NOT treat it as degraded,
    # or every EVM LP withdraw would abort and lock the position.
    p = prepare_liquidity(
        thorchain=FakeThor(dust_threshold=0),
        adapter=FakeAdapter(),
        memo="-:ETH.ETH:10000",
        amount=None,
        now=0,
    )
    assert p.plan.amount == 0


def test_prepare_liquidity_aborts_when_halted():
    with pytest.raises(SwapAborted):
        prepare_liquidity(
            thorchain=FakeThor(tradable=False),
            adapter=FakeAdapter(),
            memo="+:BTC.BTC",
            amount=1,
            now=0,
        )


def test_prepare_liquidity_add_aborts_when_lp_paused_globally():
    # THORChain refunds LP adds while PAUSELP=1; abort before wasting gas.
    with pytest.raises(SwapAborted, match="paused"):
        prepare_liquidity(
            thorchain=FakeThor(mimir={"PAUSELP": 1}),
            adapter=FakeAdapter(),
            memo="+:BTC.BTC",
            amount=50000,
            now=0,
        )


def test_prepare_liquidity_add_aborts_when_pool_deposit_paused():
    with pytest.raises(SwapAborted, match="paused"):
        prepare_liquidity(
            thorchain=FakeThor(mimir={"PAUSELPDEPOSIT-BTC-BTC": 1}),
            adapter=FakeAdapter(),
            memo="+:BTC.BTC",
            amount=50000,
            now=0,
        )


def test_prepare_liquidity_withdraw_allowed_when_lp_paused():
    # Withdrawals must still work so LPs can exit even when deposits are paused.
    p = prepare_liquidity(
        thorchain=FakeThor(mimir={"PAUSELP": 1}),
        adapter=FakeAdapter(),
        memo="-:BTC.BTC:10000",
        amount=None,
        now=0,
    )
    assert p.plan.amount == 1000
