"""
HTTP retry policy shared by the Qase SDK clients and the raw ``requests`` calls.

Transient failures are retried with exponential backoff: network errors,
timeouts, 408, 429 and 5xx. A ``Retry-After`` header is honoured when the server
sends one. Every other 4xx is deterministic and fails immediately, because
retrying a 404 only adds minutes of sleep before the same error.

The limits are constants on purpose. They are not configuration.
"""
from __future__ import annotations

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RETRY_STATUSES = (408, 429, 500, 502, 503, 504)


class _BackoffRetry(Retry):
    """
    Honour ``Retry-After``, but never sleep less than our own backoff. A short
    Retry-After under sustained load would otherwise use up every attempt in a
    few seconds while the rate limit window is still full.
    """

    def sleep(self, response=None):
        retry_after = self.get_retry_after(response) if response is not None else None
        delay = max(retry_after or 0.0, self.get_backoff_time())
        if delay > 0:
            time.sleep(delay)


def build_retry() -> Retry:
    return _BackoffRetry(
        total=8,
        connect=6,
        read=3,
        status=8,
        backoff_factor=1.5,
        backoff_max=60,
        status_forcelist=RETRY_STATUSES,
        # Retry every method: the script already re-sent POSTs on 429/5xx, and a
        # write that fails halfway is recorded in the mappings for resume.
        allowed_methods=None,
        respect_retry_after_header=True,
        # Hand the final response back instead of raising, so callers keep their
        # existing status-code handling and error messages.
        raise_on_status=False,
    )


session = requests.Session()
_adapter = HTTPAdapter(max_retries=build_retry(), pool_connections=16, pool_maxsize=64)
session.mount("https://", _adapter)
session.mount("http://", _adapter)

get = session.get
post = session.post
put = session.put
patch = session.patch
request = session.request
