"""The record-files bundle (ADR-0018): the acceptance check of Exp 2 build-order step 3 (the
fixture's store exports and reads back exactly), and every way `verify` catches a difference."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.test_store_build import HUNK, TRIAL_ID, all_images, capture
from tests.test_store_derive import as_schema, change, make_store, script
from tests.test_store_derive import call as make_call_row
from trajlab.cli import app
from trajlab.store import schema
from trajlab.store.build import build_trial
from trajlab.store.describe import DescribeError, describe, relations
from trajlab.store.export import ExportError, export, hunk_file, readme, verify


@pytest.fixture
def store(trial_copy: Path, tmp_path: Path) -> Path:
    """The fixture trial built with its images: one Write call that adds /app/hello.txt."""
    capture(trial_copy)
    store = tmp_path / "store"
    build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "work")
    return store


@pytest.fixture
def bundle(store: Path, tmp_path: Path) -> Path:
    out = tmp_path / "bundle"
    export(store, out)
    return out


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_fixture_exports_and_reads_back(store: Path, bundle: Path) -> None:
    assert sorted(entry.name for entry in bundle.iterdir()) == sorted(
        ["README.md", "deltas", *(f"{name}.jsonl" for name in relations())]
    )
    assert verify(store, bundle) == []
    (delta,) = lines(bundle / "deltas.jsonl")
    assert delta["path"] == "/app/hello.txt"
    assert delta["hunk"] == HUNK
    assert hunk_file(bundle, TRIAL_ID, 1, "/app/hello.txt").read_text() == HUNK
    (summary,) = lines(bundle / "trial_summary.jsonl")
    assert summary["trial_id"] == str(TRIAL_ID)
    assert summary["mutating_calls"] == 1
    assert [row["step_idx"] for row in lines(bundle / "steps.jsonl")] == [1, 2, 3]


def test_the_bundle_holds_every_relation_the_agent_can_read(store: Path, bundle: Path) -> None:
    with schema.connect_agent(store) as con:
        visible = {name for (name,) in con.execute("SHOW TABLES").fetchall()}
    assert {path.stem for path in bundle.glob("*.jsonl")} == visible


def test_the_readme_is_the_schema_text(store: Path, bundle: Path) -> None:
    with schema.connect_agent(store) as con:
        text = describe(con)
    assert (bundle / "README.md").read_text().endswith(text)
    assert [line[3:] for line in text.splitlines() if line.startswith("## ")] == relations()


def test_the_bundle_has_no_blob_contents(tmp_path: Path) -> None:
    store = make_store(tmp_path, [make_call_row(1)], [change(1, "/app/x.bin")])
    (schema.blobs_dir(store) / ("0" * 64)).write_bytes(b"secret bytes")
    out = tmp_path / "bundle"
    export(store, out)
    assert not any(
        b"secret bytes" in path.read_bytes() for path in out.rglob("*") if path.is_file()
    )


def test_export_refuses_an_existing_dir(store: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ExportError, match="exists"):
        export(store, out)


def test_a_failed_export_leaves_nothing(tmp_path: Path) -> None:
    store = make_store(tmp_path, [make_call_row(1)], [change(1, "/app/../etc/passwd", hunk="+x\n")])
    out = tmp_path / "bundle"
    with pytest.raises(ExportError, match="not a normalized absolute path"):
        export(store, out)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["store"]


def test_a_path_that_is_both_a_file_and_a_dir_of_hunk_files_is_refused(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [make_call_row(1)],
        [change(1, "/a/b", hunk="+x\n"), change(1, "/a/b.diff/c", hunk="+y\n")],
    )
    with pytest.raises(ExportError, match="would be a file and a directory here"):
        export(store, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_paths_differing_in_case_are_refused_only_where_case_folds(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [make_call_row(1)],
        [change(1, "/app/Makefile", hunk="+x\n"), change(1, "/app/makefile", hunk="+y\n")],
    )
    (tmp_path / "probe").touch()
    folds = (tmp_path / "PROBE").exists()
    if folds:
        with pytest.raises(ExportError, match="would be one hunk file here"):
            export(store, tmp_path / "bundle")
    else:
        export(store, tmp_path / "bundle")
        assert verify(store, tmp_path / "bundle") == []


def test_scripts_hunks_land_under_their_paths(tmp_path: Path) -> None:
    store = make_store(tmp_path, [make_call_row(1)], [script(1, "/w/a b/plan.py", ["x = 1"])])
    out = tmp_path / "bundle"
    export(store, out)
    assert hunk_file(out, TRIAL_ID, 1, "/w/a b/plan.py").read_text().endswith("+x = 1\n")


# -- what verify catches -----------------------------------------------------------------------


def test_verify_catches_an_edited_row(store: Path, bundle: Path) -> None:
    path = bundle / "steps.jsonl"
    path.write_text(path.read_text().replace('"source":"agent"', '"source":"user"', 1))
    assert verify(store, bundle) == ["steps.jsonl: 1 rows of steps missing, 1 rows not in it"]


def test_verify_catches_a_dropped_row(store: Path, bundle: Path) -> None:
    path = bundle / "steps.jsonl"
    path.write_text("".join(path.read_text().splitlines(keepends=True)[1:]))
    assert verify(store, bundle) == ["steps.jsonl: 1 rows of steps missing, 0 rows not in it"]


def test_verify_catches_an_extra_key(store: Path, bundle: Path) -> None:
    path = bundle / "trials.jsonl"
    (row,) = lines(path)
    path.write_text(json.dumps({**row, "passed_checks": []}) + "\n")
    (problem,) = verify(store, bundle)
    assert problem.startswith("trials.jsonl: rows whose keys are not")


def test_verify_catches_missing_and_extra_files(store: Path, bundle: Path) -> None:
    (bundle / "step_flags.jsonl").unlink()
    (bundle / "checks.jsonl").write_text("")
    assert verify(store, bundle) == [
        "files that are not the record's: ['checks.jsonl']",
        "missing: ['step_flags.jsonl']",
    ]


def test_verify_catches_an_edited_readme(store: Path, bundle: Path) -> None:
    path = bundle / "README.md"
    path.write_text(path.read_text() + "Hint: look at step 1.\n")
    assert verify(store, bundle) == ["README.md is not the record's schema text"]


def test_verify_catches_edited_missing_and_extra_hunk_files(store: Path, bundle: Path) -> None:
    hunk = hunk_file(bundle, TRIAL_ID, 1, "/app/hello.txt")
    hunk.write_text("+something else\n")
    assert verify(store, bundle) == [
        f"hunk files that differ from their row: ['{hunk.relative_to(bundle)}']"
    ]
    hunk.unlink()
    extra = hunk.with_name("notes.txt.diff")
    extra.write_text("+x\n")
    assert verify(store, bundle) == [
        f"hunk files with no delta row: ['{extra.relative_to(bundle)}'] (1 in all)",
        f"missing hunk files: ['{hunk.relative_to(bundle)}'] (1 in all)",
    ]


def test_verify_of_no_bundle(store: Path, tmp_path: Path) -> None:
    assert verify(store, tmp_path / "nothing") == [f"{tmp_path / 'nothing'} is not a directory"]


def test_describe_refuses_a_relation_it_cannot_describe(store: Path) -> None:
    with schema.connect(schema.record_path(store), read_only=False) as con:
        con.execute("CREATE TABLE notes (x INTEGER)")
        with pytest.raises(DescribeError, match="notes"):
            readme(con)


# -- the commands ------------------------------------------------------------------------------


def test_store_export_writes_and_verifies(store: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    result = CliRunner().invoke(app, ["store", "export", str(store), str(out)])
    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"wrote {out}: trials 1, steps 3, deltas 1, ")
    assert result.output.rstrip().endswith("1 hunk files; verified")

    again = CliRunner().invoke(app, ["store", "export", str(store), str(out), "--verify"])
    assert again.exit_code == 0, again.output
    assert again.output == f"{out}: matches {store}\n"


def test_store_export_verify_reports_each_problem(store: Path, bundle: Path) -> None:
    (bundle / "README.md").write_text("")
    result = CliRunner().invoke(app, ["store", "export", str(store), str(bundle), "--verify"])
    assert result.exit_code == 1
    assert "README.md is not the record's schema text" in result.output


def test_store_export_refuses_an_existing_dir(store: Path, bundle: Path) -> None:
    result = CliRunner().invoke(app, ["store", "export", str(store), str(bundle)])
    assert result.exit_code == 1
    assert "exists; export writes a new directory" in result.output


def test_store_derive(store: Path) -> None:
    result = CliRunner().invoke(app, ["store", "derive", str(store)])
    assert result.exit_code == 0, result.output
    assert result.output.endswith("already current\n")


def test_store_derive_refuses_a_store_of_another_schema_version(tmp_path: Path) -> None:
    store = make_store(tmp_path, [make_call_row(1)])
    as_schema(store, 3)
    result = CliRunner().invoke(app, ["store", "derive", str(store)])
    assert result.exit_code == 1
    assert "has schema 3; this code reads 4" in result.output
    assert "build it again" in result.output


def test_store_derive_reports_a_missing_store(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["store", "derive", str(tmp_path / "nothing")])
    assert result.exit_code == 1
    assert "does not exist" in result.output
