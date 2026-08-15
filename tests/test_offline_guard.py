"""The offline guard in ``conftest.py``: a unit test must not reach the network.

Live I/O belongs behind ``-m network``. A test that leaks a real call does not
look broken, it looks *flaky* — and only when the remote host happens to be
down. These tests assert the guard is actually armed for an unmarked test, and
lifted for a ``network``-marked one.
"""

from __future__ import annotations

import socket

import grpc
import pytest

from conftest import NetworkAccessInUnitTest


def test_socket_connect_is_blocked():
    # `with` matters: the suite runs under `filterwarnings = ["error"]`, so an
    # unclosed socket is a ResourceWarning and a failure.
    with socket.socket() as sock, pytest.raises(NetworkAccessInUnitTest) as exc:
        sock.connect(("192.0.2.1", 80))  # TEST-NET-1, never routable
    message = str(exc.value)
    assert "test_socket_connect_is_blocked" in message
    assert "network" in message


def test_create_connection_is_blocked():
    with pytest.raises(NetworkAccessInUnitTest):
        socket.create_connection(("192.0.2.1", 80), timeout=1)


def test_grpc_channel_is_blocked():
    # grpcio's C core does its own syscalls, so the socket patch above does not
    # see it: with `socket.socket.connect` raising, a real channel to
    # zec.rocks:443 still came up. Hence the separate grpc seam in the guard.
    with pytest.raises(NetworkAccessInUnitTest):
        grpc.secure_channel("zec.rocks:443", grpc.ssl_channel_credentials())
    with pytest.raises(NetworkAccessInUnitTest):
        grpc.insecure_channel("zec.rocks:80")


def test_creating_a_socket_is_still_allowed():
    # Only connecting is refused — plenty of code constructs sockets (or asks
    # the OS about them) without doing any I/O, and blocking that would be
    # noise rather than a leak.
    socket.socket().close()


@pytest.mark.network
def test_guard_is_lifted_for_network_marked_tests():
    # A refused connection to localhost, not the guard: `NetworkAccessInUnitTest`
    # is not an OSError, so it would escape this `raises` instead of satisfying it.
    # Nothing leaves the machine, so this stays honest even off-line.
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", 1), timeout=1)
