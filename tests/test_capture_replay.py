import hashlib
import subprocess
from typing import Any

import pytest

from trajlab.capture.replay import SnapshotReader, added_lines, apply_diff, file_after, rebuild
from trajlab.checkpoint.diffs import unified_diff


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def diff(before: str | None, after: str, path: str = "/app/a.py") -> str:
    # The watcher's own diff, so the replay is tested against what it records.
    result = unified_diff(path, before.encode() if before is not None else None, after.encode())
    assert result is not None
    return result


VERSIONS = ["a = 1\nb = 2\n", "a = 1\nb = 3\nc = 4\n", "b = 3\nc = 4\n"]


def history(texts: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """A file created by the first call and edited by each later one, as the trace lists it."""
    versions, diffs, before = [], {}, None
    for i, text in enumerate(texts):
        call = f"c{i + 1}"
        versions.append(
            {"id": call, "event": i * 2, "op": "added" if before is None else "modified",
             "kind": "file", "sha256": sha(text), "before_sha256": sha(before) if before else None}
        )  # fmt: skip
        diffs[call] = diff(before, text)
        before = text
    return versions, diffs


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("", "x\n"),
        ("x\ny\nz\n", "x\nY\nz\n"),
        ("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n", "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\n"),
        ("1\n2\n3\n4\n5\n6\n7\n8\n9\n", "0\n1\n2\n3\n4\n6\n7\n8\n9\n"),
        ("keep\ndrop\n", "keep\n"),
    ],
)
def test_apply_diff_undoes_the_watchers_diff(before: str, after: str) -> None:
    assert apply_diff(before.splitlines(), diff(before, after)) == after.splitlines()


def test_apply_diff_refuses_a_diff_that_does_not_fit() -> None:
    assert apply_diff(["other"], diff("x\ny\n", "x\nY\n")) is None


def test_added_lines_are_new_file_line_numbers() -> None:
    assert added_lines(diff("a\nb\nc\n", "a\nB\nc\nd\n")) == [2, 4]


def test_rebuild_follows_a_file_from_creation_and_checks_hashes() -> None:
    versions, diffs = history(VERSIONS)

    assert rebuild(versions, diffs) == (True, VERSIONS[-1], True)
    assert rebuild(versions[:2], diffs) == (True, VERSIONS[1], True)


def test_rebuild_cannot_start_from_a_file_that_existed_before() -> None:
    versions, diffs = history(VERSIONS)

    assert rebuild(versions[1:], diffs) == (True, None, False)


def test_a_skipped_diff_breaks_the_chain_until_a_file_is_recreated() -> None:
    versions, diffs = history(VERSIONS)
    del diffs["c2"]

    assert rebuild(versions, diffs) == (True, None, False)
    removed = {"id": "c4", "event": 6, "op": "removed", "kind": "other", "sha256": None}
    recreated = {"id": "c5", "event": 8, "op": "added", "kind": "file", "sha256": sha("new\n")}
    diffs["c5"] = diff(None, "new\n")
    assert rebuild([*versions, removed], diffs) == (False, None, True)
    assert rebuild([*versions, removed, recreated], diffs) == (True, "new\n", True)


def test_file_after_reports_the_state_at_each_event() -> None:
    versions, diffs = history(VERSIONS)

    early = file_after(versions, diffs, 1)
    assert (early["text"], early["last_change"], early["next_change"]) == (VERSIONS[0], "c1", "c2")
    late = file_after(versions, diffs, 4)
    assert late["text"] == VERSIONS[2] and late["verified"]
    assert file_after(versions, diffs, -1)["exists"] is None


class FakeDocker:
    def __init__(self, returncode: int = 0, stdout: bytes = b"content", stderr: bytes = b""):
        self.calls: list[list[str]] = []
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(command)
        return self.result


def test_the_snapshot_reader_runs_cat_offline_and_caches() -> None:
    docker = FakeDocker()
    reader = SnapshotReader(runner=docker)

    assert reader.read("sha256:abc", "/app/a.py") == (b"content", "")
    assert reader.read("sha256:abc", "/app/a.py") == (b"content", "")
    assert len(docker.calls) == 1
    command = docker.calls[0]
    assert command[:2] == ["docker", "run"] and command[-4:] == [
        "cat",
        "sha256:abc",
        "--",
        "/app/a.py",
    ]
    assert {"--pull", "never", "--network", "none", "--rm"} <= set(command)


def test_the_snapshot_reader_says_why_docker_failed() -> None:
    # What Docker 29 prints for an image that is not there (checked 2026-10-20).
    help_line = b"\n\nRun 'docker run --help' for more information\n"
    missing = b"docker: Error response from daemon: No such image: sha256:abc" + help_line
    data, problem = SnapshotReader(runner=FakeDocker(125, b"", missing)).read("sha256:abc", "/x")
    assert data is None and problem.startswith("its image is not on this machine")
    other = b"docker: Error response from daemon: something else broke" + help_line
    assert SnapshotReader(runner=FakeDocker(125, b"", other)).read("i", "/x") == (
        None,
        "something else broke",
    )
