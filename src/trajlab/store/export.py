"""The `record-files` bundle: a store's record exported to files (ADR-0018, technical spec §3.4).

`export(store_dir, out_dir)` writes, from `record.duckdb` only:

    <out>/README.md                                      the schema text (trajlab.store.describe)
    <out>/<relation>.jsonl                               every row, one JSON object per line
    <out>/deltas/<trial_id>/<step_idx>/<path>.diff       each delta row's hunk again, as a file

for every relation an agent's connection can read: `trials`, `steps`, `deltas`, the derived
views, and `store_meta`. A row's JSON is DuckDB's own (UUIDs and instants as strings, instants in
UTC, lists as arrays), in key order. Nothing comes from a trial dir or `heldout.duckdb`, so the
bundle holds exactly what the `record-engine` arm can query; it has no blob contents for the
same reason (`connect_agent` cannot read `blobs/`, and `blob_ref` names them in both arms).

The bundle is written beside `out_dir`, checked with `verify`, and only then renamed into place,
so `out_dir` either holds a verified bundle or does not exist.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

import duckdb

from trajlab.store.describe import columns, describe, relations
from trajlab.store.schema import connect, record_path

README_FILENAME = "README.md"
HUNKS_DIRNAME = "deltas"
HUNK_SUFFIX = ".diff"

_LAYOUT = f"""\
# The record

Every file here is one relation of the record: `<relation>.jsonl` holds its rows, one JSON
object per line, with the columns below. Instants are UTC. Each `deltas` row's `hunk` is also in
`{HUNKS_DIRNAME}/<trial_id>/<step_idx>/<path>{HUNK_SUFFIX}`.

"""


class ExportError(ValueError):
    """The bundle cannot be written, or does not match its store."""


@dataclass(frozen=True)
class ExportResult:
    out_dir: Path
    rows: dict[str, int]
    hunk_files: int


def readme(con: duckdb.DuckDBPyConnection) -> str:
    return _LAYOUT + describe(con)


def hunk_file(out_dir: Path, trial_id: UUID | str, step_idx: int, path: str) -> Path:
    """Where a delta row's hunk goes in a bundle."""
    parts = PurePosixPath(path).parts
    if not path.startswith("/") or any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise ExportError(f"delta path {path!r} is not a normalized absolute path")
    *dirs, name = parts[1:]
    return out_dir.joinpath(HUNKS_DIRNAME, str(trial_id), str(step_idx), *dirs, name + HUNK_SUFFIX)


def _literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _hunks(con: duckdb.DuckDBPyConnection) -> list[tuple[UUID, int, str, str]]:
    return con.execute(
        "SELECT trial_id, step_idx, path, hunk FROM deltas WHERE hunk IS NOT NULL ORDER BY ALL"
    ).fetchall()


