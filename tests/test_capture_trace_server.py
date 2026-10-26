import csv
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    FIXTURE_JOB_NAME,
    FIXTURE_TOOL_CALL_ID,
    FIXTURE_TRIAL_NAME,
    make_call,
    make_checkpoint,
    write_capture,
)
from tests.test_capture_replay import FakeDocker
from trajlab.capture.labels import LabelStore
from trajlab.capture.replay import SnapshotReader
from trajlab.capture.trace_server import Response, TraceSite
from trajlab.checkpoint.diffs import sha256, unified_diff
from trajlab.contracts import (
    CHANGES_FILENAME,
    LABEL_PATTERNS,
    TRAJLAB_DIRNAME,
    CallChanges,
    FileChange,
    TrialLabels,
)

LOCAL = {"host": "localhost:8765"}
SAVE = LOCAL | {"x-trajlab": "1"}
TRIAL_URL = f"{FIXTURE_JOB_NAME}/{FIXTURE_TRIAL_NAME}"


@pytest.fixture
def site(job_dir: Path, tmp_path: Path) -> TraceSite:
    return TraceSite(job_dir.parent, LabelStore(tmp_path / "labels"))


def get(site: TraceSite, target: str, headers: dict[str, str] = LOCAL) -> Response:
    return site.handle("GET", target, headers)


def body(response: Response) -> Any:
    return json.loads(response.body)


def labels_doc(**fields: Any) -> dict[str, Any]:
    doc = {
        "job_name": FIXTURE_JOB_NAME,
        "trial_name": FIXTURE_TRIAL_NAME,
        "labeler": "amina",
        "trial": None,
        "steps": {
            FIXTURE_TOOL_CALL_ID: {
                "patterns": ["no_effect_write"],
                "note": "wrote the same bytes",
                "updated_at": "2026-10-19T12:00:00Z",
            }
        },
        "updated_at": "2026-10-19T12:00:00Z",
    }
    return doc | fields


def put_labels(
    site: TraceSite, doc: dict[str, Any], labeler: str = "amina", headers: dict[str, str] = SAVE
) -> Response:
    target = f"/api/labels/{TRIAL_URL}?labeler={labeler}"
    return site.handle("PUT", target, headers, json.dumps(doc).encode())


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1:9000", "[::1]:8765", "localhost"])
def test_local_hosts_are_served(site: TraceSite, host: str) -> None:
    assert get(site, "/api/meta", {"host": host}).status == 200


@pytest.mark.parametrize("host", ["evil.example:8765", "", "localhost.evil.example"])
def test_other_hosts_are_refused(site: TraceSite, host: str) -> None:
    # A DNS-rebinding page reaches 127.0.0.1 under its own host name.
    assert get(site, "/api/meta", {"host": host}).status == 403


def test_the_page_and_its_assets(site: TraceSite) -> None:
    page = get(site, "/")
    assert page.status == 200 and page.content_type.startswith("text/html")
    assert ("Content-Security-Policy", page.headers[0][1]) == page.headers[0]
    assert "script-src 'self'" in page.headers[0][1]
    assert get(site, "/static/app.js").content_type == "text/javascript"
    assert get(site, "/static/app.css").status == 200
    assert get(site, "/static/../trace.py").status == 404
    assert get(site, "/favicon.ico").status == 204


def test_meta_lists_the_patterns(site: TraceSite) -> None:
    patterns = body(get(site, "/api/meta"))["patterns"]
    assert [p["key"] for p in patterns] == list(LABEL_PATTERNS)
    assert all(p["title"] and p["help"] for p in patterns)


def test_experiments_trials_and_a_trace(site: TraceSite) -> None:
    jobs = body(get(site, "/api/jobs"))["jobs"]
    assert [(j["name"], j["trials"], j["finished"], j["passed"]) for j in jobs] == [
        (FIXTURE_JOB_NAME, 1, 1, 1)
    ]

    trials = body(get(site, f"/api/jobs/{FIXTURE_JOB_NAME}?labeler=amina"))["trials"]
    assert [t["name"] for t in trials] == [FIXTURE_TRIAL_NAME]
    assert trials[0]["progress"]["steps_judged"] == 0

    trace = body(get(site, f"/api/trace/{TRIAL_URL}"))
    assert [e["id"] for e in trace["events"] if e["kind"] == "call"] == [FIXTURE_TOOL_CALL_ID]


@pytest.mark.parametrize(
    "target",
    [
        f"/api/trace/{FIXTURE_JOB_NAME}/nope",
        "/api/trace/nope/x",
        "/api/trace/..%2F..%2Fetc/passwd",
        f"/api/trace/{FIXTURE_JOB_NAME}/..%2F{FIXTURE_JOB_NAME}",
        "/api/nothing",
    ],
)
def test_unknown_names_are_not_found(site: TraceSite, target: str) -> None:
    assert get(site, target).status == 404


