import argparse
import sys
import time
import webbrowser

from dispatcher import attach, http, sentience_auth, settings, watches
from dispatcher.errors import OperatorError
from dispatcher.google_calendar import GoogleCalendar


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
    calendar = commands.add_parser("calendar", help="manage the monitored calendars")
    calendar_actions = calendar.add_subparsers(
        title="actions", dest="action", required=True
    )
    attach_parser = calendar_actions.add_parser(
        "attach", help="put calendars you own under conflict monitoring"
    )
    attach_parser.add_argument(
        "calendar_id",
        nargs="?",
        help=f"a calendar's ID, or `{attach.ALL_CALENDARS}` for every calendar you own",
    )
    attach_parser.add_argument(
        "--auto-discover",
        action="store_true",
        help=f"the same as `{attach.ALL_CALENDARS}`",
    )
    detach_parser = calendar_actions.add_parser(
        "detach", help="stop monitoring a calendar and close its Google subscription"
    )
    detach_parser.add_argument("calendar_id", help="the ID of an attached calendar")
    return parser


def auth_sentience() -> None:
    with http.client() as client:
        url = sentience_auth.start_authorization(client, int(time.time()))
    print(f"Sign in to Sentience at:\n{url}")
    open_url(url)


def _has_valid_target(args: argparse.Namespace) -> bool:
    if args.auto_discover:
        return args.calendar_id in (None, attach.ALL_CALENDARS)
    return args.calendar_id is not None


def calendar_attach(calendar_id: str | None, auto_discover: bool) -> int:
    """Print what happened to each calendar; the exit status is nonzero if any did not attach."""
    # Every setting is checked before the first call to Google.
    address = attach.webhook_address(settings.require("PUBLIC_BASE_URL"))
    google = GoogleCalendar()
    now = int(time.time())
    if auto_discover or calendar_id == attach.ALL_CALENDARS:
        outcomes = attach.attach_all(google, address, now)
        if not outcomes:
            print("You own no calendars.")
    else:
        outcomes = [attach.attach_one(google, calendar_id, address, now)]
    failed = False
    for outcome in outcomes:
        detail = f": {outcome.detail}" if outcome.detail else ""
        succeeded = outcome.result in ("attached", "skipped")
        print(
            f"{outcome.result} {outcome.calendar_id}{detail}",
            file=sys.stdout if succeeded else sys.stderr,
        )
        failed = failed or not succeeded
    return 1 if failed else 0


def calendar_detach(calendar_id: str) -> None:
    watches.detach(GoogleCalendar(), calendar_id)
    print(f"detached {calendar_id}")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    if (
        args.command == "calendar"
        and args.action == "attach"
        and not _has_valid_target(args)
    ):
        parser.error("calendar attach takes a calendar ID, `all`, or --auto-discover")
    try:
        if args.command == "auth":
            auth_sentience()
        elif args.action == "detach":
            calendar_detach(args.calendar_id)
        else:
            status = calendar_attach(args.calendar_id, args.auto_discover)
            if status:
                raise SystemExit(status)
    except OperatorError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
