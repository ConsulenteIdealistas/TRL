from __future__ import annotations

import argparse
import logging
from datetime import date

from .pipeline import ingest_issue, run_daily


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run-daily").add_argument("--today", default=None)

    p_ing = sub.add_parser("ingest-issue")
    p_ing.add_argument("--issue-number", type=int, required=True)

    args = ap.parse_args()

    if args.cmd == "run-daily":
        d = date.fromisoformat(args.today) if args.today else None
        run_daily(d)
    elif args.cmd == "ingest-issue":
        ingest_issue(args.issue_number)


if __name__ == "__main__":
    main()