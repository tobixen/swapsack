"""MayaChain adapter — the native CACAO asset (a Cosmos-SDK / Tendermint chain).

Phase 1 (this file): **Hold + Balance**, read-only. Derives the transparent
``maya1`` address from the seed and reads the CACAO balance from a mayanode REST
node (keyless; the same host the Maya swap backend uses). Deriving the address
is the money-sensitive part — a wrong address silently sends funds to one the
wallet does not control — so the derivation is cross-checked in the tests
against three independent BIP32 implementations and a golden vector.

MayaChain shares THORChain's key scheme: SLIP-44 coin type 931, secp256k1, and a
``bech32(ripemd160(sha256(compressed_pubkey)))`` account address; only the HRP
differs (``maya`` vs ``thor``). See docs/cacao.md for the full-support roadmap
(Send/From/Liquidity need Cosmos ``MsgSend``/``MsgDeposit`` signing — not here).
"""

from __future__ import annotations

import base64
import dataclasses

from bitcoinlib.encoding import hash160
from bitcoinlib.keys import HDKey
from bitcoinlib.mnemonic import Mnemonic

from cryptoswap_wallet.chains.base import BalanceReport
from cryptoswap_wallet.net import HTTP_ERRORS, HttpClient
from cryptoswap_wallet.swap import BroadcastError, Prepared
from cryptoswap_wallet.verify import MayaSendPlan, verify_maya_send

DEFAULT_MAYANODE = "https://mayanode.mayachain.info"
DEFAULT_DERIVATION = "m/44'/931'/0'/0/0"
DEFAULT_CHAIN_ID = "mayachain-mainnet-v1"
MAYA_HRP = "maya"
CACAO_DENOM = "cacao"
# Maya's native CACAO is 1e10 (10 decimals) — the one asset that deviates from
# THORChain's 1e8 convention. See cryptoswap_wallet.thorchain.asset_unit.
CACAO_DECIMALS = 10
CACAO_UNIT = 10**CACAO_DECIMALS
# Gas limit for a native MsgSend. MayaChain charges a fixed native-tx fee itself
# (deducted from the account), so the cosmos Fee carries no coins; the network
# is the source of truth. If mainnet rejects, this and the fee are the knobs.
DEFAULT_GAS_LIMIT = 2_000_000

# --- bech32 (BIP173) --------------------------------------------------------
# A minimal, self-contained implementation (no new dependency) for the ``maya``
# HRP. Verified against the reference test vectors and a real on-chain address.
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: bytes | list[int], frm: int, to: int, pad: bool) -> list[int]:
    acc = bits = 0
    ret: list[int] = []
    maxv = (1 << to) - 1
    for value in data:
        if value < 0 or value >> frm:
            raise ValueError("invalid value for bech32 base conversion")
        acc = (acc << frm) | value
        bits += frm
        while bits >= to:
            bits -= to
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (to - bits)) & maxv)
    elif bits >= frm or (acc << (to - bits)) & maxv:
        raise ValueError("invalid padding in bech32 base conversion")
    return ret


def bech32_encode(hrp: str, data: bytes) -> str:
    """Encode a byte string (e.g. a 20-byte account hash) as a bech32 address."""
    values = _convertbits(data, 8, 5, pad=True)
    checksum_input = _bech32_hrp_expand(hrp) + values + [0, 0, 0, 0, 0, 0]
    polymod = _bech32_polymod(checksum_input) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in values + checksum)


def bech32_decode(address: str) -> tuple[str, bytes]:
    """Inverse of :func:`bech32_encode`; raises :class:`ValueError` on bad checksum."""
    pos = address.rfind("1")
    if pos < 1 or pos + 7 > len(address):
        raise ValueError(f"not a bech32 address: {address!r}")
    hrp = address[:pos]
    try:
        values = [_BECH32_CHARSET.index(c) for c in address[pos + 1 :]]
    except ValueError:
        raise ValueError(f"invalid bech32 character in {address!r}") from None
    if _bech32_polymod(_bech32_hrp_expand(hrp) + values) != 1:
        raise ValueError(f"bad bech32 checksum in {address!r}")
    return hrp, bytes(_convertbits(values[:-6], 5, 8, pad=False))


# --- balance parsing --------------------------------------------------------


def parse_balances(payload: dict, denom: str = CACAO_DENOM) -> int:
    """Sum the base-unit amount for ``denom`` in a cosmos bank balances response.

    A fresh (never-funded) account returns an empty ``balances`` list -> 0.
    """
    total = 0
    for entry in payload.get("balances", []):
        if entry.get("denom") == denom:
            total += int(entry.get("amount", 0))
    return total


@dataclasses.dataclass
class BuiltMayaTx:
    """An unsigned Cosmos tx: the two SignDoc byte-blobs + what signing needs."""

    body_bytes: bytes
    auth_info_bytes: bytes
    chain_id: str
    account_number: int
    private_key: bytes