def test_labels_save_and_load(site: TraceSite, tmp_path: Path) -> None:
    assert body(get(site, f"/api/labels/{TRIAL_URL}?labeler=amina")) is None

    saved = put_labels(site, labels_doc())

    assert saved.status == 200
    on_disk = TrialLabels.model_validate_json(
        (
            tmp_path / "labels" / FIXTURE_JOB_NAME / "amina" / f"{FIXTURE_TRIAL_NAME}.json"
        ).read_text()
    )
    assert on_disk.steps[FIXTURE_TOOL_CALL_ID].patterns == ("no_effect_write",)
    assert on_disk.updated_at != datetime(2026, 10, 19, 12, 0, tzinfo=UTC)  # the server's time
    assert body(get(site, f"/api/labels/{TRIAL_URL}?labeler=amina")) == body(saved)
    progress = body(get(site, f"/api/jobs/{FIXTURE_JOB_NAME}?labeler=amina"))["trials"][0][
        "progress"
    ]
    assert (progress["steps_judged"], progress["found"]) == (1, ["no_effect_write"])


def test_a_save_needs_the_header(site: TraceSite) -> None:
    # A page on another site cannot add a custom header without CORS approval.
    assert put_labels(site, labels_doc(), headers=LOCAL).status == 403


@pytest.mark.parametrize(
    ("doc", "labeler"),
    [
        (labels_doc(labeler="petra"), "amina"),
        (labels_doc(trial_name="other"), "amina"),
        (labels_doc(), "bad name"),
        (
            labels_doc(
                steps={"x": {"patterns": ["looping"], "updated_at": "2026-10-19T12:00:00Z"}}
            ),
            "amina",
        ),
        (labels_doc(extra_field=1), "amina"),
    ],
)
def test_bad_saves_are_refused(site: TraceSite, doc: dict[str, Any], labeler: str) -> None:
    assert put_labels(site, doc, labeler).status == 400


def test_labels_need_a_labeler(site: TraceSite) -> None:
    assert get(site, f"/api/labels/{TRIAL_URL}").status == 400


def test_export_csv_and_json(site: TraceSite) -> None:
    put_labels(site, labels_doc())

    response = get(site, f"/api/export?format=csv&job={FIXTURE_JOB_NAME}")

    assert response.content_type.startswith("text/csv")
    assert dict(response.headers)["Content-Disposition"].startswith(
        'attachment; filename="trajlab-labels-hello-world-smoke-'
    )
    rows = list(csv.DictReader(io.StringIO(response.body.decode())))
    assert [
        (r["trial"], r["tool_call_id"], r["call"], r["tool"], r["no_effect_write"]) for r in rows
    ] == [(FIXTURE_TRIAL_NAME, FIXTURE_TOOL_CALL_ID, "1", "Write", "1")]
    exported = body(get(site, f"/api/export?format=json&job={FIXTURE_JOB_NAME}&labeler=amina"))
    assert exported["patterns"] == list(LABEL_PATTERNS)
    assert [r["note"] for r in exported["rows"]] == ["wrote the same bytes"]
    assert exported["labels"][0]["labeler"] == "amina"
    assert get(site, f"/api/export?trial={FIXTURE_TRIAL_NAME}").status == 400


def test_other_methods_are_refused(site: TraceSite) -> None:
    assert site.handle("POST", "/api/meta", LOCAL).status == 405


def test_the_transcript_and_the_raw_trajectory(site: TraceSite, job_dir: Path) -> None:
    transcript = get(site, f"/api/transcript/{TRIAL_URL}")

    assert transcript.content_type == "text/plain; charset=utf-8"
    assert "inline" in dict(transcript.headers)["Content-Disposition"]
    text = transcript.body.decode()
    assert text.index("Create a file called hello.txt") < text.index(
        f"#1 Write · {FIXTURE_TOOL_CALL_ID}"
    )

    raw = get(site, f"/api/raw/{TRIAL_URL}")
    trajectory = job_dir / FIXTURE_TRIAL_NAME / "agent" / "trajectory.json"
    assert raw.content_type == "application/json; charset=utf-8"
    assert raw.body == trajectory.read_bytes()

    trajectory.unlink()
    assert get(site, f"/api/transcript/{TRIAL_URL}").status == 404
    assert get(site, f"/api/raw/{TRIAL_URL}").status == 404


def test_a_save_from_a_page_older_than_the_hints_seen_removal_is_kept(site: TraceSite) -> None:
    assert put_labels(site, labels_doc(hints_seen=True)).status == 200


def test_the_page_modules_are_served(site: TraceSite) -> None:
    assert get(site, "/static/app.js").content_type == "text/javascript"
    assert get(site, "/static/app.css").content_type == "text/css"
    for bad in ("/static/App.js", "/static/missing.js", "/static/app.py", "/static/a/b.js"):
        assert get(site, bad).status == 404, bad


