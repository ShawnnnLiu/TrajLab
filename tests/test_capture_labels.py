import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trajlab.capture.labels import (
    EXPORT_FIELDS,
    PATTERN_HELP,
    LabelError,
    LabelStore,
    export_rows,
    progress,
    rows_csv,
)
from trajlab.contracts import LABEL_PATTERNS, Label, TrialLabels

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def labels(
    trial_name: str = "t1", labeler: str = "amina", job: str = "job-a", **fields: Any
) -> TrialLabels:
    return TrialLabels(
        job_name=job, trial_name=trial_name, labeler=labeler, updated_at=NOW, **fields
    )


def label(patterns: tuple[str, ...] | None, **fields: Any) -> Label:
    return Label(patterns=patterns, updated_at=NOW, **fields)


def context(job: str, trial: str) -> dict[str, Any]:
    steps = {
        "c1": {"call": 1, "tool": "Bash", "summary": "pytest -q"},
        "c2": {"call": 2, "tool": "Edit", "summary": "=HYPERLINK(1)"},
        "stop_1": {"call": None, "tool": "Stop", "summary": "end of the agent's turn"},
    }
    return {"task": "task-x", "reward": 0.0, "steps": steps}


def test_every_pattern_has_a_title_and_definition() -> None:
    assert set(PATTERN_HELP) == set(LABEL_PATTERNS)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels")
    saved = labels(steps={"c1": label(("loop",), note="ran it 4 times")}, done=True)

    path = store.save(saved)

    assert path == tmp_path / "labels" / "job-a" / "amina" / "t1.json"
    assert store.load("job-a", "amina", "t1") == saved
    assert store.load("job-a", "amina", "t2") is None
    assert [p.name for p in path.parent.iterdir()] == ["t1.json"]  # no temp file left


def test_iter_labels_filters_by_job_labeler_and_trial(tmp_path: Path) -> None:
    store = LabelStore(tmp_path)
    for job, labeler, trial in (
        ("job-a", "amina", "t1"),
        ("job-a", "amina", "t2"),
        ("job-a", "petra", "t1"),
        ("job-b", "amina", "t1"),
    ):
        store.save(labels(trial, labeler, job))

    def names(**filters: str) -> list[tuple[str, str, str]]:
        return [(x.job_name, x.labeler, x.trial_name) for x in store.iter_labels(**filters)]

    assert len(names()) == 4
    assert names(job="job-a", labeler="amina") == [
        ("job-a", "amina", "t1"),
        ("job-a", "amina", "t2"),
    ]
    assert names(job="job-a", trial="t1") == [("job-a", "amina", "t1"), ("job-a", "petra", "t1")]
    assert store.labelers("job-a") == ["amina", "petra"]
    assert store.labelers("job-c") == []


@pytest.mark.parametrize(
    ("job", "labeler", "trial"),
    [
        ("..", "amina", "t1"),
        ("job-a", "../amina", "t1"),
        ("job-a", "amina", "x/../../t1"),
        ("job-a", "", "t1"),
    ],
)
def test_names_cannot_leave_the_labels_dir(
    tmp_path: Path, job: str, labeler: str, trial: str
) -> None:
    with pytest.raises(LabelError):
        LabelStore(tmp_path).path(job, labeler, trial)


def test_a_corrupt_label_file_is_reported(tmp_path: Path) -> None:
    store = LabelStore(tmp_path)
    path = store.path("job-a", "amina", "t1")
    path.parent.mkdir(parents=True)
    path.write_text("{")

    with pytest.raises(LabelError, match="not a TrialLabels file"):
        store.load("job-a", "amina", "t1")


def test_progress_counts_judged_steps_only() -> None:
    saved = labels(
        trial=label(("unverified_completion",)),
        steps={
            "c1": label(("loop", "stall")),
            "c2": label(()),
            "c3": label(None, note="look again"),
        },
    )

    assert progress(saved) == {
        "trial_judged": True,
        "steps_judged": 2,
        "done": False,
        "found": ["loop", "stall", "unverified_completion"],
    }
    assert progress(None)["steps_judged"] == 0


def test_export_rows_put_the_trial_first_then_steps_in_trial_order() -> None:
    saved = labels(
        trial=label(("loop",)),
        steps={
            "stop_1": label((), unsure=True),
            "c2": label(("no_effect_write",), note="Edit changed nothing"),
            "c1": label(None, note="a note, no judgment"),
            "c9": label(None),  # an empty label: not exported
        },
    )

    rows = export_rows([saved], context)

    assert [(r["level"], r["tool_call_id"], r["call"]) for r in rows] == [
        ("trial", "", None),
        ("step", "c1", 1),
        ("step", "c2", 2),
        ("step", "stop_1", None),
    ]
    note_only, edit, stop = rows[1:]
    assert note_only["judged"] is False and note_only["loop"] is None
    assert edit["no_effect_write"] is True and edit["loop"] is False
    assert edit["patterns"] == ["no_effect_write"]
    assert stop["patterns"] == [] and stop["unsure"] is True
    assert rows[0]["task"] == "task-x" and rows[0]["labeler"] == "amina"


def test_rows_csv_has_a_column_per_pattern_and_defuses_formulas() -> None:
    saved = labels(steps={"c2": label(("no_effect_write", "loop"), note="-> same file")})

    parsed = list(csv.DictReader(io.StringIO(rows_csv(export_rows([saved], context)))))

    assert tuple(parsed[0]) == EXPORT_FIELDS
    row = parsed[0]
    assert (row["loop"], row["no_effect_write"], row["stall"]) == ("1", "1", "0")
    assert row["patterns"] == "loop;no_effect_write"
    assert row["summary"] == "'=HYPERLINK(1)"
    assert row["note"] == "'-> same file"
    assert row["reward"] == "0.0"


def test_a_file_saved_with_the_retired_hints_seen_field_still_loads(tmp_path: Path) -> None:
    store = LabelStore(tmp_path)
    path = store.path("job-a", "amina", "t1")
    path.parent.mkdir(parents=True)
    old = labels(steps={"c1": label(("loop",))})
    path.write_text(json.dumps(old.model_dump(mode="json") | {"hints_seen": True}))

    assert store.load("job-a", "amina", "t1") == old
    store.save(old)
    assert "hints_seen" not in path.read_text()
