"""Tests for the chain-agnostic swap orchestrator (prepare_swap / execute_swap).

A fake adapter supplies build_and_verify so these exercise only the
chain-agnostic flow; the real build+verify integration lives in the per-chain
adapter tests.
"""

import dataclasses
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
from swapsack.thorchain import ChainStatus, PoolDepth, Quote, SwapFees

VAULT = "bc1qvault"
# Pool depths the symmetric-liquidity tests price against; defined up here
# because FakeThor.pool() serves every test in the file. 1000.0 asset (1e8)
# against 8850.0 CACAO (1e10) — the ~8.85 protocol-per-asset ratio Maya's USDC
# pools actually run at.
POOL_ASSET_DEPTH = 100_000_000_000
POOL_PROTOCOL_DEPTH = 88_500_000_000_000


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
        pool_depth=None,
        status=None,
    ):
        self._quote = quote or make_quote()
        self._status = status
        self._tradable = tradable
        self._chain = chain
        self._mimir = mimir or {}
        self._dust = dust_threshold
        self.path_prefix = path_prefix
        self._pool_depth = pool_depth

    def inbound_addresses(self):
        if self._status is not None:
            return {self._status.chain: self._status}
        return {self._chain: make_status(self._chain, self._tradable, self._dust)}

    def quote_swap(self, *args, **kwargs):
        return self._quote

    def mimir(self):
        return self._mimir

    def liquidity_provider(self, pool, address):
        return getattr(self, "lp_record", None)

    def constants(self):
        return getattr(self, "constants_payload", {"int_64_values": {}})

    def pool(self, asset):
        return self._pool_depth or PoolDepth(
            asset=asset,
            balance_asset=POOL_ASSET_DEPTH,
            balance_protocol=POOL_PROTOCOL_DEPTH,
        )


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


def test_prepare_explains_mayas_wording_of_a_tolerance_rejection():
    # Maya refuses the identical condition from its tx-out manager, in words
    # of its own: `(emitted/limit)` in 1e8 units of the output asset. Observed
    # live 2026-08-16 on a CACAO->ETH swap at the 300 bps default.
    thor = _RaisingThor(
        "failed to simulate swap: internal error\n"
        "* outbound amount does not meet requirements (2323009/2326360)",
        path_prefix="mayachain",
    )
    with pytest.raises(SwapAborted) as exc:
        prepare(thor=thor)
    msg = str(exc.value)
    assert msg.startswith("Maya rejected the quote")
    assert "--tolerance-bps" in msg
    # limit = feeless * (10000 - 300) / 10000, so clearing this quote takes
    # ceil(10000 - 9700 * 2323009 / 2326360) = 314 bps.
    assert "--tolerance-bps 314;" in msg


def test_prepare_names_the_tolerance_that_clears_a_thorchain_rejection():
    thor = _RaisingThor(
        "failed to simulate swap: emit asset 2425906900 less than price "
        "limit 2707570991: invalid request"
    )
    with pytest.raises(SwapAborted) as exc:
        prepare(thor=thor)
    msg = str(exc.value)
    assert msg.startswith("THORChain rejected the quote")
    # ceil(10000 - 9700 * 2425906900 / 2707570991) = 1310
    assert "--tolerance-bps 1310;" in msg


def test_prepare_names_maya_for_an_unrelated_maya_rejection():
    thor = _RaisingThor("pool suspended", path_prefix="mayachain")
    with pytest.raises(SwapAborted) as exc:
        prepare(thor=thor)
    assert str(exc.value) == "Maya rejected the quote: pool suspended"


def test_prepare_does_not_read_any_number_pair_as_slippage():
    # Only the tolerance wordings are explained; a bare `(a/b)` elsewhere in
    # an unrelated failure must not be dressed up as a slippage rejection.
    thor = _RaisingThor(
        "failed to simulate swap: internal error\n* fail to prepare (12/34)",
        path_prefix="mayachain",
    )
    with pytest.raises(SwapAborted) as exc:
        prepare(thor=thor)
    assert "tolerance" not in str(exc.value)


def _maya_abort(call):
    with pytest.raises(SwapAborted) as exc:
        call()
    msg = str(exc.value)
    assert "Maya" in msg
    assert "THORChain" not in msg
    return msg


def test_prepare_names_maya_when_the_chain_is_not_tradable():
    _maya_abort(lambda: prepare(thor=FakeThor(tradable=False, path_prefix="mayachain")))


