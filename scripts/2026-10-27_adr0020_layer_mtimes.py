"""Dump every top-layer tar member of a trial's checkpoint images with its mtime (ADR-0020).

Used for the 2026-10-27 validation, when the schema-2 store kept no write times; since schema 3
`deltas.mtime_after` holds them, so this is only needed to look at members that are not delta
rows (directories, excluded paths).

    uv run python scripts/2026-10-27_adr0020_layer_mtimes.py <jobs dir> <out dir> <work dir>
        <trial name>...

For each trial writes <out_dir>/<trial_name>.jsonl, one line per member of each checkpoint's
top layer: {"seq", "path", "mtime", "size", "type", "whiteout"}. Uses the store's own
DockerImageProvider so the images are found and saved exactly as `trajlab store build` does.
"""

import json
import logging
import sys
import tarfile
import time
from pathlib import Path

from trajlab.contracts import CheckpointRecord
from trajlab.store.build import start_image
from trajlab.store.docker_images import DockerImageProvider
from trajlab.store.layers import normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extract_mtimes")


def members(stream):
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for info in tar:
            path = normalize(info.name)
            if path is None:
                continue
            base = path.rsplit("/", 1)[-1]
            whiteout = base.startswith(".wh.")
            yield {
                "path": path,
                "mtime": info.mtime,
                "size": info.size,
                "type": "dir"
                if info.isdir()
                else "file"
                if info.isfile()
                else "symlink"
                if info.issym()
                else "link"
                if info.islnk()
                else "other",
                "whiteout": whiteout,
            }


def main() -> None:
    jobs_dir, out_dir, work_dir, *names = sys.argv[1:]
    jobs_dir, out_dir, work_dir = Path(jobs_dir), Path(out_dir), Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        trial_dir = jobs_dir / name
        t0 = time.time()
        records = [
            CheckpointRecord.model_validate_json(line)
            for line in (trial_dir / "agent/checkpoints/checkpoints.jsonl").read_text().splitlines()
            if line.strip()
        ]
        image = start_image(trial_dir)
        if image is None:
            log.error("%s: no start image", name)
            continue
        out = out_dir / f"{name}.jsonl"
        n = 0
        with DockerImageProvider(work_dir, start_image=image) as provider:
            chain = provider.chain(records)
            missing = sorted(set(r.seq for r in records) - set(chain.tops))
            with out.open("w") as fh:
                for seq in sorted(chain.tops):
                    with chain.tops[seq].open() as stream:
                        for member in members(stream):
                            member["seq"] = seq
                            fh.write(json.dumps(member) + "\n")
                            n += 1
        log.info(
            "%s: %d checkpoints, %d members, missing seqs %s, %.1f s",
            name,
            len(chain.tops),
            n,
            missing,
            time.time() - t0,
        )


if __name__ == "__main__":
    main()
