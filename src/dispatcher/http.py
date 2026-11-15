"""The one HTTP client for every call to Sentience's auth server and to Resend."""

import httpx

TIMEOUT_SECONDS = 15.0

# Tests set this to replace the network; the app leaves it unset.
transport: httpx.BaseTransport | None = None


def client() -> httpx.Client:
    return httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS)
