# Corpus data

Job directories are not in git. Each corpus version has a manifest in `manifests/<corpus_id>.json`
naming the Harbor version, repo sha, config, task list, model, attempts, and where the data lives.

Storage location: `/srv/trajlab/jobs` on the Linux server (group-writable shared folder). In the
checkout there, `corpus/jobs` is a symlink to it, so configs keep `"jobs_dir": "corpus/jobs"`.
Pass `--storage /srv/trajlab/jobs` to `trajlab run` and `trajlab repair` so the manifest records it.

## Experiments

| Experiment | Corpus ids | Checkpoints | Design |
| --- | --- | --- | --- |
| 1: TB 4.0 repair | `tb40-sonnet-v2` (first attempts), `tb40-repair-v2-<trial>-<arm>` (repairs) | `docker commit` only: filesystem state, no CRIU, no process or memory state | ADR-0012 |
| 2: TB 4.0 repair with running programs | `tb40-sonnet-wp1` (first attempts), `tb40-repair-wp1-<trial>-<arm>` (repairs) | one Waypoint save per first attempt (files and running programs, before the tests); no per-call checkpoints | ADR-0013, `docs/waypoint-runbook.md` |

Earlier corpora (`hello-world-*`, `tb21-*`, `tb40-sonnet-v1*`, `tb40-repair-v1-*`) are development
runs and the Mac pilot, also `docker commit` only.
