"""Tests for the bitcoinlib-backed BtcAdapter.

The build path is the safety-critical one: a constructed (unsigned) swap tx must
pass the same verify gate that guards broadcasting, and must sign across the
distinct derivation paths of its inputs. Skipped if bitcoinlib is not installed.
"""

import pytest

pytest.importorskip("bitcoinlib")

from cryptoswap.chains.btc import BtcAdapter  # noqa: E402
from cryptoswap.chains.coins import Utxo  # noqa: E402
from cryptoswap.verify import SwapPlan, verify_btc_swap  # noqa: E402

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
PATH = "m/84'/0'/0'/0/0"
EXPECTED_ADDR = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
VAULT = "bc1qct4mxayrdy96d4py20l4u02mu06r667f42p9fp"
MEMO = "=:ETH.ETH:0x1111111111111111111111111111111111111111"


def test_derive_address_matches_bip84_vector():
    assert BtcAdapter().derive_address(MNEMONIC, PATH) == EXPECTED_ADDR


def test_built_swap_passes_verify_gate():
    a = BtcAdapter()
    addr = a.derive_address(MNEMONIC, PATH)
    utxos = [Utxo(txid="aa" * 32, vout=0, value=200000, address=addr, path=PATH)]
    built = a.build_unsigned_swap(
        mnemonic=MNEMONIC,
        utxos=utxos,
        vault_address=VAULT,
        amount=178100,
        memo=MEMO,
        fee_rate=2,
    )
    plan = SwapPlan(
        inbound_address=VAULT, amount=178100, memo=MEMO, expiry=9_999_999_999
    )
    problems = verify_btc_swap(
        built.outputs,
        fee=built.fee,
        plan=plan,
        owned_addresses={addr, built.change_address},
        now=0,
        max_fee=100_000,
    )
    assert problems == []


def test_signs_multiple_inputs_across_paths():
    a = BtcAdapter()
    path0, path1 = "m/84'/0'/0'/0/0", "m/84'/0'/0'/0/1"
    addr0 = a.derive_address(MNEMONIC, path0)
    addr1 = a.derive_address(MNEMONIC, path1)
    utxos = [
        Utxo(txid="aa" * 32, vout=0, value=120000, address=addr0, path=path0),
        Utxo(txid="bb" * 32, vout=0, value=120000, address=addr1, path=path1),
    ]
    built = a.build_unsigned_swap(
        mnemonic=MNEMONIC,
        utxos=utxos,
        vault_address=VAULT,
        amount=178100,
        memo=MEMO,
        fee_rate=2,
    )
    assert len(built.keys) == 2
    raw = a.sign(built)
    assert isinstance(raw, str) and len(raw) > 0
    assert built.tx.verify() is True


def test_generate_mnemonic_is_usable():
    from cryptoswap.chains.btc import generate_mnemonic

    mnemonic = generate_mnemonic()
    assert len(mnemonic.split()) == 12
    addr = BtcAdapter().derive_address(mnemonic, PATH)
    assert addr.startswith("bc1q")


def test_build_sweep_spends_all_with_no_change():
    from cryptoswap.chains.coins import sweep_amount

    a = BtcAdapter()
    p0, p1 = "m/84'/0'/0'/0/0", "m/84'/0'/0'/0/1"
    a0, a1 = a.derive_address(MNEMONIC, p0), a.derive_address(MNEMONIC, p1)
    utxos = [
        Utxo(txid="aa" * 32, vout=0, value=100000, address=a0, path=p0),
        Utxo(txid="bb" * 32, vout=0, value=100000, address=a1, path=p1),
    ]
    total = 200000
    send, _ = sweep_amount(total, len(utxos), 2, len(MEMO.encode()))
    built = a.build_unsigned_swap(
        mnemonic=MNEMONIC,
        utxos=utxos,
        vault_address=VAULT,
        amount=send,
        memo=MEMO,
        fee_rate=2,
        sweep=True,
    )
    assert built.fee == total - send
    non_data = [o for o in built.outputs if o.op_return_data is None]
    assert len(non_data) == 1  # only the vault output, no change
    assert non_data[0].address == VAULT
    assert non_data[0].value == send
    assert len(built.keys) == 2
    plan = SwapPlan(inbound_address=VAULT, amount=send, memo=MEMO, expiry=9_999_999_999)
    problems = verify_btc_swap(
        built.outputs,
        fee=built.fee,
        plan=plan,
        owned_addresses={a0, a1, built.change_address},
        now=0,
        max_fee=100_000,
    )
    assert problems == []


def test_parse_address_info_confirmed_and_pending():
    from cryptoswap.chains.btc import parse_address_info

    info = parse_address_info(
        {
            "chain_stats": {
                "funded_txo_sum": 5000,
                "spent_txo_sum": 1000,
                "tx_count": 2,
            },
            "mempool_stats": {
                "funded_txo_sum": 3000,
                "spent_txo_sum": 0,
                "tx_count": 1,
            },
        }
    )
    assert info.confirmed == 4000
    assert info.pending == 3000
    assert info.has_history is True


def test_parse_address_info_unused():
    from cryptoswap.chains.btc import parse_address_info

    info = parse_address_info(
        {
            "chain_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
            "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
        }
    )
    assert info.has_history is False
    assert info.confirmed == 0
    assert info.pending == 0


def _quote(memo, *, inbound=VAULT, expiry=9_999_999_999, min_in=1000):
    from cryptoswap.thorchain import Quote, SwapFees

    return Quote(
        inbound_address=inbound,
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


def test_btc_build_and_verify_clean():
    from cryptoswap.swap import SwapRequest

    a = BtcAdapter()
    addr = a.derive_address(MNEMONIC, PATH)
    utxos = [Utxo(txid="aa" * 32, vout=0, value=200000, address=addr, path=PATH)]
    dest = "0x1111111111111111111111111111111111111111"
    request = SwapRequest(
        from_asset="BTC.BTC", to_asset="ETH.ETH", amount=178100, destination=dest
    )
    prepared = a.build_and_verify(
        quote=_quote(f"=:e:{dest}:6700000"),
        request=request,
        now=0,
        mnemonic=MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=2,
        change_address=addr,
        max_fee=100000,
    )
    assert prepared.problems == []


def test_btc_build_and_verify_flags_wrong_destination():
    from cryptoswap.swap import SwapRequest

    a = BtcAdapter()
    addr = a.derive_address(MNEMONIC, PATH)
    utxos = [Utxo(txid="aa" * 32, vout=0, value=200000, address=addr, path=PATH)]
    request = SwapRequest(
        from_asset="BTC.BTC", to_asset="ETH.ETH", amount=178100, destination="0xmine"
    )
    prepared = a.build_and_verify(
        quote=_quote("=:e:0xsomeoneelse"),
        request=request,
        now=0,
        mnemonic=MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=2,
        change_address=addr,
        max_fee=100000,
    )
    assert not prepared.safe
