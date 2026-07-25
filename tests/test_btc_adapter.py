"""Tests for the bitcoinlib-backed BtcAdapter.

The build path is the safety-critical one: a constructed (unsigned) swap tx must
pass the same verify gate that guards broadcasting, and must sign across the
distinct derivation paths of its inputs. Skipped if bitcoinlib is not installed.
"""

import pytest

pytest.importorskip("bitcoinlib")

from swapsack.chains.btc import BtcAdapter  # noqa: E402
from swapsack.chains.coins import Utxo  # noqa: E402
from swapsack.verify import SwapPlan, verify_btc_swap  # noqa: E402

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


def test_testnet_network_derives_testnet_address():
    # A testnet adapter derives a tb1 (bech32 testnet) address, not bc1 mainnet.
    addr = BtcAdapter(network="testnet").derive_address(MNEMONIC, PATH)
    assert addr.startswith("tb1")
    assert BtcAdapter().network == "bitcoin"  # default stays mainnet


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
    raws = a.sign(built)
    assert len(raws) == 1 and isinstance(raws[0], str)
    assert built.tx.verify() is True


def test_sign_refuses_a_half_signed_tx():
    # M3: if the signer leaves an input unsigned (e.g. a key didn't match and
    # fail_on_unknown_key swallowed it), sign() must refuse rather than emit a
    # tx that only gets rejected at broadcast. Simulate by no-op'ing the signer.
    a = BtcAdapter()
    utxos = [
        Utxo(txid="aa" * 32, vout=0, value=200000, address=EXPECTED_ADDR, path=PATH)
    ]
    built = a.build_unsigned_swap(
        mnemonic=MNEMONIC,
        utxos=utxos,
        vault_address=VAULT,
        amount=150000,
        memo=MEMO,
        fee_rate=2,
    )
    built.tx.sign = lambda *args, **kwargs: None  # signer does nothing
    with pytest.raises(RuntimeError, match="unsigned"):
        a.sign(built)


def test_generate_mnemonic_is_usable():
    from swapsack.chains.btc import generate_mnemonic

    mnemonic = generate_mnemonic()
    assert len(mnemonic.split()) == 12
    addr = BtcAdapter().derive_address(mnemonic, PATH)
    assert addr.startswith("bc1q")


def test_build_sweep_spends_all_with_no_change():
    from swapsack.chains.coins import sweep_amount

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


def test_btc_build_and_verify_send_clean_and_signs():
    a = BtcAdapter()
    addr = a.derive_address(MNEMONIC, PATH)
    recipient = a.derive_address(MNEMONIC, "m/84'/0'/0'/0/9")  # a valid external addr
    utxos = [Utxo(txid="aa" * 32, vout=0, value=200000, address=addr, path=PATH)]
    prepared = a.build_and_verify_send(
        recipient=recipient,
        amount=100000,
        now=0,
        mnemonic=MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=2,
        change_address=addr,
        max_fee=100000,
    )
    assert prepared.problems == []
    # A plain send carries no OP_RETURN/memo output.
    assert all(o.op_return_data is None for o in prepared.built.outputs)
    pays = [o for o in prepared.built.outputs if o.address == recipient]
    assert len(pays) == 1 and pays[0].value == 100000
    raws = a.sign(prepared.built)
    assert len(raws) == 1 and prepared.built.tx.verify() is True


def test_btc_send_sweep_spends_all_no_change():
    from swapsack.chains.coins import sweep_amount

    a = BtcAdapter()
    addr = a.derive_address(MNEMONIC, PATH)
    recipient = a.derive_address(MNEMONIC, "m/84'/0'/0'/0/9")
    utxos = [Utxo(txid="aa" * 32, vout=0, value=150000, address=addr, path=PATH)]
    send, _ = sweep_amount(150000, len(utxos), 2, memo_len=0)
    prepared = a.build_and_verify_send(
        recipient=recipient,
        amount=send,
        now=0,
        mnemonic=MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=2,
        change_address=addr,
        max_fee=100000,
        sweep=True,
    )
    assert prepared.problems == []
    assert prepared.built.fee == 150000 - send
    non_data = [o for o in prepared.built.outputs if o.op_return_data is None]
    assert len(non_data) == 1 and non_data[0].address == recipient


