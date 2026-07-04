"""Byte-exact tests for the hand-rolled MayaChain protobuf/tx assembly.

The golden ``*_HEX`` vectors were produced by cosmpy (a maintained Cosmos-SDK
library) for fixed inputs; asserting our pure serializer reproduces them proves
the wire format is correct without shipping cosmpy as a runtime dependency. The
signature is checked by recovering the signer pubkey from it (a valid Cosmos
secp256k1 signature is exactly a recoverable low-S ECDSA sig over the SignDoc
hash).
"""

import hashlib

from cryptoswap_wallet.chains.maya_tx import (
    MSGSEND_TYPE_URL,
    auth_info,
    msg_send,
    sign_direct,
    sign_doc,
    tx_body,
)

# Fixed inputs shared with the cosmpy oracle (see docs/cacao.md).
PRIV = bytes.fromhex("cd48c8b23a5d619cb67b7a4886d25127acf2e8c023e42a1e9ae14c6194532aa9")
PUB = bytes.fromhex(
    "02205c476a22d5fe10b74489db9479d0e36e25a32da393a771fcf12380136a451f"
)
FROM = "maya1gm00vwsfcp48enm4uv9e5dhm37jtd0ye2fs0sl"
TO = "maya10sy79jhw9hw9sqwdgu0k4mw4qawzl7czewzs47"
CHAIN_ID = "mayachain-mainnet-v1"

# cosmpy golden bytes for: MsgSend 10000000000 cacao, memo "hello", seq 7,
# fee 2000000 cacao / gas 2000000, account_number 12345.
BODY_HEX = (
    "0a90010a1c2f636f736d6f732e62616e6b2e763162657461312e4d736753656e6412700a2b"
    "6d61796131676d30307677736663703438656e6d34757639653564686d33376a7464307965"
    "32667330736c122b6d6179613130737937396a687739687739737177646775306b346d7734"
    "7161777a6c37637a65777a7334371a140a05636163616f120b3130303030303030303030"
    "120568656c6c6f"
)
AUTH_HEX = (
    "0a500a460a1f2f636f736d6f732e63727970746f2e736563703235366b312e5075624b6579"
    "12230a2102205c476a22d5fe10b74489db9479d0e36e25a32da393a771fcf12380136a451f"
    "12040a020801180712160a100a05636163616f1207323030303030301080897a"
)
SIGNDOC_HEX = (
    "0a9a010a90010a1c2f636f736d6f732e62616e6b2e763162657461312e4d736753656e6412"
    "700a2b6d61796131676d30307677736663703438656e6d34757639653564686d33376a7464"
    "30796532667330736c122b6d6179613130737937396a687739687739737177646775306b34"
    "6d77347161777a6c37637a65777a7334371a140a05636163616f120b31303030303030303030"
    "30120568656c6c6f126a0a500a460a1f2f636f736d6f732e63727970746f2e736563703235"
    "366b312e5075624b657912230a2102205c476a22d5fe10b74489db9479d0e36e25a32da393a"
    "771fcf12380136a451f12040a020801180712160a100a05636163616f1207323030303030"
    "301080897a1a146d617961636861696e2d6d61696e6e65742d763120b960"
)


def _body() -> bytes:
    return tx_body(
        [(MSGSEND_TYPE_URL, msg_send(FROM, TO, "cacao", "10000000000"))], "hello"
    )


def _auth() -> bytes:
    return auth_info(PUB, 7, [("cacao", "2000000")], 2000000)


def test_msgsend_txbody_matches_cosmpy():
    assert _body().hex() == BODY_HEX


def test_authinfo_matches_cosmpy():
    assert _auth().hex() == AUTH_HEX


def test_sign_doc_matches_cosmpy():
    assert sign_doc(_body(), _auth(), CHAIN_ID, 12345).hex() == SIGNDOC_HEX


def test_sign_direct_is_a_recoverable_signature_over_the_doc():
    from eth_keys import keys

    doc = sign_doc(_body(), _auth(), CHAIN_ID, 12345)
    sig64 = sign_direct(PRIV, doc)
    assert len(sig64) == 64
    digest = hashlib.sha256(doc).digest()
    signer = keys.PrivateKey(PRIV).public_key
    recovered = [
        keys.Signature(sig64 + bytes([v])).recover_public_key_from_msg_hash(digest)
        for v in (0, 1)
    ]
    assert signer in recovered