def test_prepare_names_maya_when_the_quote_has_no_memo():
    thor = FakeThor(quote=make_quote(memo=None), path_prefix="mayachain")
    _maya_abort(lambda: prepare(thor=thor))


def test_prepare_liquidity_names_maya_when_the_chain_is_not_tradable():
    _maya_abort(
        lambda: prepare_liquidity(
            thorchain=FakeThor(tradable=False, path_prefix="mayachain"),
            adapter=FakeAdapter(),
            memo="+:BTC.BTC",
            amount=1,
            now=0,
        )
    )


def test_prepare_liquidity_names_maya_when_lp_is_paused():
    _maya_abort(
        lambda: prepare_liquidity(
            thorchain=FakeThor(mimir={"PAUSELP": 1}, path_prefix="mayachain"),
            adapter=FakeAdapter(),
            memo="+:BTC.BTC",
            amount=50000,
            now=0,
        )
    )


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


# --- symmetric (two-sided) liquidity ---------------------------------------
#
# The money-sensitive part is the *coordination*, not the arithmetic (that lives
# in test_liquidity.py): both legs must be built and gated before either is
# broadcast, and a failure after the first leg is out must be reported as a
# pending position rather than swallowed. These fakes stand in for an
# account-model asset adapter (ETH) and a Cosmos protocol adapter (Maya/THOR).


class FakeCosmosAdapter:
    """Stands in for MayaAdapter/ThorAdapter (the protocol leg)."""

    symbol = "CACAO"
    decimals = 10

    def __init__(self, chain="MAYA", asset="MAYA.CACAO", problems=None, balance=10**20):
        self.chain = chain
        self.asset = asset
        self._problems = problems or []
        self._balance = balance
        self.built = None
        self.broadcasted = False
        self.broadcast_error = None

    def fetch_balance(self, address):
        return self._balance

    def build_and_verify_native_deposit(self, *, memo, amount, mnemonic, now, **kw):
        self.built = SimpleNamespace(memo=memo, amount=amount)
        plan = SimpleNamespace(memo=memo, amount=amount, expiry=now + 3600)
        return Prepared(
            quote=None,
            built=SimpleNamespace(fee=0),
            plan=plan,
            problems=list(self._problems),
        )

    def sign(self, built):
        return ["cafe"]

    def broadcast(self, raws):
        if self.broadcast_error is not None:
            raise self.broadcast_error
        self.broadcasted = True
        return "protocol_txid"


class FakeAssetAdapter(FakeAdapter):
    """FakeAdapter that records the deposit build and can fail its broadcast."""

    def __init__(self, chain="ETH", problems=None):
        super().__init__(chain=chain, problems=problems)
        self.built = None
        self.broadcast_error = None

    def build_and_verify_deposit(self, *, vault, memo, amount, now, **kwargs):
        self.built = SimpleNamespace(vault=vault, memo=memo, amount=amount, kw=kwargs)
        return super().build_and_verify_deposit(
            vault=vault, memo=memo, amount=amount, now=now, **kwargs
        )

    def broadcast(self, raws):
        if self.broadcast_error is not None:
            raise self.broadcast_error
        return "asset_txid"


ETH_ADDR = "0xB0b"
MAYA_ADDR = "maya1abc"


def make_symmetric(
    thor=None, asset_adapter=None, protocol_adapter=None, asset_amount=10_000_000_000
):
    from swapsack.swap import prepare_symmetric_liquidity

    return prepare_symmetric_liquidity(
        thorchain=thor or FakeThor(chain="ETH"),
        asset_adapter=asset_adapter or FakeAssetAdapter(),
        protocol_adapter=protocol_adapter or FakeCosmosAdapter(),
        pool="ETH.USDC-0XA0B8",
        asset_amount=asset_amount,
        asset_address=ETH_ADDR,
        protocol_address=MAYA_ADDR,
        mnemonic="x " * 12,
        now=0,
    )


def test_symmetric_legs_cross_reference_each_others_address():
    # The crux of the pairing: each leg's memo names the OTHER side's address.
    asset_adapter, protocol_adapter = FakeAssetAdapter(), FakeCosmosAdapter()
    p = make_symmetric(asset_adapter=asset_adapter, protocol_adapter=protocol_adapter)
    assert p.asset_memo == f"+:ETH.USDC-0XA0B8:{MAYA_ADDR}"
    assert p.protocol_memo == f"+:ETH.USDC-0XA0B8:{ETH_ADDR}"
    assert asset_adapter.built.memo == p.asset_memo
    assert protocol_adapter.built.memo == p.protocol_memo
    assert asset_adapter.built.vault == VAULT


