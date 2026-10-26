"""The disposable process one agent query runs in (ADR-0019; `trajlab.store.query.run`).

    python -m trajlab.store.query_child <store> --cap N [--memory-limit SIZE] [--threads N]

The statement comes on stdin. On success the answer is one JSON object on stdout,
`{"rows": [<row JSON text>, ...], "truncated": bool}`, and the exit status is 0. A refusal or a
database error is its message on stderr with exit status 1; the parent reads nothing else.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from trajlab.store.query import REFUSED, StatementRefused, execute


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trajlab.store.query_child")
    parser.add_argument("store", type=Path)
    parser.add_argument("--cap", type=int, required=True)
    parser.add_argument("--memory-limit")
    parser.add_argument("--threads", type=int)
    args = parser.parse_args(argv)
    sql = sys.stdin.read()
    try:
        rows, truncated = execute(
            args.store, sql, cap=args.cap, memory_limit=args.memory_limit, threads=args.threads
        )
    except StatementRefused as error:
        sys.stderr.write(f"{REFUSED}{error}\n")
        return 1
    except Exception as error:  # DuckDB's and the store's errors alike: the message is the answer
        sys.stderr.write((str(error).strip() or type(error).__name__) + "\n")
        return 1
    json.dump({"rows": rows, "truncated": truncated}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
