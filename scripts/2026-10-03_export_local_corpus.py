"""Export everything the local runs hold for later figures, before the local Docker is wiped.

Two parts, written under `corpus/exports/<name>/` (ignored by git, like `corpus/jobs/`):

- `docker/`: what only Docker holds. `images.jsonl` has one row per trajlab image (checkpoint
  and preinstalled) with size, labels, layer digests, and per-layer sizes from `docker history`;
  the top layer of a checkpoint is its commit delta, and shared digests give deduplicated
  storage. `listings/` has a full `find / -xdev` listing of each such image, in the change
  detector's own NUL-separated format (`trajlab.checkpoint.changes.parse_listing` reads it), so
  file-level diffs between consecutive checkpoints stay computable after the images are gone.
- `tables/`: every local trial flattened from its trial dir: `trials.csv`, `checkpoints.csv`
  (each record joined to its image, if the image survived), `calls.csv`, plus
  `trial_reports/` from `2026-10-02_trial_report.py` for postprocessed trials.

    uv run python scripts/2026-10-03_export_local_corpus.py [--name local-2026-10-03]
"""

import argparse
import csv
import gzip
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from harbor.models.trial.result import TrialResult

from trajlab.checkpoint.changes import FIND_PRINTF, parse_listing
from trajlab.contracts import (
    CALLS_FILENAME,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    POLICY_FILENAME,
    PREINSTALL_RECORD_FILENAME,
    RESUME_RECORD_FILENAME,
)

logger = logging.getLogger("export")

REPO = Path(__file__).resolve().parent.parent
JOBS = REPO / "corpus/jobs"
REPOSITORIES = ("trajlab-checkpoint", "trajlab-preinstalled")
KEPT_LABEL_PREFIXES = ("trajlab.", "org.harborframework.", "org.opencontainers.image.")
REPORT_SCRIPT = REPO / "scripts/2026-10-02_trial_report.py"


def docker(*args: str, timeout: int = 300) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def export_images(out: Path) -> dict[str, dict[str, Any]]:
    """Write images.jsonl and one listing per image; return the rows by full image id."""
    (out / "listings").mkdir(parents=True, exist_ok=True)
    (out / "system_df.txt").write_text(docker("system", "df", "-v"))
    ids: list[str] = []
    for repository in REPOSITORIES:
        ids += docker("images", "--no-trunc", "-q", repository).split()
    rows: dict[str, dict[str, Any]] = {}
    with (out / "images.jsonl").open("w") as sink:
        for n, image_id in enumerate(dict.fromkeys(ids), 1):
            inspect = json.loads(docker("image", "inspect", image_id))[0]
            labels = inspect["Config"].get("Labels") or {}
            history = [
                json.loads(line)
                for line in docker(
                    "history", "--no-trunc", "--human=false", "--format", "{{json .}}", image_id
                ).splitlines()
            ]
            row = {
                "image_id": image_id,
                "repo_tags": inspect.get("RepoTags") or [],
                "size": inspect["Size"],
                "created": inspect["Created"],
                "architecture": inspect.get("Architecture"),
                "labels": {k: v for k, v in labels.items() if k.startswith(KEPT_LABEL_PREFIXES)},
                "layers": inspect["RootFS"].get("Layers", []),
                # Newest first, as docker history prints; size in bytes.
                "history": [
                    {"created_by": h["CreatedBy"][:300], "size": int(h["Size"]), "id": h["ID"]}
                    for h in history
                ],
            }
            row["listing"] = listing(image_id, row, out / "listings")
            sink.write(json.dumps(row) + "\n")
            rows[image_id] = row
            logger.info("image %d/%d %s", n, len(ids), row["repo_tags"][:1])
    return rows


def listing(image_id: str, row: dict[str, Any], directory: Path) -> dict[str, Any]:
    labels = row["labels"]
    if "trajlab.trial_name" in labels:
        name = f"{labels['trajlab.trial_name']}/{int(labels['trajlab.seq']):04d}.find.gz"
    else:
        name = f"_preinstalled/{image_id.removeprefix('sha256:')[:16]}.find.gz"
    target = directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-u",
            "0",
            "--entrypoint",
            "find",
            image_id,
            "/",
            "-xdev",
            "-printf",
            FIND_PRINTF,
        ],  # fmt: skip
        capture_output=True,
        timeout=600,
    )
    raw = completed.stdout.decode(errors="surrogateescape")
    try:
        entries = len(parse_listing(raw))
    except ValueError as error:
        logger.warning("unparseable listing for %s: %s", image_id, error)
        return {"path": None, "error": str(error), "returncode": completed.returncode}
    with gzip.open(target, "wb") as sink:
        sink.write(completed.stdout)
    return {
        "path": str(target.relative_to(directory.parent)),
        "entries": entries,
        "returncode": completed.returncode,
        "stderr": completed.stderr.decode(errors="replace")[-500:],
    }


