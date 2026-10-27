"""A stand-in for `trajlab.store.query_child` in tests: noise on stderr, then the answer that
`FAKE_QUERY_CHILD` names, so the parent's reading of a child is tested without a store."""

import json
import os
import sys

mode = os.environ.get("FAKE_QUERY_CHILD", "refuse")
sys.stderr.write("x.py:1: DeprecationWarning: something in a dependency\n")
if mode == "refuse":
    json.dump({"error": "refused: fake"}, sys.stdout)
elif mode == "rows":
    json.dump({"rows": ['{"a":1}'], "truncated": False}, sys.stdout)
elif mode == "garbage":
    sys.stdout.write("not json\n")
else:
    sys.stderr.write("Traceback (most recent call last):\n  ...\nRuntimeError: the child died\n")
    sys.exit(3)
