"""Cardano chain adapter — hold, balance, send, sweep, swap-from and LP.

Keys and addresses come from :mod:`swapsack.chains.cardano_keys` (CIP-1852,
Icarus master key, Shelley *base* addresses — the ones Yoroi/Eternl show for the
same mnemonic). Everything network goes to **Koios** (``api.koios.rest``), a
keyless public Cardano API; any Koios instance can be named with ``--ada-api``.

Units: the adapter's whole surface — balances, UTXO values, amounts, fees —
is in **1e-8 ADA**, the unit Maya quotes and the shared UTXO flow and verify
gate count in. ADA's own unit is the lovelace (1e-6), so a value read from the
chain is scaled up by :data:`LOVELACE_TO_UNITS` here and back down only on the
wire. Swaps route through Maya only: THORChain has no ADA pool.

Spending builds through :mod:`swapsack.chains.cardano_tx` and plugs into the
shared gate wrappers (:class:`~swapsack.chains.gated.GatedTxBuilder`) like ZEC
does: a Maya deposit's memo travels as CIP-20 metadata rather than an
OP_RETURN, and the gate reads it back out of the serialized bytes. Only
pure-ADA UTXOs are ever spent, so no native token can be dropped by accident.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from swapsack.chains.base import AddressInfo, BalanceReport
from swapsack.chains.cardano_keys import (
    ExtendedKey,
    base_address,
    blake2b_224,
    derive,
    parse_path,
    root_key,
)
from swapsack.chains.cardano_tx import (
    LONGEST_ADDRESS,
    ProtocolParams,
    TxIn,
    TxOut,
    UnsignedTx,
    address_bytes,
    min_lovelace,
    parse,
    payment_key_hash,
    required_fee,
    serialize,
    txid,
)
from swapsack.chains.coins import InsufficientFunds, Utxo
from swapsack.chains.gated import GatedTxBuilder
from swapsack.net import HttpClient
from swapsack.swap import Prepared, SwapAborted
from swapsack.verify import TxOutput

DEFAULT_ADA_API = "https://api.koios.rest/api/v1"
ACCOUNT = "m/1852'/1815'/0'"
DEFAULT_DERIVATION = f"{ACCOUNT}/0/0"  # external chain, first index
CHANGE_PATH = f"{ACCOUNT}/1/0"  # internal (change) chain, first index
# CIP-1852 role 2: the account's single staking key, shared by every base
# address of the account (only the payment half differs per address).
STAKE_ROLE_INDEX = "2/0"
LOVELACE_TO_UNITS = 100  # 1 lovelace = 1e-6 ADA = 100 × 1e-8 ADA
# A built tx stays valid this many slots (1 s each) past the tip, so one that
# never lands expires and frees its inputs within two hours.
TTL_SLOTS = 7200
# Maya refuses a longer memo: MaxMayaMemoSizeV1 in mayanode's
# constants/constants.go (develop, 2026-10-23); MaxMayaMemoSize there is 512,
# but a node on the older rule would drop the deposit.
MEMO_MAX_BYTES = 250


def parse_address_info(rows: list[dict]) -> AddressInfo:
    """One address's Koios ``address_info`` answer, in 1e-8 ADA.

    Koios lists every address that ever appeared on chain and omits the rest,
    so presence alone is the history signal: a used-but-emptied address comes
    back with balance 0 and must keep the gap-limit scan going. Koios indexes
    the ledger only (no mempool), so ``pending`` is always 0.
    """
    if not rows:
        return AddressInfo(has_history=False, confirmed=0, pending=0)
    return AddressInfo(
        has_history=True,
        confirmed=int(rows[0]["balance"]) * LOVELACE_TO_UNITS,
        pending=0,
    )


@dataclasses.dataclass
class AdaBuilt:
    """A built (unsigned) ADA spend, ready for the verify gate + signer.

    ``fee`` and the neutral ``outputs`` are in 1e-8 ADA like the rest of the
    adapter's surface; ``tx`` is in lovelace, as the chain counts.
    """

    tx: UnsignedTx
    keys: list[ExtendedKey]
    outputs: list[TxOutput]  # re-read from the serialized bytes (neutral)
    fee: int
    change_address: str
    inputs: list[Utxo]
    # Said next to the fee when part of it is not fee at all (folded change).
    fee_note: str = ""


def neutral_view(raw: bytes) -> tuple[list[TxOutput], int]:
    """What a serialized tx pays and its fee, in the gate's terms (1e-8 ADA).

    The memo becomes one data entry (``address=None``), as an OP_RETURN does
    on the other UTXO chains — the gate's "exactly one memo, byte-identical
    to the quote" rule then applies unchanged.
    """
    parsed = parse(raw)
    outputs = [
        TxOutput(address=address, value=lovelace * LOVELACE_TO_UNITS)
        for address, lovelace in parsed.outputs
    ]
    if parsed.memo is not None:
        outputs.append(TxOutput(address=None, value=0, op_return_data=parsed.memo))
    return outputs, parsed.fee * LOVELACE_TO_UNITS


def _select(
    pool: list[Utxo],
    recipient: TxOut,
    change_raw: bytes,
    fee_for: Callable[[list[Utxo], tuple[TxOut, ...]], int],
    params: ProtocolParams,
) -> tuple[list[Utxo], tuple[TxOut, ...], int]:
    """Largest-first: ``(chosen, outputs, fee)`` with a viable change.

    A leftover under min-ADA cannot be an output. More inputs are tried
    first; only when they run out does the first selection that covered
    amount + fee fold its leftover into the fee (the gate's ``--max-fee``
    still bounds it).
    """
    fold = None
    chosen: list[Utxo] = []
    for utxo in pool:
        chosen.append(utxo)
        total = sum(u.value for u in chosen) // LOVELACE_TO_UNITS
        # Size the fee on the largest the change could be, then once more
        # on the real change: the fee can only shrink, the change only grow.
        fee = fee_for(chosen, (recipient, TxOut(change_raw, total)))
        change = TxOut(change_raw, total - recipient.lovelace - fee)
        exact = fee_for(chosen, (recipient, change))
        settled = TxOut(change_raw, total - recipient.lovelace - exact)
        if fee_for(chosen, (recipient, settled)) <= exact:
            fee, change = exact, settled
        if change.lovelace >= min_lovelace(change, params):
            return list(chosen), (recipient, change), fee
        alone = fee_for(chosen, (recipient,))
        if fold is None and total - recipient.lovelace >= alone:
            fold = (list(chosen), total - recipient.lovelace)
    if fold is None:
        have = sum(u.value for u in pool) / 1e8
        raise InsufficientFunds(
            f"{recipient.lovelace / 1e6:.6f} ADA plus the fee exceeds the "
            f"{have:.6f} ADA spendable"
        )
    return fold[0], (recipient,), fold[1]


class AdaAdapter(HttpClient, GatedTxBuilder):
    """ChainAdapter for Cardano (Shelley base addresses), pure-ADA spends."""

    chain = "ADA"
    asset = "ADA.ADA"
    unconfirmed_spendable = False  # Koios indexes the ledger, not the mempool
    default_derivation = DEFAULT_DERIVATION
    account = ACCOUNT
    change_path = CHANGE_PATH
    # The ADA.ADA pool exists only on Maya.
    lp_backends = ("maya",)
    memo_max_bytes = MEMO_MAX_BYTES
    # 0.5 ADA. A simple payment costs ~0.17-0.2 ADA; this leaves room for many
    # inputs while still catching a fee that is wrong by an order of magnitude.
    default_max_fee = 50_000_000
    fee_basis = "(Cardano minimum fee for the tx size)"

    def __init__(
        self,
        api_url: str = DEFAULT_ADA_API,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(timeout, hint="--ada-api / $SWAPSACK_ADA_API")
        self.api_url = api_url.rstrip("/")
        self.bip39_passphrase = bip39_passphrase
        self._params: ProtocolParams | None = None

    # --- derivation ------------------------------------------------------------

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        """The base address whose payment key sits at ``path``.

        The stake half is the account's role-2 key, so ``path`` must be a full
        CIP-1852 ``m/purpose'/coin'/account'/role/index``.
        """
        if len(parse_path(path)) != 5:
            raise ValueError(f"not a CIP-1852 address path: {path!r}")
        account = path.rsplit("/", 2)[0]
        root = root_key(mnemonic, self.bip39_passphrase)
        payment = derive(root, path).public_key
        stake = derive(root, f"{account}/{STAKE_ROLE_INDEX}").public_key
        return base_address(payment, stake)

    # --- network via Koios; guarded by an opt-in live test ---------------------

    def _query(self, endpoint: str, body: dict) -> list[dict]:
        resp = self._post_query(f"{self.api_url}/{endpoint}", json=body)
        resp.raise_for_status()
        return resp.json()

    def _fetch(self, endpoint: str, **kwargs: object) -> list[dict]:
        resp = self._get(f"{self.api_url}/{endpoint}", **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _address_rows(self, address: str) -> list[dict]:
        return self._query("address_info", {"_addresses": [address]})

    def address_info(self, address: str) -> AddressInfo:
        return parse_address_info(self._address_rows(address))

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from swapsack.chains.scan import wallet_balance_from_scan

        return wallet_balance_from_scan(
            derive_address=lambda p: self.derive_address(mnemonic, p),
            probe=self.address_info,
            account=account,
            symbol="ADA",
        )

    def tip_slot(self) -> int:
        return int(self._fetch("tip")[0]["abs_slot"])

    def protocol_params(self) -> ProtocolParams:
        """The current epoch's fee and min-ADA parameters (cached per adapter).

        Right after an epoch boundary Koios can know the tip's epoch before it
        has that epoch's row; the previous epoch's parameters then still apply
        to anything built this minute (they change only by governance).
        """
        if self._params is None:
            epoch = self._fetch("tip")[0]["epoch_no"]
            rows = self._fetch("epoch_params", params={"_epoch_no": epoch})
            if not rows:
                rows = self._fetch("epoch_params", params={"_epoch_no": epoch - 1})
            row = rows[0]
            self._params = ProtocolParams(
                min_fee_a=int(row["min_fee_a"]),
                min_fee_b=int(row["min_fee_b"]),
                coins_per_utxo_byte=int(row["coins_per_utxo_size"]),
                max_tx_size=int(row["max_tx_size"]),
            )
        return self._params

    # --- send / sweep / deposit ------------------------------------------------

    def fetch_utxos(
        self,
        address: str,
        *,
        include_unconfirmed: bool = False,  # noqa: ARG002 (ledger-only source)
    ) -> list[Utxo]:
        """This address's pure-ADA outputs, in 1e-8 ADA.

        An output that also holds native tokens is left alone: spending it
        would mean carrying the tokens into change, which this wallet does not
        do, and dropping them is not an option. Its ADA still shows in
        ``balance``; move the tokens out with another wallet to free it. An
        output carrying a reference script is skipped too: Conway charges its
        bytes on top of the size fee, which the fee maths here leaves out.
        """
        rows = self._address_rows(address)
        return [
            Utxo(
                txid=u["tx_hash"],
                vout=int(u["tx_index"]),
                value=int(u["value"]) * LOVELACE_TO_UNITS,
                address=address,
            )
            for row in rows
            for u in row.get("utxo_set", [])
            if not u.get("asset_list") and not u.get("reference_script")
        ]

    def fetch_fee_rate(self, target_blocks: int = 2) -> float:  # noqa: ARG002
        """Cardano fees are size-based with no market; 0 = no rate."""
        return 0.0

    def sweep_send_amount(
        self,
        total: int,
        n_inputs: int,
        fee_rate: float,  # noqa: ARG002 (no fee market)
        memo_len: int = 0,
        extra_fee: int = 0,  # noqa: ARG002 (no unconfirmed ADA input to lift)
    ) -> tuple[int, int]:
        """``(amount, fee)`` draining ``total`` into one output, in 1e-8 ADA.

        The recipient is not known yet, so the fee is sized for the worst
        case: the longest address, one witness per input and a TTL as long as
        it will ever encode. The build then pays exactly ``total - amount``.
        Inputs share witnesses per address, so with many UTXOs on one address
        that overpays ~4,400 lovelace per extra input (30 UTXOs: ~0.13 ADA),
        and a swap sweep sized for a 250-byte memo adds ~44 lovelace per unused
        byte. Never under.
        """
        params = self.protocol_params()
        lovelace = total // LOVELACE_TO_UNITS
        dummy = UnsignedTx(
            inputs=tuple(TxIn(i.to_bytes(32, "big"), i) for i in range(n_inputs)),
            outputs=(TxOut(LONGEST_ADDRESS, lovelace),),
            fee=0,
            ttl=2**32 - 1,
            memo="x" * memo_len if memo_len else None,
        )
        fee = required_fee(dummy, n_inputs, params)
        amount = lovelace - fee
        if amount < min_lovelace(TxOut(LONGEST_ADDRESS, amount), params):
            raise InsufficientFunds(
                f"{total / 1e8:.6f} ADA cannot cover a {fee / 1e6:.6f} ADA fee "
                "and still leave an output above the minimum ADA"
            )
        return amount * LOVELACE_TO_UNITS, fee * LOVELACE_TO_UNITS

    def _signing_keys(self, mnemonic: str, chosen: list[Utxo]) -> list[ExtendedKey]:
        """One key per distinct input address, each proven to own it."""
        root = root_key(mnemonic, self.bip39_passphrase)
        keys: dict[bytes, ExtendedKey] = {}
        for utxo in chosen:
            key = derive(root, utxo.path or DEFAULT_DERIVATION)
            if payment_key_hash(address_bytes(utxo.address)) != blake2b_224(
                key.public_key
            ):
                raise SwapAborted(
                    f"refusing to sign: the payment key at {utxo.path} does not "
                    f"own {utxo.address}"
                )
            keys[key.public_key] = key
        return list(keys.values())

    def build_unsigned_swap(
        self,
        *,
        mnemonic: str,
        utxos: list[Utxo],
        vault_address: str,
        amount: int,
        memo: str | bytes | None,
        fee_rate: float = 0.0,  # noqa: ARG002 (no fee market; hook parity)
        change_address: str,
        sweep: bool = False,
    ) -> AdaBuilt:
        """Build the unsigned tx paying ``amount`` (1e-8 ADA) to ``vault_address``.

        ``memo`` of ``None`` is a plain send; a ``str`` is a Maya memo, carried
        as CIP-20 metadata. Selection is largest-first and keeps adding inputs
        while the leftover is too small to be a change output; only when the
        UTXOs run out is such a leftover folded into the fee.
        """
        if isinstance(memo, bytes):
            raise SwapAborted("a binary memo (Chainflip) has no ADA route")
        if amount % LOVELACE_TO_UNITS:
            raise SwapAborted(
                f"{amount / 1e8} ADA is finer than a lovelace; "
                "ADA amounts take at most 6 decimals"
            )
        params = self.protocol_params()
        ttl = self.tip_slot() + TTL_SLOTS
        lovelace = amount // LOVELACE_TO_UNITS
        recipient = TxOut(address_bytes(vault_address), lovelace)
        floor = min_lovelace(recipient, params)
        if lovelace < floor:
            raise SwapAborted(f"an ADA output must hold at least {floor / 1e6:.6f} ADA")
        change_raw = address_bytes(change_address)

        def unsigned(
            chosen: list[Utxo], outputs: tuple[TxOut, ...], fee: int
        ) -> UnsignedTx:
            inputs = tuple(TxIn(bytes.fromhex(u.txid), u.vout) for u in chosen)
            return UnsignedTx(inputs, outputs, fee, ttl, memo)

        def fee_for(chosen: list[Utxo], outputs: tuple[TxOut, ...]) -> int:
            witnesses = len({u.address for u in chosen})
            return required_fee(unsigned(chosen, outputs, 0), witnesses, params)

        pool = sorted(utxos, key=lambda u: u.value, reverse=True)
        if sweep:
            chosen = pool
            outputs: tuple[TxOut, ...] = (recipient,)
            fee = sum(u.value for u in chosen) // LOVELACE_TO_UNITS - lovelace
            if fee < fee_for(chosen, outputs):
                raise InsufficientFunds(
                    f"sweeping {lovelace / 1e6:.6f} ADA leaves "
                    f"{fee / 1e6:.6f} ADA, below the minimum fee"
                )
        else:
            chosen, outputs, fee = _select(pool, recipient, change_raw, fee_for, params)

        tx = unsigned(chosen, outputs, fee)
        witnesses = len({u.address for u in chosen})
        dummy = [(bytes([i]) * 32, bytes(64)) for i in range(witnesses)]
        size = len(serialize(tx, dummy))
        if size > params.max_tx_size:
            raise SwapAborted(
                f"the tx would be {size} bytes, over Cardano's "
                f"{params.max_tx_size}-byte limit — too many inputs; spend less, "
                "or consolidate the UTXOs first"
            )
        neutral, neutral_fee = neutral_view(serialize(tx, []))
        folded = fee - fee_for(chosen, outputs)
        return AdaBuilt(
            tx=tx,
            keys=self._signing_keys(mnemonic, chosen),
            outputs=neutral,
            # Read from the body, not the builder's own number, like outputs.
            fee=neutral_fee,
            change_address=change_address,
            inputs=chosen,
            fee_note=(
                f"(includes {folded / 1e6:.6f} ADA of change below the min-ADA "
                "an output must hold, so it cannot be returned)"
                if folded > 0 and not sweep
                else ""
            ),
        )

    def build_and_verify_deposit(
        self,
        *,
        vault: str,
        memo: str | bytes,
        amount: int,
        now: int,
        mnemonic: str,
        scanned_utxos: list[Utxo],
        fee_rate: float,
        change_address: str,  # noqa: ARG002 (replaced: see below)
        max_fee: int,
        sweep: bool = False,
    ) -> Prepared:
        """An LP deposit (add or withdraw trigger) from the receive address only.

        Maya files a position under, and authorises its withdraw from, the
        address of the deposit's *first input* — and inputs are a set, sorted
        by txid on the wire, so with UTXOs on several addresses which one is
        first is an accident. Spending only the receive address's UTXOs, and
        returning change there, keeps every add and withdraw on one address.
        """
        lp_address = self.derive_address(mnemonic, DEFAULT_DERIVATION)
        utxos = [u for u in scanned_utxos if u.address == lp_address]
        if not utxos:
            raise InsufficientFunds(
                f"ADA liquidity is added and withdrawn from {lp_address} only, "
                "and it holds no spendable ADA — send some there first"
            )
        if sweep and len(utxos) != len(scanned_utxos):
            raise SwapAborted(
                f"--amount max for ADA liquidity needs all the ADA on {lp_address}; "
                "send it there first, or give an amount"
            )
        prepared = super().build_and_verify_deposit(
            vault=vault,
            memo=memo,
            amount=amount,
            now=now,
            mnemonic=mnemonic,
            scanned_utxos=utxos,
            fee_rate=fee_rate,
            change_address=lp_address,
            max_fee=max_fee,
            sweep=sweep,
        )
        stray = {u.address for u in prepared.built.inputs} - {lp_address}
        if stray:
            prepared.problems.append(f"LP deposit spends from {sorted(stray)}")
        return prepared

    def sign(self, built: AdaBuilt) -> list[str]:
        digest = txid(built.tx)
        raw = serialize(
            built.tx, [(key.public_key, key.sign(digest)) for key in built.keys]
        )
        # Read the result back: the txid each witness signed must be the one
        # the bytes hash to, and what they pay must be what the gate passed.
        parsed = parse(raw)
        outputs, fee = neutral_view(raw)
        if (
            parsed.txid != digest
            or len(parsed.witnesses) != len(built.keys)
            or outputs != built.outputs
            or fee != built.fee
        ):
            raise RuntimeError("refusing to broadcast: ADA tx failed its read-back")
        return [raw.hex()]

    def broadcast(self, raws: list[str]) -> str:
        tx_id = ""
        for raw in raws:
            data = bytes.fromhex(raw)
            resp = self._post(
                f"{self.api_url}/submittx",
                data=data,
                headers={"Content-Type": "application/cbor"},
            )
            tx_id = parse(data).txid.hex()
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Koios rejected the tx (HTTP {resp.status_code}), so it was "
                    f"not sent: {resp.text}"
                )
            # Koios answers the txid as a JSON string, or as bare text on some
            # instances. Anything else comes *after* the node took the tx.
            answer = resp.text.strip().strip('"')
            if answer != tx_id:
                raise RuntimeError(
                    f"Koios accepted the tx but answered {answer[:200]!r}; it may "
                    f"have been broadcast as {tx_id} — check that txid before "
                    "retrying, or you may pay twice"
                )
        return tx_id