def trial_dirs() -> list[Path]:
    return sorted(p.parent for p in JOBS.glob("*/*/result.json") if (p.parent / "agent").is_dir())


def read_jsonl(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line] if path.exists() else []


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as sink:
        writer = csv.DictWriter(sink, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: json.dumps(v) if isinstance(v, dict | list) else v for k, v in row.items()}
            )


def export_tables(out: Path, images: dict[str, dict[str, Any]]) -> None:
    (out / "trial_reports").mkdir(parents=True, exist_ok=True)
    trials: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for trial in trial_dirs():
        job = trial.parent.name
        try:
            result = TrialResult.model_validate_json((trial / "result.json").read_text())
        except ValueError as error:
            logger.warning("skip %s: %s", trial, error)
            continue
        ckdir = trial / "agent" / CHECKPOINTS_DIRNAME
        # As written, not validated: early trials predate later CheckpointRecord fields.
        records = [json.loads(x) for x in read_jsonl(ckdir / CHECKPOINT_RECORDS_FILENAME)]
        call_records = [json.loads(x) for x in read_jsonl(ckdir / CALLS_FILENAME)]
        agent = result.agent_result
        rewards = result.verifier_result.rewards if result.verifier_result else None
        extra: dict[str, Any] = {}
        for key, filename in (
            ("policy", POLICY_FILENAME),
            ("resume", RESUME_RECORD_FILENAME),
            ("preinstall", PREINSTALL_RECORD_FILENAME),
        ):
            found = sorted(trial.rglob(filename))
            extra[key] = json.loads(found[0].read_text()) if found else None
        trials.append(
            {
                "job": job,
                "trial_name": result.trial_name,
                "trial_id": str(result.id),
                "task": result.task_name,
                "agent": result.agent_info.name if result.agent_info else None,
                "agent_version": result.agent_info.version if result.agent_info else None,
                "model": (
                    result.agent_info.model_info.name
                    if result.agent_info and result.agent_info.model_info
                    else None
                ),
                "reward": (rewards or {}).get("reward"),
                "exception": (
                    result.exception_info.exception_type if result.exception_info else None
                ),
                "input_tokens": agent.n_input_tokens if agent else None,
                "cache_tokens": agent.n_cache_tokens if agent else None,
                "output_tokens": agent.n_output_tokens if agent else None,
                "cost_usd": agent.cost_usd if agent else None,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                **{
                    f"{phase}_s": (
                        (span.finished_at - span.started_at).total_seconds()
                        if (span := getattr(result, phase)) and span.started_at and span.finished_at
                        else None
                    )
                    for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier")
                },
                "n_checkpoints": len(records),
                "n_checkpoint_images_kept": sum(r["checkpoint_id"] in images for r in records),
                "n_hooked_calls": len(call_records),
                "enriched": (trial / "agent/trajectory.enriched.json").exists(),
                **extra,
            }
        )
        for r in records:
            image = images.get(r["checkpoint_id"])
            checkpoints.append(
                {
                    "job": job,
                    **r,
                    "image_kept": image is not None,
                    "image_size": image["size"] if image else None,
                    "top_layer_bytes": image["history"][0]["size"] if image else None,
                    "n_layers": len(image["layers"]) if image else None,
                    "listing": image["listing"]["path"] if image else None,
                }
            )
        calls += [{"job": job, **c} for c in call_records]
        if records and call_records and (trial / "agent/trajectory.enriched.json").exists():
            report = subprocess.run(
                [sys.executable, str(REPORT_SCRIPT), str(trial)], capture_output=True, text=True
            )
            if report.returncode == 0:
                target = out / "trial_reports" / job / f"{trial.name}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(report.stdout)
            else:
                logger.warning("report failed for %s: %s", trial, report.stderr[-300:])
    write_csv(out / "trials.csv", trials)
    write_csv(out / "checkpoints.csv", checkpoints)
    write_csv(out / "calls.csv", calls)
    logger.info("%d trials, %d checkpoints, %d calls", len(trials), len(checkpoints), len(calls))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="local-2026-10-03")
    parser.add_argument("--skip-docker", action="store_true", help="Reuse docker/images.jsonl.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = REPO / "corpus/exports" / args.name
    images_path = out / "docker/images.jsonl"
    if args.skip_docker:
        images = {
            row["image_id"]: row for row in map(json.loads, images_path.read_text().splitlines())
        }
    else:
        images = export_images(out / "docker")
    (out / "tables").mkdir(parents=True, exist_ok=True)
    export_tables(out / "tables", images)
    return 0


if __name__ == "__main__":
    sys.exit(main())
