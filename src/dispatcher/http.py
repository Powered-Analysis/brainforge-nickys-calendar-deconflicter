"""The one HTTP client for every call to Sentience's auth server and to Resend."""

import httpx2

TIMEOUT_SECONDS = 15.0

# Tests set this to replace the network; the app leaves it unset.
transport: httpx2.BaseTransport | None = None


def client() -> httpx2.Client:
    return httpx2.Client(transport=transport, timeout=TIMEOUT_SECONDS)
