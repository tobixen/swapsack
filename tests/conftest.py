"""Shared pytest configuration.

The unit suite runs under ``filterwarnings = ["error"]`` (see pyproject) so
deprecations/misconfig surface as failures. The opt-in ``network`` integration
tests do real HTTP I/O, and niquests/urllib3 keep-alive sockets are released by
the garbage collector *after* the session is already closed. pytest's
unraisable-exception hook then re-raises that ``ResourceWarning`` and attributes
it to whichever ``network`` test happens to be running when the GC fires — which
flaked the "Integration (network)" CI job intermittently (e.g. pinned on
``test_btc_testnet_send_broadcast`` though any network call could be the source).

The socket *is* released; only the teardown timing is nondeterministic. The
per-item ``ignore::ResourceWarning`` filter below covers a GC that fires *during*
a network test, but not one that fires *after the last test* — between teardown
and pytest's unraisable-exception flush at ``pytest_unconfigure`` — which is the
window that intermittently reddened the "Integration (network)" job. So we also
force the reclamation deterministically in each network test's teardown (which
still runs inside that item's filter scope), draining the sockets before they
can leak into the unfiltered session-teardown window.

Both are scoped narrowly to ``network``-marked tests, so a genuine
leaked-resource (or any other unraisable) in the unit suite still fails as before.

The other direction is the offline guard below: everything *without* the
``network`` marker is refused a connection outright, so a test that forgets to
mock a call fails loudly and by name instead of quietly doing live I/O.
"""

from __future__ import annotations

import gc
import socket

import grpc
import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        if item.get_closest_marker("network"):
            item.add_marker(pytest.mark.filterwarnings("ignore::ResourceWarning"))


@pytest.fixture(autouse=True)
def _drain_keepalive_sockets(request: pytest.FixtureRequest):
    """Force GC of niquests/urllib3 keep-alive sockets in a network test's teardown.

    ``Session.close()`` does not eagerly close pooled keep-alive sockets; the GC
    does. Running ``gc.collect()`` here — still inside the item's
    ``ignore::ResourceWarning`` scope — finalizes them deterministically so none
    survives into the session-teardown window where the warning would be re-raised.
    """
    yield
    if request.node.get_closest_marker("network"):
        gc.collect()


class NetworkAccessInUnitTest(BaseException):
    """A test without the ``network`` marker tried to open a connection.

    Deliberately a ``BaseException`` and not an ``OSError``: every HTTP client in
    the tree (and niquests/urllib3 underneath) catches connection errors and
    retries or rewraps them, which would bury the message that names the
    offending test. Inheriting outside the ``Exception`` tree makes the guard
    un-swallowable, so the leak is reported where it happened.
    """


def _refuse(nodeid: str, what: str):
    def blocked(*_args: object, **_kwargs: object):
        raise NetworkAccessInUnitTest(
            f"{nodeid} tried to reach the network via {what}. The default suite "
            "is offline: mock the call, or mark the test @pytest.mark.network "
            "(opt-in, excluded by default)."
        )

    return blocked


@pytest.fixture(autouse=True)
def _offline_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Refuse outbound connections in every test that is not ``network``-marked.

    Two seams are needed, not one. ``socket.socket.connect`` covers the HTTP
    clients (niquests/urllib3 build the socket in Python), but grpcio — ZEC's
    lightwalletd transport — connects from its C core and does *not* go through
    the Python socket object: with ``socket.socket.connect`` patched to raise, a
    real channel to ``zec.rocks:443`` still came up. So the grpc channel
    factories are blocked separately.

    That leaves any other C-level client as an unguarded hole, which is why
    ``unshare -rn -- uv run --no-sync pytest -q`` remains the belt-and-braces
    check: it is the kernel saying no, rather than us.
    """
    if request.node.get_closest_marker("network"):
        yield
        return
    nodeid = request.node.nodeid
    monkeypatch.setattr(socket.socket, "connect", _refuse(nodeid, "socket.connect"))
    monkeypatch.setattr(
        socket.socket, "connect_ex", _refuse(nodeid, "socket.connect_ex")
    )
    monkeypatch.setattr(
        socket, "create_connection", _refuse(nodeid, "socket.create_connection")
    )
    monkeypatch.setattr(grpc, "secure_channel", _refuse(nodeid, "grpc.secure_channel"))
    monkeypatch.setattr(
        grpc, "insecure_channel", _refuse(nodeid, "grpc.insecure_channel")
    )
    yield


class FakeSession:
    """Stands in for ``niquests.Session``: scripted outcomes, recorded calls.

    This is the seam to patch when a test wants to steer a client's HTTP. Its
    own ``_get``/``_get_with_fallback`` are *not*: the retry and endpoint-
    failover loops live there (``swapsack.net``), so replacing them tests a
    client that no longer exists.

    Each call pops the next outcome — an exception to raise, or an object to
    return — and falls back to ``"ok"`` once the script runs out.
    """

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.gets: list[str] = []
        self.posts: list[str] = []
        self.timeouts: list[float] = []
        # Query parameters and the rest, recorded per call: a client that drops
        # them still sends a URL, so only this can tell the difference.
        self.kwargs: list[dict[str, object]] = []

    def _next(self, url: str, recorded: list[str], timeout: float) -> object:
        recorded.append(url)
        self.timeouts.append(timeout)
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get(self, url: str, timeout: float = 0, **kwargs: object) -> object:
        self.kwargs.append(kwargs)
        return self._next(url, self.gets, timeout)

    def post(self, url: str, timeout: float = 0, **kwargs: object) -> object:
        self.kwargs.append(kwargs)
        return self._next(url, self.posts, timeout)


# Shaped like a real Esplora /tx response, with synthetic addresses: a partial
# send (one recipient + one change output), the case a sweep never produces.
# Lives here rather than in a test module because two suites use it — and
# ``import tests.…`` collides with the ``tests`` package bitcoinlib installs.
ESPLORA_TX_PARTIAL_SEND = {
    "txid": "cc" * 32,
    "size": 226,
    "weight": 561,
    "fee": 160,
    "status": {"confirmed": True, "block_height": 959260},
    "vin": [
        {
            "prevout": {
                "scriptpubkey_type": "v0_p2wpkh",
                "scriptpubkey_address": "bc1qspender",
                "value": 2_010_000,
            }
        }
    ],
    "vout": [
        {
            "scriptpubkey_type": "p2pkh",
            "scriptpubkey_address": "1Recipient",
            "value": 1_527_000,
        },
        {
            "scriptpubkey_type": "v0_p2wpkh",
            "scriptpubkey_address": "bc1qchange",
            "value": 482_840,
        },
    ],
}


@pytest.fixture
def esplora_tx_partial_send() -> dict:
    return ESPLORA_TX_PARTIAL_SEND
