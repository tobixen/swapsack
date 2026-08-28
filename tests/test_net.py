"""The shared HTTP client's retry policy.

A single slow Esplora reply used to abort a whole swap — after the passphrase
prompt, with a 60-line traceback (blockstream.info, read timeout=20). Reads are
replayable, so they are retried; writes are not.
"""

from __future__ import annotations

import niquests
import pytest

from swapsack.net import DEFAULT_RETRIES, HostUnreachable, HttpClient


class FakeSession:
    """Stands in for ``niquests.Session``: scripted outcomes, recorded calls."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.gets: list[str] = []
        self.posts: list[str] = []
        self.timeouts: list[float] = []

    def _next(self, url: str, recorded: list[str], timeout: float) -> object:
        recorded.append(url)
        self.timeouts.append(timeout)
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get(self, url: str, timeout: float = 0, **_kwargs: object) -> object:
        return self._next(url, self.gets, timeout)

    def post(self, url: str, timeout: float = 0, **_kwargs: object) -> object:
        return self._next(url, self.posts, timeout)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Swallow the backoff, and record what it would have slept."""
    slept: list[float] = []
    monkeypatch.setattr("swapsack.net.time.sleep", slept.append)
    return slept


def _client(*outcomes: object, **kwargs: object) -> tuple[HttpClient, FakeSession]:
    client = HttpClient(**kwargs)  # type: ignore[arg-type]
    session = FakeSession(*outcomes)
    client._session = session  # type: ignore[assignment]
    return client, session


def test_get_retries_a_read_timeout_and_succeeds(no_sleep):
    timeout = niquests.exceptions.ReadTimeout("read timed out")
    client, session = _client(timeout, timeout, "body")
    assert client._get("https://blockstream.info/api/address/bc1q") == "body"
    assert len(session.gets) == 3


def test_get_retries_a_connection_error(no_sleep):
    client, session = _client(niquests.exceptions.ConnectionError("no route"))
    assert client._get("https://example.invalid/x") == "ok"
    assert len(session.gets) == 2


def test_get_gives_up_and_raises_the_last_failure(no_sleep):
    fail = niquests.exceptions.ReadTimeout("read timed out")
    client, session = _client(*[fail] * 10)
    with pytest.raises(HostUnreachable):
        client._get("https://blockstream.info/api/address/bc1q")
    # One attempt plus the retries — bounded, not a loop against a dead host.
    assert len(session.gets) == DEFAULT_RETRIES + 1
    assert len(no_sleep) == DEFAULT_RETRIES


def test_backoff_grows_between_attempts(no_sleep):
    fail = niquests.exceptions.ConnectTimeout("connect timed out")
    client, _ = _client(*[fail] * 10)
    with pytest.raises(HostUnreachable):
        client._get("https://example.invalid/x")
    assert no_sleep == sorted(no_sleep) and no_sleep[0] > 0
    assert len(set(no_sleep)) == len(no_sleep)  # exponential, not a fixed sleep


def test_get_does_not_retry_a_non_transport_failure(no_sleep):
    # A TooManyRedirects (or any other RequestException that is not a transport
    # failure) means the peer answered: replaying it would just answer again.
    client, session = _client(niquests.exceptions.TooManyRedirects("loop"))
    with pytest.raises(niquests.exceptions.TooManyRedirects):
        client._get("https://example.invalid/x")
    assert len(session.gets) == 1


def test_post_is_never_retried(no_sleep):
    # A POST is a broadcast or an order. A read timeout there is ambiguous —
    # the peer may well have taken it — so it must not be silently re-sent.
    client, session = _client(niquests.exceptions.ReadTimeout("read timed out"))
    with pytest.raises(niquests.exceptions.ReadTimeout):
        client._post("https://blockstream.info/api/tx", data=b"raw")
    assert len(session.posts) == 1
    assert no_sleep == []


def test_retry_note_names_the_host_but_not_the_path(no_sleep, capsys):
    # The note goes to a terminal the user pastes into issues; an address in a
    # URL path is personal data, the host alone is not.
    client, _ = _client(niquests.exceptions.ReadTimeout("read timed out"))
    client._get("https://blockstream.info/api/address/bc1qsecretaddress")
    err = capsys.readouterr().err
    assert "blockstream.info" in err
    assert "bc1qsecretaddress" not in err


def test_retries_can_be_disabled(no_sleep):
    client, session = _client(
        niquests.exceptions.ReadTimeout("read timed out"), retries=0
    )
    with pytest.raises(HostUnreachable):
        client._get("https://example.invalid/x")
    assert len(session.gets) == 1


def test_giving_up_says_what_a_user_can_act_on(no_sleep):
    # The raw niquests message is "HTTPSConnectionPool(host=..., port=443):
    # Read timed out. (read timeout=20.0)" wrapped in 60 lines of urllib3
    # frames. What a user needs: which host, that it was tried more than once,
    # and that nothing was sent.
    # niquests/urllib3 put the *whole URL* in their own message, so the wrapper
    # must not simply interpolate it: that would re-leak the address it took
    # care not to print.
    fail = niquests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='blockstream.info', port=443): Max retries "
        "exceeded with url: /api/address/bc1qsecretaddress (Caused by ...)"
    )
    client, _ = _client(*[fail] * 10)
    with pytest.raises(HostUnreachable) as exc:
        client._get("https://blockstream.info/api/address/bc1qsecretaddress")
    message = str(exc.value)
    assert "blockstream.info" in message
    assert f"{DEFAULT_RETRIES + 1} attempts" in message
    assert "ReadTimeout" in message  # the class name, not its chatty message
    assert "bc1qsecretaddress" not in message  # the path is the user's business
    # The transport failure is kept as the cause, for a --debug traceback.
    assert isinstance(exc.value.__cause__, niquests.exceptions.ReadTimeout)


def test_host_unreachable_is_still_a_request_exception():
    # Every call site in the tree catches HTTP_ERRORS (RequestException); the
    # nicer message must not slip past them.
    assert issubclass(HostUnreachable, niquests.exceptions.RequestException)


def test_the_endpoint_hint_is_included_when_the_client_has_one(no_sleep):
    # Only the caller knows which flag points somewhere else, so it supplies
    # the one line of advice.
    fail = niquests.exceptions.ReadTimeout("read timed out")
    client, _ = _client(*[fail] * 10, hint="try --esplora https://example.org/api")
    with pytest.raises(HostUnreachable, match="--esplora"):
        client._get("https://blockstream.info/api/address/bc1q")


def test_reads_may_time_out_sooner_than_writes(no_sleep):
    # A stalled read never recovers, and these APIs answer in well under a
    # second, so a read gives up early and retries. A POST is a broadcast: it
    # is not retried, so it keeps the patient timeout.
    client, session = _client("ok", "ok", timeout=20.0, read_timeout=8.0)
    client._get("https://blockstream.info/api/blocks/tip/height")
    client._post("https://blockstream.info/api/tx", data=b"raw")
    assert session.timeouts == [8.0, 20.0]


def test_read_timeout_defaults_to_the_general_timeout(no_sleep):
    client, session = _client("ok", timeout=12.0)
    client._get("https://example.org/x")
    assert session.timeouts == [12.0]