def write_hello_diff(trial: Path) -> None:
    """The fixture's one call, recorded as creating /app/hello.txt, with its diff."""
    write_capture(trial, checkpoints=[make_checkpoint(1)], calls=[make_call(1, change="changed")])
    content = b"Hello, world!"
    file = FileChange(
        path="/app/hello.txt",
        op="added",
        kind="file",
        size=len(content),
        sha256=sha256(content),
        is_text=True,
        diff=unified_diff("/app/hello.txt", None, content),
    )
    change = CallChanges(
        tool_call_id=FIXTURE_TOOL_CALL_ID, call_seq=1, files=(file,), files_total=1, capture_ms=5
    )
    (trial / TRAJLAB_DIRNAME).mkdir()
    (trial / TRAJLAB_DIRNAME / CHANGES_FILENAME).write_text(change.model_dump_json() + "\n")


def test_a_file_the_agent_created_is_rebuilt_from_its_diff(site: TraceSite, job_dir: Path) -> None:
    write_hello_diff(job_dir / FIXTURE_TRIAL_NAME)

    view = body(get(site, f"/api/file/{TRIAL_URL}?path=/app/hello.txt&call={FIXTURE_TOOL_CALL_ID}"))

    assert (view["source"], view["text"], view["verified"]) == ("diffs", "Hello, world!", True)
    assert view["changed_lines"] == [1]


def test_the_trace_and_the_file_view_read_the_changes_once(
    site: TraceSite, job_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_hello_diff(job_dir / FIXTURE_TRIAL_NAME)
    reads = []
    read_text = Path.read_text

    def counting(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == CHANGES_FILENAME:
            reads.append(self)
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)

    assert get(site, f"/api/trace/{TRIAL_URL}").status == 200
    view = body(get(site, f"/api/file/{TRIAL_URL}?path=/app/hello.txt&call={FIXTURE_TOOL_CALL_ID}"))

    assert view["source"] == "diffs"
    assert len(reads) == 1


def test_otherwise_the_file_is_read_from_the_calls_checkpoint(
    job_dir: Path, tmp_path: Path
) -> None:
    trial = job_dir / FIXTURE_TRIAL_NAME
    calls = [make_call(1, change="changed", changed_paths=("~/app/hello.txt",))]
    write_capture(trial, checkpoints=[make_checkpoint(1)], calls=calls)
    docker = FakeDocker(stdout=b"from the snapshot\n")
    site = TraceSite(
        job_dir.parent, LabelStore(tmp_path / "labels"), snapshots=SnapshotReader(docker)
    )

    target = f"/api/file/{TRIAL_URL}?path=/app/hello.txt&call={FIXTURE_TOOL_CALL_ID}"
    view = body(get(site, target))

    assert (view["source"], view["checkpoint"], view["text"]) == (
        "snapshot",
        1,
        "from the snapshot\n",
    )
    assert docker.calls[0][-3:] == [make_checkpoint(1).checkpoint_id, "--", "/app/hello.txt"]
    assert (
        get(site, f"/api/file/{TRIAL_URL}?path=/etc/passwd&call={FIXTURE_TOOL_CALL_ID}").status
        == 404
    )
    assert get(site, f"/api/file/{TRIAL_URL}?path=/app/hello.txt&call=nope").status == 404


def test_the_queue_agreement_and_guide(site: TraceSite, tmp_path: Path) -> None:
    queue = body(get(site, f"/api/queue/{FIXTURE_JOB_NAME}?labeler=amina"))
    assert [s["key"] for s in queue["sections"]] == ["all"]
    assert queue["sections"][0]["trials"][0]["name"] == FIXTURE_TRIAL_NAME

    assert body(get(site, f"/api/agreement/{FIXTURE_JOB_NAME}"))["result"] is None
    put_labels(site, labels_doc())
    put_labels(site, labels_doc(labeler="petra"), labeler="petra")
    shared = body(get(site, f"/api/agreement/{FIXTURE_JOB_NAME}"))
    assert (shared["a"], shared["b"], shared["result"]["items"]["step"]) == ("amina", "petra", 1)

    assert body(get(site, "/api/guide"))["text"] is None
    guide = tmp_path / "guide.md"
    guide.write_text("## Loop\nRepeating without progress.\n")
    with_guide = TraceSite(site.root, site.labels, guide=guide)
    assert body(get(with_guide, "/api/guide"))["sections"] == {
        "loop": "Repeating without progress."
    }


def test_the_tutorial_is_served_and_its_examples_exist(site: TraceSite) -> None:
    tutorial = get(site, "/static/tutorial.md")
    assert tutorial.content_type == "text/markdown; charset=utf-8"
    names = set(re.findall(r"^```sample\n([\w-]+)\n```$", tutorial.body.decode(), re.MULTILINE))
    script = get(site, "/static/view-tutorial.js").body.decode()
    known = set(re.findall(r'case "([\w-]+)":', script))
    assert names and names <= known
