"""The disposable process one agent query runs in (ADR-0019; `trajlab.store.query.run`).

    python -m trajlab.store.query_child <store> --cap N [--memory-limit SIZE] [--threads N]

The statement comes on stdin. The answer is one JSON object on stdout with exit status 0:
`{"rows": [<row JSON text>, ...], "truncated": bool}`, or `{"error": <message>}` for a refusal
or a database error. Nothing on stderr is part of the answer, so a warning printed there (by
Python, DuckDB, or a dependency) cannot get into a result or in front of a refusal's prefix;
any other exit status means the child died, and the parent reports that.
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
        answer: dict[str, object] = {"error": f"{REFUSED}{error}"}
    except Exception as error:  # DuckDB's and the store's errors alike: the message is the answer
        answer = {"error": str(error).strip() or type(error).__name__}
    else:
        answer = {"rows": rows, "truncated": truncated}
    json.dump(answer, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
