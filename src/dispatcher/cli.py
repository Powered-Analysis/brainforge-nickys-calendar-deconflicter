import argparse


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m dispatcher",
        description=(
            "Detects conflicts across the operator's Google Calendars and hands each "
            "one to the operator's Sentience agent."
        ),
    )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
