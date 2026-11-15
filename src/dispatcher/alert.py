"""The Re-Authorization Alert: the one message the dispatcher sends itself."""

import logging

import httpx

from dispatcher import settings
from dispatcher.errors import OperatorError

RESEND_URL = "https://api.resend.com/emails"
SENDER = "Calendar Deconflicter <onboarding@resend.dev>"
SUBJECT = "[Calendar Deconflicter] Alert \N{EM DASH} Sentience needs re-authorization"

log = logging.getLogger(__name__)


def body(reauth_link: str) -> str:
    # No event data: the email leaves for a mailbox the dispatcher does not control.
    return (
        f"Conflicts are not being delivered to Sentience; reconnect it at {reauth_link}"
    )


def send_reauthorization_alert(client: httpx.Client, reauth_link: str) -> None:
    """Email the operator. A failure is logged, never raised: the alert is sent once per
    expiry, and nothing else depends on it. The log names no key or address."""
    try:
        response = client.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {settings.require('RESEND_API_KEY')}"},
            json={
                "from": SENDER,
                "to": [settings.require("ALERT_TO")],
                "subject": SUBJECT,
                "text": body(reauth_link),
            },
        )
    except (httpx.HTTPError, OperatorError) as exc:
        log.warning("Re-authorization alert not sent: %s", type(exc).__name__)
        return
    if not response.is_success:
        log.warning("Re-authorization alert not sent: HTTP %s", response.status_code)