def test_symmetric_protocol_amount_comes_from_the_pool_ratio():
    p = make_symmetric()
    # 100 asset units at 8.85 protocol-per-asset, protocol side native (1e10).
    assert p.protocol_amount == 10_000_000_000 * POOL_PROTOCOL_DEPTH // POOL_ASSET_DEPTH
    assert p.asset_amount == 10_000_000_000


def test_symmetric_gates_the_protocol_leg_against_the_lp_pause():
    # The regression this exists for: build_and_verify_native_deposit has no
    # vault and so never consults mimir on its own. A paused pool must stop the
    # symmetric add before EITHER leg is built.
    asset_adapter, protocol_adapter = FakeAssetAdapter(), FakeCosmosAdapter()
    with pytest.raises(SwapAborted, match="paused"):
        make_symmetric(
            thor=FakeThor(chain="ETH", mimir={"PAUSELP": 1}),
            asset_adapter=asset_adapter,
            protocol_adapter=protocol_adapter,
        )
    assert asset_adapter.built is None
    assert protocol_adapter.built is None


def test_symmetric_lp_pause_names_the_network():
    thor = FakeThor(chain="ETH", mimir={"PAUSELP": 1}, path_prefix="mayachain")
    with pytest.raises(SwapAborted, match="^Maya has LP deposits paused"):
        make_symmetric(thor=thor)


def test_symmetric_aborts_when_the_asset_chain_is_halted():
    with pytest.raises(SwapAborted):
        make_symmetric(thor=FakeThor(chain="ETH", tradable=False))


def _maya_global_halt(chain):
    # What Maya's inbound_addresses showed under HALTCHAINGLOBAL=1 on
    # 2026-09-24: every chain halted and trading-paused, global flag absent.
    return dataclasses.replace(
        make_status(chain), halted=True, chain_trading_paused=True
    )


_HALT_GATES = {
    "swap": lambda thor: prepare(thor=thor, adapter=FakeAdapter(chain="ARB")),
    "liquidity": lambda thor: prepare_liquidity(
        thorchain=thor,
        adapter=FakeAdapter(chain="ARB"),
        memo="+:ARB.ETH",
        amount=1,
        now=0,
    ),
    "symmetric": lambda thor: make_symmetric(thor=thor),
}


@pytest.mark.parametrize("gate", sorted(_HALT_GATES))
def test_halt_abort_names_the_backend_and_the_flags(gate):
    # A Maya halt used to be reported as "not currently tradable on THORChain",
    # sending the user to check the wrong network, with no hint of why.
    chain = "ETH" if gate == "symmetric" else "ARB"
    thor = FakeThor(path_prefix="mayachain", status=_maya_global_halt(chain))
    with pytest.raises(SwapAborted) as err:
        _HALT_GATES[gate](thor)
    msg = str(err.value)
    assert f"{chain} is not currently tradable on Maya" in msg
    assert "THORChain" not in msg
    assert "chain halted" in msg and "chain trading paused" in msg


@pytest.mark.parametrize("gate", sorted(_HALT_GATES))
def test_unlisted_chain_abort_says_it_is_unlisted(gate):
    thor = FakeThor(path_prefix="mayachain", chain="NOPE")
    with pytest.raises(SwapAborted, match="not listed by Maya"):
        _HALT_GATES[gate](thor)