def test_uint64_and_string_defaults_are_omitted():
    from cryptoswap_wallet.chains.maya_tx import _string, _uint64

    assert _uint64(3, 0) == b""
    assert _uint64(3, 7) != b""
    assert _string(2, "") == b""
    assert _string(2, "x") != b""


def test_decode_msg_send_body_roundtrips_the_builder():
    from cryptoswap_wallet.chains.maya_tx import decode_msg_send_body

    body = tx_body([(MSGSEND_TYPE_URL, msg_send(FROM, TO, "cacao", "12345"))], "note")
    decoded = decode_msg_send_body(body)
    assert decoded == {
        "type_url": MSGSEND_TYPE_URL,
        "from_addr": FROM,
        "to_addr": TO,
        "denom": "cacao",
        "amount": "12345",
        "memo": "note",
    }


def test_verify_maya_send_flags_a_tampered_recipient():
    from cryptoswap_wallet.chains.maya_tx import decode_msg_send_body
    from cryptoswap_wallet.verify import MayaSendPlan, verify_maya_send

    body = tx_body([(MSGSEND_TYPE_URL, msg_send(FROM, TO, "cacao", "500"))], "")
    good = MayaSendPlan(from_addr=FROM, recipient=TO, denom="cacao", amount="500")
    assert verify_maya_send(decoded=decode_msg_send_body(body), plan=good) == []

    # Plan expecting a different recipient must be caught.
    evil = MayaSendPlan(from_addr=FROM, recipient=FROM, denom="cacao", amount="500")
    assert verify_maya_send(decoded=decode_msg_send_body(body), plan=evil)


def test_decode_msg_deposit_against_a_real_onchain_tx():
    # The MsgDeposit *value* (inside the Any) of a real MayaChain tx at height
    # 17312886 — a ZEC deposit swapping to BTC. Validates the wire format
    # (coin{asset,amount}, memo, signer) our builder/decoder rely on.
    from cryptoswap_wallet.chains.maya_tx import (
        MSGDEPOSIT_TYPE_URL,
        decode_msg_deposit_body,
    )

    real_value = bytes.fromhex(
        "0a1d0a110a035a454312035a45431a035a4543280112083233323030303030"
        "123c3d3a4254437e4254433a6d617961313736383230776d6a70353336777473"
        "6464786e34793532676870337732336b373437686172643a3137313635371a14"
        "f68ea7bb720d23a72e0d69a7525148b862e546de"
    )
    body = tx_body([(MSGDEPOSIT_TYPE_URL, real_value)], "")
    decoded = decode_msg_deposit_body(body)
    assert decoded["coins"] == [("ZEC.ZEC", "23200000")]
    assert (
        decoded["memo"]
        == "=:BTC~BTC:maya176820wmjp536wtsddxn4y52ghp3w23k747hard:171657"
    )
    assert len(decoded["signer"]) == 20


def test_msg_deposit_cacao_build_roundtrips():
    from cryptoswap_wallet.chains.maya_tx import (
        MSGDEPOSIT_TYPE_URL,
        decode_msg_deposit_body,
        msg_deposit,
    )

    signer = bytes(range(20))
    memo = "=:BTC.BTC:bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    value = msg_deposit([("MAYA.CACAO", "500000000000000")], memo, signer)
    decoded = decode_msg_deposit_body(tx_body([(MSGDEPOSIT_TYPE_URL, value)], ""))
    assert decoded["coins"] == [("MAYA.CACAO", "500000000000000")]
    assert decoded["memo"] == memo
    assert decoded["signer"] == signer


def test_verify_maya_send_rejects_a_memo_on_a_plain_send():
    from cryptoswap_wallet.chains.maya_tx import decode_msg_send_body
    from cryptoswap_wallet.verify import MayaSendPlan, verify_maya_send

    body = tx_body([(MSGSEND_TYPE_URL, msg_send(FROM, TO, "cacao", "500"))], "leak")
    plan = MayaSendPlan(from_addr=FROM, recipient=TO, denom="cacao", amount="500")
    assert any(
        "memo" in p
        for p in verify_maya_send(decoded=decode_msg_send_body(body), plan=plan)
    )
