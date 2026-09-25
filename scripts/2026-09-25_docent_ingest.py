"""Load a finished Harbor job into a Docent collection (experiment 1, stock baseline).

Usage:
    set -a && source .env && set +a
    uv run --with docent python scripts/2026-09-25_docent_ingest.py corpus/jobs/<job>

Docent is kept out of pyproject.toml on purpose: this repo's dependencies are capture only.
"""

import sys
from pathlib import Path

from docent import Docent
from docent.sdk.integrations import convert_harbor_directory_to_agent_runs

job = Path(sys.argv[1])
runs = convert_harbor_directory_to_agent_runs(job)  # one AgentRun per trial
client = Docent()  # reads DOCENT_API_KEY
cid = client.create_collection(name=f"tb-{job.name}")
client.add_agent_runs(cid, runs)
print(f"{len(runs)} runs -> collection {cid}")
