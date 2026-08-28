"""Shared HTTP client used by every chain adapter and the THORChain client.

Centralises the lazy-session + context-manager lifecycle that was previously
copy-pasted four times (A1 in docs/core-review.md). Uses niquests rather than
httpx.

Reads are retried and report themselves in a line a user can act on; writes are
neither — see :meth:`HttpClient._get` and :class:`HostUnreachable`.
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlsplit

import niquests

# Network + HTTP-status errors worth catching at call sites.
HTTP_ERRORS = (niquests.exceptions.RequestException,)

# Transport failures where no answer was received: the request either never
# arrived or its reply never came back, so replaying a read is safe.
TRANSIENT_ERRORS = (
    niquests.exceptions.ConnectionError,  # also covers ProxyError/SSLError
    niquests.exceptions.Timeout,  # ConnectTimeout + ReadTimeout
)

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF = 1.0


class HostUnreachable(niquests.exceptions.RequestException):
    """A host that failed every attempt, said in one line instead of sixty.

    Subclasses ``RequestException`` on purpose: every call site in the tree
    already catches ``HTTP_ERRORS``, and this must not slip past them. The
    original transport error is kept as ``__cause__``.
    """


def _describe(
    host: str, exc: Exception, attempts: int, timeout: float, hint: str | None
) -> str:
    """One actionable line: which host, what went wrong, how hard we tried.

    Deliberately *without* the URL path — an Esplora path is one of the
    wallet's own addresses, and this text ends up in terminals, log paste-ins
    and bug reports. That rules out interpolating ``exc``: niquests and urllib3
    put the whole URL in their own message. Only the class name is quoted; the
    original stays reachable as ``__cause__``.
    """
    reason = {
        niquests.exceptions.ReadTimeout: "connected, then never answered",
        niquests.exceptions.ConnectTimeout: "did not accept the connection",
        niquests.exceptions.SSLError: "failed the TLS handshake",
    }.get(type(exc), "could not be reached")
    line = (
        f"{host} {reason} — gave up after {attempts} attempts "
        f"({type(exc).__name__}, {timeout:g}s timeout)"
    )
    return f"{line}\n{hint}" if hint else line


class HttpClient:
    """A lazily-created, reusable HTTP session with a context-manager lifecycle."""

    def __init__(
        self,
        timeout: float = 20.0,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        read_timeout: float | None = None,
        hint: str | None = None,
    ) -> None:
        self._timeout = timeout
        # A stalled read never recovers, so a caller talking to a flaky API may
        # give up on one sooner than on a write and let the retry do the work.
        self._read_timeout = timeout if read_timeout is None else read_timeout
        self._retries = retries
        self._backoff = backoff
        self._hint = hint
        self._session: niquests.Session | None = None

    @property
    def _http(self) -> niquests.Session:
        if self._session is None:
            self._session = niquests.Session()
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, url: str, **kwargs: object) -> niquests.Response:
        """GET ``url``, retrying a transient transport failure with backoff.

        The public APIs this talks to drop requests. Measured on 2026-08-28,
        ``blockstream.info`` black-holed roughly one request in twenty — TCP and
        TLS complete, then nothing comes back — with plain ``curl``, sequential
        or concurrent, over both IPv4 and IPv6. An HD account scan makes dozens
        of calls, so without a retry a stall was near-certain, and it aborted
        the whole swap after the passphrase prompt.

        niquests can retry this itself (``Session(retries=...)``), but it is off
        by default (``Retry(total=0, read=False)``). The loop is here instead so
        the retry can be reported with the host and *not* the URL path, and so
        the give-up carries :class:`HostUnreachable` rather than a bare timeout.

        GETs only. A POST here is a broadcast or an order, where a read timeout
        is *ambiguous*: the peer may well have taken it, so re-sending it could
        double-submit. Those are raised as-is, for a human to resolve.
        """
        host = urlsplit(url).netloc
        attempt = 0
        while True:
            try:
                return self._http.get(url, timeout=self._read_timeout, **kwargs)
            except TRANSIENT_ERRORS as exc:
                if attempt >= self._retries:
                    raise HostUnreachable(
                        _describe(
                            host, exc, attempt + 1, self._read_timeout, self._hint
                        )
                    ) from exc
                print(
                    f"note: {host} {type(exc).__name__}, "
                    f"retrying ({attempt + 1}/{self._retries})",
                    file=sys.stderr,
                )
                time.sleep(self._backoff * 2**attempt)
                attempt += 1

    def _post(self, url: str, **kwargs: object) -> niquests.Response:
        return self._http.post(url, timeout=self._timeout, **kwargs)