def test_btc_send_change_returns_to_the_wallets_change_address():
    """A partial (non-sweep) send must return the remainder to OUR change path.

    The live BTC loop in ``test_integration_testnet.py`` only ever sweeps, so
    the change output — which on a 10%-of-balance send carries the other 90% —
    is exercised nowhere else. The verify gate cannot catch a wrong change
    address on its own: ``GatedTxBuilder`` puts ``change_address`` into the
    owned set by construction, so a bad path would be self-certifying. Pin it
    against an independently derived address instead.
    """
    from swapsack.chains.btc import CHANGE_PATH

    a = BtcAdapter()
    addr = a.derive_address(MNEMONIC, PATH)
    recipient = a.derive_address(MNEMONIC, "m/84'/0'/0'/0/9")
    change_address = a.derive_address(MNEMONIC, CHANGE_PATH)
    assert change_address != addr  # a real second address, not the receive one
    utxos = [Utxo(txid="aa" * 32, vout=0, value=1_000_000, address=addr, path=PATH)]

    prepared = a.build_and_verify_send(
        recipient=recipient,
        amount=100_000,  # 10% of the UTXO; 90% must come back as change
        now=0,
        mnemonic=MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=2,
        change_address=change_address,
        max_fee=50_000,  # the CLI default
        sweep=False,
    )
    assert prepared.problems == []

    outs = [o for o in prepared.built.outputs if o.op_return_data is None]
    assert len(outs) == 2  # recipient + change, nothing else
    change_outs = [o for o in outs if o.address != recipient]
    assert len(change_outs) == 1
    # Not merely "some owned address": exactly the derived change address.
    assert change_outs[0].address == change_address
    # And the full remainder, to the satoshi — no silent leak into the fee.
    assert change_outs[0].value == 1_000_000 - 100_000 - prepared.built.fee
    assert prepared.built.fee <= 50_000
    a.sign(prepared.built)
    assert prepared.built.tx.verify() is True


def test_btc_swap_change_returns_to_the_wallets_change_address():
    """Same guarantee on the swap path (vault + OP_RETURN + change)."""
    from swapsack.chains.btc import CHANGE_PATH

    a = BtcAdapter()
    addr = a.derive_address(MNEMONIC, PATH)
    change_address = a.derive_address(MNEMONIC, CHANGE_PATH)
    utxos = [Utxo(txid="aa" * 32, vout=0, value=1_000_000, address=addr, path=PATH)]

    built = a.build_unsigned_swap(
        mnemonic=MNEMONIC,
        utxos=utxos,
        vault_address=VAULT,
        amount=100_000,
        memo=MEMO,
        fee_rate=2,
        change_address=change_address,
    )
    change_outs = [
        o for o in built.outputs if o.op_return_data is None and o.address != VAULT
    ]
    assert len(change_outs) == 1
    assert change_outs[0].address == change_address
    assert change_outs[0].value == 1_000_000 - 100_000 - built.fee


def test_btc_change_address_is_found_by_the_wallet_scan():
    """Change must be *recoverable*, not just correctly addressed.

    Change lands on the internal branch, so if the gap-limit scan only walked
    the receive branch the remainder would be unspendable-in-practice: invisible
    to ``balance`` and never selected as an input by a later send. Prove the
    scan that feeds both actually reaches the change address.
    """
    from swapsack.chains.base import AddressInfo
    from swapsack.chains.btc import ACCOUNT, CHANGE_PATH
    from swapsack.chains.scan import scan_account

    a = BtcAdapter()
    change_address = a.derive_address(MNEMONIC, CHANGE_PATH)
    assert CHANGE_PATH.startswith(f"{ACCOUNT}/1/")  # internal branch, per BIP44

    def probe(address: str) -> AddressInfo:
        # Only the change address has history: the wallet spent everything from
        # the receive branch and holds the remainder as change.
        hit = address == change_address
        return AddressInfo(has_history=hit, confirmed=900_000 if hit else 0, pending=0)

    records = scan_account(
        derive_address=lambda p: a.derive_address(MNEMONIC, p),
        probe=probe,
        account=ACCOUNT,
    )
    assert [(path, address) for path, address, _ in records] == [
        (CHANGE_PATH, change_address)
    ]
    assert sum(info.confirmed for _, _, info in records) == 900_000


