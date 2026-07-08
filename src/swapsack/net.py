"""Shared HTTP client used by every chain adapter and the THORChain client.

Centralises the lazy-session + context-manager lifecycle that was previously
copy-pasted four times (A1 in docs/core-review.md). Uses niquests rather than
httpx.
"""

from __future__ import annotations

import niquests

# Network + HTTP-status errors worth catching at call sites.
HTTP_ERRORS = (niquests.exceptions.RequestException,)


class HttpClient:
    """A lazily-created, reusable HTTP session with a context-manager lifecycle."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout
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
        return self._http.get(url, timeout=self._timeout, **kwargs)

    def _post(self, url: str, **kwargs: object) -> niquests.Response:
        return self._http.post(url, timeout=self._timeout, **kwargs)
