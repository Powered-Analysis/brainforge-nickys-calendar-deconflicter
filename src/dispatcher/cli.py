import argparse
import sys
import time
import webbrowser

from dispatcher import http, sentience_auth
from dispatcher.errors import OperatorError


def open_url(url: str) -> None:
    """The one place the operator's browser is opened."""
    webbrowser.open(url)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dispatcher",
        description=(
            "Detects conflicts across the operator's Google Calendars and hands each "
            "one to the operator's Sentience agent."
        ),
    )
    commands = parser.add_subparsers(title="commands", dest="command")
    auth = commands.add_parser("auth", help="connect an account")
    auth_targets = auth.add_subparsers(title="accounts", dest="target", required=True)
    auth_targets.add_parser(
        "sentience", help="register with Sentience and sign in through the browser"
    )
    return parser


def auth_sentience() -> None:
    with http.client() as client:
        url = sentience_auth.start_authorization(client, int(time.time()))
    print(f"Sign in to Sentience at:\n{url}")
    open_url(url)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    try:
        auth_sentience()
    except OperatorError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