def test_parse_address_info_confirmed_and_pending():
    from swapsack.chains.btc import parse_address_info

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
    from swapsack.chains.btc import parse_address_info

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
    from swapsack.thorchain import Quote, SwapFees

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
    from swapsack.swap import SwapRequest

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
    from swapsack.swap import SwapRequest

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


def test_btc_derivation_honors_bip39_passphrase():
    base = BtcAdapter().derive_address(MNEMONIC)
    withpw = BtcAdapter(bip39_passphrase="extra-word").derive_address(MNEMONIC)
    assert withpw != base
    # Empty passphrase == no passphrase: a v1 wallet keeps its addresses.
    assert BtcAdapter(bip39_passphrase="").derive_address(MNEMONIC) == base


# --- on-chain transaction summary (what `status` reports for a txid) ---------


def test_parse_tx_summary_reads_a_partial_send(esplora_tx_partial_send):
    from swapsack.chains.btc import parse_tx_summary

    tx = parse_tx_summary(esplora_tx_partial_send)
    assert tx.confirmed is True
    assert tx.block_height == 959260
    assert tx.fee == 160
    assert tx.total_in == 2_010_000
    assert tx.total_out == 2_009_840
    # Inputs + outputs + fee must balance, or we are misreporting a spend.
    assert tx.total_in == tx.total_out + tx.fee
    assert [(o.address, o.value) for o in tx.outputs] == [
        ("1Recipient", 1_527_000),
        ("bc1qchange", 482_840),
    ]
    assert [(i.address, i.value) for i in tx.inputs] == [("bc1qspender", 2_010_000)]
    # vsize drives the fee rate: weight 561 -> ceil(561/4) = 141 vB.
    assert tx.vsize == 141
    assert round(tx.fee_rate, 2) == round(160 / 141, 2)


def test_parse_tx_summary_handles_unconfirmed_and_op_return(esplora_tx_partial_send):
    from swapsack.chains.btc import parse_tx_summary

    payload = {
        **esplora_tx_partial_send,
        "status": {"confirmed": False},
        "vout": [
            *esplora_tx_partial_send["vout"],
            {"scriptpubkey_type": "op_return", "value": 0},
        ],
    }
    tx = parse_tx_summary(payload)
    assert tx.confirmed is False and tx.block_height is None
    # An OP_RETURN (a swap memo) carries no address but must not be dropped:
    # its presence is what distinguishes a swap deposit from a plain send.
    assert tx.outputs[-1].address is None
    assert tx.outputs[-1].op_return is True


def test_built_tx_signals_bip125_rbf():
    """Every input must opt in to Replace-By-Fee (nSequence 0xfffffffd).

    Without it a stuck low-fee tx cannot be fee-bumped (the wallet targets a
    handful of blocks by default, so this is a real risk), and standard-policy
    nodes reject a replacement outright. Signalling is harmless today and
    unlocks a future `bump` command.
    """
    a = BtcAdapter()
    p0, p1 = "m/84'/0'/0'/0/0", "m/84'/0'/0'/0/1"
    a0, a1 = a.derive_address(MNEMONIC, p0), a.derive_address(MNEMONIC, p1)
    utxos = [
        Utxo(txid="aa" * 32, vout=0, value=120000, address=a0, path=p0),
        Utxo(txid="bb" * 32, vout=0, value=120000, address=a1, path=p1),
    ]
    built = a.build_unsigned_swap(
        mnemonic=MNEMONIC,
        utxos=utxos,
        vault_address=VAULT,
        amount=178100,
        memo=MEMO,
        fee_rate=2,
    )
    assert built.tx.inputs, "expected at least one input"
    for inp in built.tx.inputs:
        assert inp.sequence == 0xFFFFFFFD
        assert inp.sequence < 0xFFFFFFFE  # BIP125 opt-in threshold
    # RBF signalling must not break signing/verification.
    (raw_hex,) = a.sign(built)
    assert built.tx.verify() is True
    # What actually relays is the serialization, not the attribute above, so
    # read the sequence back out of the signed bytes. nLockTime must still be 0:
    # 0xfffffffd is chosen precisely to signal RBF while leaving locktime usable.
    from bitcoinlib.transactions import Transaction

    reparsed = Transaction.parse_hex(raw_hex, strict=False)
    assert len(reparsed.inputs) == len(built.tx.inputs)
    for inp in reparsed.inputs:
        assert inp.sequence == 0xFFFFFFFD
    assert reparsed.locktime == 0