class MayaAdapter(HttpClient):
    """ChainAdapter for MayaChain (native CACAO)."""

    chain = "MAYA"
    asset = "MAYA.CACAO"

    def __init__(
        self,
        mayanode_url: str = DEFAULT_MAYANODE,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(timeout)
        self.mayanode_url = mayanode_url.rstrip("/")
        self.bip39_passphrase = bip39_passphrase

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        """Derive the ``maya1`` account address for ``path`` (default receive key)."""
        seed = Mnemonic().to_seed(mnemonic, self.bip39_passphrase)
        pubkey = HDKey.from_seed(seed).key_for_path(path).public_byte
        return bech32_encode(MAYA_HRP, hash160(pubkey))

    def fetch_balance(self, address: str) -> int:
        """Confirmed CACAO balance in base units (1e10); 0 for a fresh account."""
        resp = self._get(f"{self.mayanode_url}/cosmos/bank/v1beta1/balances/{address}")
        resp.raise_for_status()
        return parse_balances(resp.json())

    def wallet_balance(self, mnemonic: str) -> BalanceReport:
        address = self.derive_address(mnemonic)
        return BalanceReport(
            symbol="CACAO",
            confirmed=self.fetch_balance(address),
            decimals=CACAO_DECIMALS,
            note=f"({address})",
            addresses=(address,),
        )

    # --- spending FROM Maya (plain CACAO send) ------------------------------

    def _keys(self, mnemonic: str, path: str) -> tuple[bytes, bytes]:
        """Return ``(private_byte, compressed_public_byte)`` for ``path``."""
        seed = Mnemonic().to_seed(mnemonic, self.bip39_passphrase)
        key = HDKey.from_seed(seed).key_for_path(path)
        return key.private_byte, key.public_byte

    def fetch_account(self, address: str) -> tuple[int, int]:
        """Return ``(account_number, sequence)`` for signing; both 0 for a fresh
        account the node has not seen yet."""
        resp = self._get(f"{self.mayanode_url}/cosmos/auth/v1beta1/accounts/{address}")
        if resp.status_code == 404:
            return 0, 0
        resp.raise_for_status()
        account = resp.json().get("account", {})
        return int(account.get("account_number", 0)), int(account.get("sequence", 0))

    def fetch_chain_id(self) -> str:
        """The network's chain id (falls back to the known mainnet id on failure)."""
        try:
            resp = self._get(
                f"{self.mayanode_url}/cosmos/base/tendermint/v1beta1/node_info"
            )
            resp.raise_for_status()
            return resp.json()["default_node_info"]["network"] or DEFAULT_CHAIN_ID
        except (KeyError, ValueError, *HTTP_ERRORS):
            return DEFAULT_CHAIN_ID

    def build_and_verify_send(
        self,
        *,
        recipient: str,
        amount: int,
        mnemonic: str,
        path: str = DEFAULT_DERIVATION,
        gas_limit: int = DEFAULT_GAS_LIMIT,
    ) -> Prepared:
        """Build + gate a plain CACAO ``MsgSend`` of ``amount`` (1e10 base units).

        Signing/broadcast happen only after the returned gate passes. The gate
        decodes the *serialized* body and binds sender/recipient/denom/amount and
        the absence of a memo — a wrong recipient here is an unrefundable loss.
        """
        from cryptoswap_wallet.chains import maya_tx

        private_key, public_key = self._keys(mnemonic, path)
        sender = self.derive_address(mnemonic, path)
        amount_str = str(amount)
        body_bytes = maya_tx.tx_body(
            [
                (
                    maya_tx.MSGSEND_TYPE_URL,
                    maya_tx.msg_send(sender, recipient, CACAO_DENOM, amount_str),
                )
            ],
            "",
        )
        account_number, sequence = self.fetch_account(sender)
        auth_info_bytes = maya_tx.auth_info(public_key, sequence, [], gas_limit)
        built = BuiltMayaTx(
            body_bytes=body_bytes,
            auth_info_bytes=auth_info_bytes,
            chain_id=self.fetch_chain_id(),
            account_number=account_number,
            private_key=private_key,
        )
        plan = MayaSendPlan(
            from_addr=sender, recipient=recipient, denom=CACAO_DENOM, amount=amount_str
        )
        problems = verify_maya_send(
            decoded=maya_tx.decode_msg_send_body(body_bytes), plan=plan
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

    def sign(self, built: BuiltMayaTx) -> list[str]:
        from cryptoswap_wallet.chains import maya_tx

        doc = maya_tx.sign_doc(
            built.body_bytes,
            built.auth_info_bytes,
            built.chain_id,
            built.account_number,
        )
        signature = maya_tx.sign_direct(built.private_key, doc)
        tx_raw = maya_tx.tx_raw(built.body_bytes, built.auth_info_bytes, signature)
        return [base64.b64encode(tx_raw).decode()]

    def broadcast(self, raws: list[str]) -> str:
        """Broadcast base64 TxRaw bytes via the cosmos REST endpoint; return txhash."""
        txhash = ""
        for tx_b64 in raws:
            resp = self._post(
                f"{self.mayanode_url}/cosmos/tx/v1beta1/txs",
                json={"tx_bytes": tx_b64, "mode": "BROADCAST_MODE_SYNC"},
            )
            resp.raise_for_status()
            result = resp.json().get("tx_response", {})
            if result.get("code", 0) != 0:
                raise BroadcastError(
                    f"maya rejected tx (code {result.get('code')}): "
                    f"{result.get('raw_log')}"
                )
            txhash = result.get("txhash", "")
        return txhash
