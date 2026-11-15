"""The one function that opens a Sentience MCP session. Nothing else in the app calls an MCP client."""

import asyncio
from http import HTTPStatus

from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from dispatcher import settings
from dispatcher.errors import OperatorError

# Remote MCP Session Dispatch and Dispatch Tool Call: the only tool the dispatcher calls.
TOOL = "ask_sentience"


class DispatchFailed(OperatorError):
    """The conflict did not reach Sentience."""


class AccessTokenRejected(DispatchFailed):
    """Sentience's MCP server answered a request with 401."""


async def _ask_sentience(
    url: str, access_token: str, message: str, statuses: list[int]
) -> bool:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with create_mcp_http_client(headers=headers) as http_client:
        # The MCP client turns every HTTP error into one generic error, so the status that
        # tells a rejected token from any other failure is read from the responses.
        async def note_status(response) -> None:
            statuses.append(response.status_code)

        http_client.event_hooks["response"].append(note_status)
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport) as client:
            # `thread_id` is never sent, so every conflict opens its own Sentience chat.
            result = await client.call_tool(TOOL, {"message": message})
    return not result.is_error


def dispatch(access_token: str, message: str) -> None:
    """Deliver one conflict message, or raise DispatchFailed (AccessTokenRejected on a 401).

    Delivery: the call returning a result that is not an error. The reply is never read, and
    the failure messages name only the kind of failure, so neither the token nor anything
    Sentience said reaches a log.
    """
    url = settings.require("SENTIENCE_MCP_URL")
    statuses: list[int] = []
    try:
        delivered = asyncio.run(_ask_sentience(url, access_token, message, statuses))
    except Exception as exc:
        if HTTPStatus.UNAUTHORIZED in statuses:
            raise AccessTokenRejected("Sentience rejected the access token") from exc
        # Whatever the transport or the server raises, the conflict is not delivered.
        raise DispatchFailed(
            f"Sentience dispatch failed: {type(exc).__name__}"
        ) from exc
    if not delivered:
        raise DispatchFailed("Sentience answered the dispatch with an error result")