def export(store_dir: Path, out_dir: Path) -> ExportResult:
    """Write the store's record to `out_dir`, which must not exist, and verify it.

    Raises `ExportError` if `out_dir` exists, a delta path cannot be a file name (or two would
    be the same file there), or the written bundle does not read back as the store;
    `StoreError` if the store cannot be opened.
    """
    if out_dir.exists():
        raise ExportError(f"{out_dir} exists; export writes a new directory")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.", dir=out_dir.parent))
    try:
        with connect(record_path(store_dir), read_only=True) as con:
            (tmp / README_FILENAME).write_text(readme(con), encoding="utf-8")
            rows: dict[str, int] = {}
            for name in relations():
                target = tmp / f"{name}.jsonl"
                written = con.execute(
                    f"COPY (SELECT * FROM {name} ORDER BY ALL) TO {_literal(target)} (FORMAT json)"
                ).fetchone()
                rows[name] = written[0] if written else 0
            hunks = _hunks(con)
            targets = [
                hunk_file(tmp, trial_id, step_idx, path) for trial_id, step_idx, path, _ in hunks
            ]
            _check_distinct(tmp, targets, [path for _, _, path, _ in hunks])
            for (_, _, path, hunk), target in zip(hunks, targets, strict=True):
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(hunk.encode("utf-8"))
                except OSError as error:
                    raise ExportError(f"cannot write the hunk of {path}: {error}") from error
            problems = _verify(con, tmp)
        if problems:
            raise ExportError(f"the bundle does not match {store_dir}: {'; '.join(problems)}")
        os.rename(tmp, out_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return ExportResult(out_dir=out_dir, rows=rows, hunk_files=len(hunks))


def _check_distinct(root: Path, targets: list[Path], paths: list[str]) -> None:
    """Refuse hunk files that would be one file, or a file and a directory, under `root`: paths
    differing only in case on a case-insensitive file system, or a path `x` beside `x.diff/y`."""
    probe = root / "case-probe"
    probe.touch()
    folded = (root / "CASE-PROBE").exists()
    probe.unlink()

    def key(target: Path) -> str:
        relative = target.relative_to(root).as_posix()
        return relative.casefold() if folded else relative

    seen: dict[str, str] = {}
    for target, path in zip(targets, paths, strict=True):
        if (other := seen.setdefault(key(target), path)) != path:
            raise ExportError(f"{other} and {path} would be one hunk file here")
    for target, path in zip(targets, paths, strict=True):
        for parent in target.relative_to(root).parents:
            name = parent.as_posix().casefold() if folded else parent.as_posix()
            if name in seen:
                raise ExportError(f"{seen[name]} and {path} would be a file and a directory here")


def verify(store_dir: Path, out_dir: Path) -> list[str]:
    """Every way the bundle in `out_dir` differs from the store's record; empty if none."""
    if not out_dir.is_dir():
        return [f"{out_dir} is not a directory"]
    with connect(record_path(store_dir), read_only=True) as con:
        return _verify(con, out_dir)


def _verify(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[str]:
    problems: list[str] = []
    names = relations()
    expected = {README_FILENAME, HUNKS_DIRNAME, *(f"{name}.jsonl" for name in names)}
    found = {entry.name for entry in out_dir.iterdir()}
    if extra := sorted(found - expected):
        problems.append(f"files that are not the record's: {extra}")
    # No hunk at all leaves no deltas/ dir; a missing hunk file is _compare_hunks' to report.
    if missing := sorted(expected - {HUNKS_DIRNAME} - found):
        problems.append(f"missing: {missing}")
    readme_path = out_dir / README_FILENAME
    if readme_path.is_file() and readme_path.read_text(encoding="utf-8") != readme(con):
        problems.append(f"{README_FILENAME} is not the record's schema text")
    for name in names:
        path = out_dir / f"{name}.jsonl"
        if path.is_file():
            problems.extend(_compare(con, name, path))
    problems.extend(_compare_hunks(con, out_dir))
    return problems


def _compare(con: duckdb.DuckDBPyConnection, name: str, path: Path) -> list[str]:
    """Differences between a relation and its file: keys, then rows (as multisets)."""
    cols = columns(con, name)
    names = [column for column, _ in cols]
    keys = con.execute(
        f"SELECT DISTINCT json_keys(json) FROM read_json_objects({_literal(path)}, "
        "format = 'newline_delimited')"
    ).fetchall()
    if any(row_keys != names for (row_keys,) in keys):
        return [f"{path.name}: rows whose keys are not {names}"]
    spec = ", ".join(f"{json.dumps(column)}: {json.dumps(sql)}" for column, sql in cols)
    as_read = (
        f"SELECT {', '.join(names)} FROM read_json({_literal(path)}, "
        f"format = 'newline_delimited', columns = {{{spec}}})"
    )
    try:
        (missing,) = con.execute(  # type: ignore[misc]
            f"SELECT count(*) FROM (SELECT * FROM {name} EXCEPT ALL {as_read})"
        ).fetchone()
        (extra,) = con.execute(  # type: ignore[misc]
            f"SELECT count(*) FROM ({as_read} EXCEPT ALL SELECT * FROM {name})"
        ).fetchone()
    except duckdb.Error as error:
        return [f"{path.name}: unreadable as {name} ({error})"]
    if missing or extra:
        return [f"{path.name}: {missing} rows of {name} missing, {extra} rows not in it"]
    return []


def _compare_hunks(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[str]:
    expected: dict[Path, str] = {}
    for trial_id, step_idx, path, hunk in _hunks(con):
        expected[hunk_file(out_dir, trial_id, step_idx, path)] = hunk
    root = out_dir / HUNKS_DIRNAME
    found = {p for p in root.rglob("*") if p.is_file()} if root.is_dir() else set()
    problems: list[str] = []
    if extra := sorted(str(p.relative_to(out_dir)) for p in found - expected.keys()):
        problems.append(f"hunk files with no delta row: {extra[:5]} ({len(extra)} in all)")
    if missing := sorted(str(p.relative_to(out_dir)) for p in expected.keys() - found):
        problems.append(f"missing hunk files: {missing[:5]} ({len(missing)} in all)")
    changed = [
        str(p.relative_to(out_dir))
        for p in sorted(expected.keys() & found)
        if p.read_bytes() != expected[p].encode("utf-8")
    ]
    if changed:
        problems.append(f"hunk files that differ from their row: {changed[:5]}")
    return problems