def test_symmetric_aborts_when_protocol_balance_is_short():
    # Broadcasting a leg we know will bounce is worse than refusing up front.
    protocol_adapter = FakeCosmosAdapter(balance=1)
    with pytest.raises(SwapAborted, match="CACAO"):
        make_symmetric(protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_symmetric_is_unsafe_and_labels_which_leg_failed():
    p = make_symmetric(asset_adapter=FakeAssetAdapter(problems=["bad vault"]))
    assert not p.safe
    assert p.problems == ["asset leg: bad vault"]
    p = make_symmetric(protocol_adapter=FakeCosmosAdapter(problems=["bad memo"]))
    assert not p.safe
    assert p.problems == ["protocol leg: bad memo"]


def test_symmetric_execute_broadcasts_protocol_leg_first():
    from swapsack.swap import execute_symmetric_liquidity

    order = []
    asset_adapter, protocol_adapter = FakeAssetAdapter(), FakeCosmosAdapter()
    for adapter, name in ((asset_adapter, "asset"), (protocol_adapter, "protocol")):
        original = adapter.broadcast
        adapter.broadcast = lambda raws, _o=original, _n=name: (
            order.append(_n),
            _o(raws),
        )[1]
    p = make_symmetric(asset_adapter=asset_adapter, protocol_adapter=protocol_adapter)
    result = execute_symmetric_liquidity(
        p, asset_adapter=asset_adapter, protocol_adapter=protocol_adapter, confirm=True
    )
    # Protocol first: it is native, cheap and fast, so the expensive/slow leg is
    # the one we are still free to abandon if the first fails.
    assert order == ["protocol", "asset"]
    assert result.protocol_txid == "protocol_txid"
    assert result.asset_txid == "asset_txid"
    assert result.broadcast


def test_symmetric_execute_dry_run_broadcasts_neither():
    from swapsack.swap import execute_symmetric_liquidity

    asset_adapter, protocol_adapter = FakeAssetAdapter(), FakeCosmosAdapter()
    p = make_symmetric(asset_adapter=asset_adapter, protocol_adapter=protocol_adapter)
    result = execute_symmetric_liquidity(
        p, asset_adapter=asset_adapter, protocol_adapter=protocol_adapter, confirm=False
    )
    assert not result.broadcast
    assert result.protocol_txid is None and result.asset_txid is None
    assert not protocol_adapter.broadcasted


def test_symmetric_execute_refuses_when_either_gate_failed():
    from swapsack.swap import execute_symmetric_liquidity

    protocol_adapter = FakeCosmosAdapter()
    asset_adapter = FakeAssetAdapter(problems=["bad vault"])
    p = make_symmetric(asset_adapter=asset_adapter, protocol_adapter=protocol_adapter)
    with pytest.raises(SwapAborted):
        execute_symmetric_liquidity(
            p,
            asset_adapter=asset_adapter,
            protocol_adapter=protocol_adapter,
            confirm=True,
        )
    assert not protocol_adapter.broadcasted


def test_symmetric_execute_reports_a_half_add_loudly():
    # The failure mode the whole design exists to handle: the protocol leg is
    # irreversibly out and the asset leg did not go. The caller must be handed
    # the live txid, not a bare broadcast error.
    from swapsack.swap import (
        BroadcastError,
        PartialSymmetricAdd,
        execute_symmetric_liquidity,
    )

    asset_adapter, protocol_adapter = FakeAssetAdapter(), FakeCosmosAdapter()
    asset_adapter.broadcast_error = BroadcastError("rpc rejected")
    p = make_symmetric(asset_adapter=asset_adapter, protocol_adapter=protocol_adapter)
    with pytest.raises(PartialSymmetricAdd) as excinfo:
        execute_symmetric_liquidity(
            p,
            asset_adapter=asset_adapter,
            protocol_adapter=protocol_adapter,
            confirm=True,
        )
    assert excinfo.value.protocol_txid == "protocol_txid"
    assert "rpc rejected" in str(excinfo.value)


def test_symmetric_execute_leaves_nothing_out_when_the_first_leg_fails():
    # Protocol leg fails => nothing is live; this is the benign failure and must
    # NOT be reported as a partial add.
    from swapsack.swap import (
        BroadcastError,
        PartialSymmetricAdd,
        execute_symmetric_liquidity,
    )

    asset_adapter, protocol_adapter = FakeAssetAdapter(), FakeCosmosAdapter()
    protocol_adapter.broadcast_error = BroadcastError("nope")
    p = make_symmetric(asset_adapter=asset_adapter, protocol_adapter=protocol_adapter)
    with pytest.raises(BroadcastError) as excinfo:
        execute_symmetric_liquidity(
            p,
            asset_adapter=asset_adapter,
            protocol_adapter=protocol_adapter,
            confirm=True,
        )
    assert not isinstance(excinfo.value, PartialSymmetricAdd)


# --- CACAO-only (protocol-side) liquidity and the CACAO Pool -----------------
#
# Both are a single native MsgDeposit, so the money-sensitive parts are the
# checks that must refuse *before* it is built: a paused or staged pool, a
# disabled CACAO Pool, a short balance, an immature deposit.


def make_protocol_only(thor=None, protocol_adapter=None, amount=10**12):
    from swapsack.swap import prepare_protocol_only_liquidity

    return prepare_protocol_only_liquidity(
        thorchain=thor or FakeThor(chain="ETH", path_prefix="mayachain"),
        protocol_adapter=protocol_adapter or FakeCosmosAdapter(),
        pool="BTC.BTC",
        amount=amount,
        protocol_address=MAYA_ADDR,
        mnemonic="x " * 12,
        now=0,
    )


def test_protocol_only_add_is_a_plain_add_memo_from_the_protocol_side():
    # No paired address: the handler files it under the CACAO address with an
    # empty asset address, and a withdraw then pays out entirely in CACAO.
    protocol_adapter = FakeCosmosAdapter()
    p, existing = make_protocol_only(
        protocol_adapter=protocol_adapter, amount=5 * 10**10
    )
    assert existing is None
    assert protocol_adapter.built.memo == "+:BTC.BTC"
    assert protocol_adapter.built.amount == 5 * 10**10
    assert p.safe


def test_protocol_only_add_refuses_a_paused_pool():
    protocol_adapter = FakeCosmosAdapter()
    thor = FakeThor(chain="ETH", mimir={"PAUSELPBTC": 1}, path_prefix="mayachain")
    with pytest.raises(SwapAborted, match="^Maya has LP deposits paused"):
        make_protocol_only(thor=thor, protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_protocol_only_add_refuses_a_staged_pool():
    # handler_add_liquidity.go: "cannot add single sided liquidity while a pool
    # is staged" — it would be refunded minus the fee.
    protocol_adapter = FakeCosmosAdapter()
    staged = PoolDepth(
        asset="BTC.BTC", balance_asset=1, balance_protocol=1, status="Staged"
    )
    thor = FakeThor(chain="ETH", path_prefix="mayachain", pool_depth=staged)
    with pytest.raises(SwapAborted, match="Staged"):
        make_protocol_only(thor=thor, protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_protocol_only_add_refuses_a_short_balance():
    protocol_adapter = FakeCosmosAdapter(balance=10**10)
    with pytest.raises(SwapAborted, match="holds 1.00000000"):
        make_protocol_only(protocol_adapter=protocol_adapter, amount=2 * 10**10)
    assert protocol_adapter.built is None


class FakeMayaPool(FakeThor):
    """A Maya client that also answers the CACAO Pool endpoints."""

    def __init__(self, *, provider=None, height=20_000_000, enabled=1, **kw):
        mimir = {"CACAOPOOLENABLED": enabled, "CACAOPOOLDEPOSITMATURITYBLOCKS": 302400}
        mimir.update(kw.pop("mimir", {}))
        super().__init__(path_prefix="mayachain", mimir=mimir, **kw)
        self._provider = provider
        self._height = height

    def pool_provider(self, address):
        return self._provider

    def last_height(self):
        return self._height


def _provider(last_deposit_height=19_000_000, units=10**12):
    from swapsack.thorchain import PoolProviderPosition

    return PoolProviderPosition(
        address=MAYA_ADDR,
        units=units,
        value=2 * 10**12,
        pnl=10**11,
        deposit_amount=19 * 10**11,
        last_deposit_height=last_deposit_height,
    )


def make_pool_deposit(thor=None, protocol_adapter=None, amount=10**12):
    from swapsack.swap import prepare_pool_deposit

    return prepare_pool_deposit(
        thorchain=thor or FakeMayaPool(),
        protocol_adapter=protocol_adapter or FakeCosmosAdapter(),
        amount=amount,
        address=MAYA_ADDR,
        mnemonic="x " * 12,
        now=0,
    )


def make_pool_withdraw(thor=None, protocol_adapter=None, basis_points=10000):
    from swapsack.swap import prepare_pool_withdraw

    return prepare_pool_withdraw(
        thorchain=thor or FakeMayaPool(provider=_provider()),
        protocol_adapter=protocol_adapter or FakeCosmosAdapter(),
        basis_points=basis_points,
        address=MAYA_ADDR,
        mnemonic="x " * 12,
        now=0,
    )


def test_cacao_pool_deposit_builds_a_pool_plus_deposit():
    protocol_adapter = FakeCosmosAdapter()
    p = make_pool_deposit(protocol_adapter=protocol_adapter, amount=7 * 10**10)
    assert protocol_adapter.built.memo == "pool+"
    assert protocol_adapter.built.amount == 7 * 10**10
    assert p.safe


def test_cacao_pool_deposit_refuses_when_disabled():
    protocol_adapter = FakeCosmosAdapter()
    with pytest.raises(SwapAborted, match="CACAOPOOLENABLED"):
        make_pool_deposit(
            thor=FakeMayaPool(enabled=0), protocol_adapter=protocol_adapter
        )
    assert protocol_adapter.built is None


def test_rune_pool_reads_its_own_mimir_keys():
    # Each network has its own switch: a CACAO key says nothing about RUNEPool.
    protocol_adapter = FakeCosmosAdapter()
    thor = FakeThor(chain="ETH", path_prefix="thorchain", mimir={"CACAOPOOLENABLED": 1})
    with pytest.raises(SwapAborted, match="RUNEPOOLENABLED"):
        make_pool_deposit(thor=thor, protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


class FakeThorPool(FakeMayaPool):
    """A THORChain client that answers the RUNEPool endpoints."""

    def __init__(self, *, mimir=None, **kw):
        super().__init__(**kw)
        self.path_prefix = "thorchain"
        self._mimir = {"RUNEPOOLENABLED": 1, "RUNEPOOLDEPOSITMATURITYBLOCKS": 432000}
        self._mimir.update(mimir or {})


def test_rune_pool_deposit_builds_a_pool_plus_deposit():
    protocol_adapter = FakeCosmosAdapter(chain="THOR", asset="THOR.RUNE")
    p = make_pool_deposit(
        thor=FakeThorPool(), protocol_adapter=protocol_adapter, amount=10**8
    )
    assert protocol_adapter.built.memo == "pool+"
    assert p.safe


def test_rune_pool_deposit_refuses_while_deposits_are_halted():
    # handler_rune_pool_deposit.go: "RUNEPool deposit paused".
    protocol_adapter = FakeCosmosAdapter()
    thor = FakeThorPool(mimir={"RUNEPOOLHALTDEPOSIT": 1})
    with pytest.raises(SwapAborted, match="RUNEPOOLHALTDEPOSIT"):
        make_pool_deposit(thor=thor, protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_rune_pool_withdraw_refuses_while_withdrawals_are_halted():
    protocol_adapter = FakeCosmosAdapter()
    thor = FakeThorPool(provider=_provider(), mimir={"RUNEPOOLHALTWITHDRAW": 1})
    with pytest.raises(SwapAborted, match="RUNEPOOLHALTWITHDRAW"):
        make_pool_withdraw(thor=thor, protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_rune_pool_withdraw_uses_the_runepool_maturity():
    # 432000 blocks on THORChain, not Maya's 302400.
    thor = FakeThorPool(
        provider=_provider(last_deposit_height=20_000_000 - 400_000),
        height=20_000_000,
    )
    with pytest.raises(SwapAborted, match="32000 more blocks"):
        make_pool_withdraw(thor=thor)


def test_cacao_pool_deposit_refuses_a_short_balance():
    protocol_adapter = FakeCosmosAdapter(balance=10)
    with pytest.raises(SwapAborted, match="holds"):
        make_pool_deposit(protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_cacao_pool_withdraw_deposits_zero_with_the_withdraw_memo():
    protocol_adapter = FakeCosmosAdapter()
    prepared, position = make_pool_withdraw(
        protocol_adapter=protocol_adapter, basis_points=2500
    )
    assert protocol_adapter.built.memo == "pool-:2500"
    assert protocol_adapter.built.amount == 0
    assert position.units == 10**12
    assert prepared.safe


def test_cacao_pool_withdraw_refuses_without_a_position():
    protocol_adapter = FakeCosmosAdapter()
    with pytest.raises(SwapAborted, match="no CACAO Pool position"):
        make_pool_withdraw(
            thor=FakeMayaPool(provider=None), protocol_adapter=protocol_adapter
        )
    assert protocol_adapter.built is None


def test_cacao_pool_withdraw_refuses_an_immature_deposit():
    # The handler refuses until last_deposit_height + maturity, and the fee
    # is spent anyway: say how long is left instead.
    protocol_adapter = FakeCosmosAdapter()
    thor = FakeMayaPool(
        provider=_provider(last_deposit_height=19_900_000), height=20_000_000
    )
    with pytest.raises(SwapAborted, match="202400 more blocks"):
        make_pool_withdraw(thor=thor, protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_cacao_pool_withdraw_allows_a_deposit_exactly_at_maturity():
    thor = FakeMayaPool(
        provider=_provider(last_deposit_height=20_000_000 - 302400),
        height=20_000_000,
    )
    prepared, _ = make_pool_withdraw(thor=thor)
    assert prepared.safe


def test_cacao_pool_withdraw_refuses_when_disabled():
    # Unlike an LP pause, CACAOPOOLENABLED gates the withdraw handler too
    # (validateV129): a disabled pool refuses both directions.
    protocol_adapter = FakeCosmosAdapter()
    with pytest.raises(SwapAborted, match="CACAOPOOLENABLED"):
        make_pool_withdraw(
            thor=FakeMayaPool(provider=_provider(), enabled=0),
            protocol_adapter=protocol_adapter,
        )
    assert protocol_adapter.built is None


def _lp_record(**over):
    from swapsack.thorchain import LiquidityPosition

    fields = dict(
        pool="BTC.BTC",
        asset_address="bc1qours",
        units=1000,
        asset_redeem_value=10,
        pending_asset=0,
        protocol_redeem_value=10,
    )
    fields.update(over)
    return LiquidityPosition(**fields)


def test_protocol_only_add_refuses_against_a_pending_record():
    # handler_add_liquidity.go: a record with units 0 and a pending tx pins its
    # asset address, so a CACAO-only add (empty asset address) is refused with
    # errAddLiquidityMismatchAddr — refunded minus the fee.
    protocol_adapter = FakeCosmosAdapter()
    thor = FakeThor(chain="ETH", path_prefix="mayachain")
    thor.lp_record = _lp_record(units=0, pending_protocol=5)
    with pytest.raises(SwapAborted, match="pending"):
        make_protocol_only(thor=thor, protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_protocol_only_add_reports_the_symmetric_record_it_joins():
    # The record is keyed by the CACAO address, so the CACAO merges into an
    # existing symmetric position and withdraws proportionally with it.
    thor = FakeThor(chain="ETH", path_prefix="mayachain")
    thor.lp_record = _lp_record()
    _, existing = make_protocol_only(thor=thor)
    assert existing is not None and existing.asset_address == "bc1qours"


def test_pool_maturity_falls_back_to_the_constant():
    # No mimir override: the node uses its constant (Maya: 14400), not 0.
    thor = FakeMayaPool(
        provider=_provider(last_deposit_height=20_000_000 - 100), height=20_000_000
    )
    del thor._mimir["CACAOPOOLDEPOSITMATURITYBLOCKS"]
    thor.constants_payload = {
        "int_64_values": {"CACAOPoolDepositMaturityBlocks": 14400}
    }
    with pytest.raises(SwapAborted, match="14300 more blocks"):
        make_pool_withdraw(thor=thor)


def test_pool_maturity_unknown_refuses():
    protocol_adapter = FakeCosmosAdapter()
    thor = FakeMayaPool(provider=_provider())
    del thor._mimir["CACAOPOOLDEPOSITMATURITYBLOCKS"]
    with pytest.raises(SwapAborted, match="maturity"):
        make_pool_withdraw(thor=thor, protocol_adapter=protocol_adapter)
    assert protocol_adapter.built is None


def test_pool_maturity_blocks_reads_mimir_then_constants():
    from swapsack.swap import pool_maturity_blocks

    assert pool_maturity_blocks(FakeMayaPool()) == 302400
    thor = FakeThorPool()
    del thor._mimir["RUNEPOOLDEPOSITMATURITYBLOCKS"]
    thor.constants_payload = {
        "int_64_values": {"RUNEPoolDepositMaturityBlocks": 1296000}
    }
    assert pool_maturity_blocks(thor) == 1296000


def test_rune_pool_halt_scheduled_for_a_later_height_is_not_yet_a_halt():
    # thornode: halted only when value > 0 and height >= value.
    thor = FakeThorPool(mimir={"RUNEPOOLHALTDEPOSIT": 20_000_100}, height=20_000_000)
    p = make_pool_deposit(thor=thor)
    assert p.safe


def test_rune_pool_halt_applies_from_its_height():
    thor = FakeThorPool(mimir={"RUNEPOOLHALTDEPOSIT": 20_000_000}, height=20_000_000)
    with pytest.raises(SwapAborted, match="halted since block 20000000"):
        make_pool_deposit(thor=thor)
