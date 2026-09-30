import subprocess
from typing import Any

import pytest

from trajlab.checkpoint.backends.base import BackendError, Snapshot
from trajlab.checkpoint.backends.docker_commit import DockerCommitBackend, image_tag

IMAGE_ID = "sha256:77cb8e9cf76cb13de9b0b3fc55378c4efebe9d28de28a0d3712c219c4bfa537f"


class FakeDocker:
    """Records docker invocations and answers them from a table keyed by the subcommand."""

    def __init__(self, answers: dict[str, str | Exception]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] and kwargs["capture_output"] and kwargs["timeout"]
        self.calls.append(command)
        key = command[1] if command[1] != "container" else "inspect"
        answer = self.answers[key]
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(command, 0, stdout=answer + "\n", stderr="")


def test_container_for_filters_by_compose_labels() -> None:
    docker = FakeDocker({"ps": "c0ffee"})
    assert DockerCommitBackend(runner=docker).container_for("hello-world__k3gbok3__env") == "c0ffee"
    assert docker.calls == [
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=hello-world__k3gbok3__env",
            "--filter",
            "label=com.docker.compose.service=main",
            "--format",
            "{{.ID}}",
        ]
    ]


@pytest.mark.parametrize("stdout", ["", "a\nb"])
def test_container_for_needs_exactly_one(stdout: str) -> None:
    with pytest.raises(BackendError, match="expected one running main container"):
        DockerCommitBackend(runner=FakeDocker({"ps": stdout})).container_for("p")


def test_snapshot_commits_with_labels_and_measures_layer() -> None:
    docker = FakeDocker({"commit": IMAGE_ID, "inspect": "20975616"})
    snapshot = DockerCommitBackend(runner=docker).snapshot(
        "c0ffee",
        tag="hello-world__K3GBok3.0001",
        labels={"trajlab.trial_name": "hello-world__K3GBok3", "trajlab.seq": "1"},
    )

    assert snapshot.checkpoint_id == IMAGE_ID
    assert snapshot.bytes == 20_975_616
    assert snapshot.capture_ms >= 0
    assert snapshot.captured_at.tzinfo is not None
    assert snapshot.path is None
    assert docker.calls[0] == [
        "docker",
        "commit",
        '--change=LABEL trajlab.trial_name="hello-world__K3GBok3"',
        '--change=LABEL trajlab.seq="1"',
        "c0ffee",
        "trajlab-checkpoint:hello-world__K3GBok3.0001",
    ]
    assert docker.calls[1][1:] == [
        "container",
        "inspect",
        "--size",
        "--format",
        "{{.SizeRw}}",
        "c0ffee",
    ]


def test_snapshot_without_size_still_succeeds() -> None:
    failure = subprocess.CalledProcessError(1, "docker", stderr="boom")
    docker = FakeDocker({"commit": IMAGE_ID, "inspect": failure})
    assert DockerCommitBackend(runner=docker).snapshot("c", tag="t", labels={}).bytes is None


@pytest.mark.parametrize(
    "answer",
    [
        subprocess.CalledProcessError(1, "docker", stderr="No such container: c"),
        subprocess.TimeoutExpired("docker", 230),
        FileNotFoundError("docker"),
    ],
    ids=["exit", "timeout", "missing"],
)
def test_snapshot_failures_are_backend_errors(answer: Exception) -> None:
    with pytest.raises(BackendError):
        DockerCommitBackend(runner=FakeDocker({"commit": answer})).snapshot("c", tag="t", labels={})


def test_snapshot_rejects_unexpected_output() -> None:
    with pytest.raises(BackendError, match="not an image id"):
        DockerCommitBackend(runner=FakeDocker({"commit": "oops"})).snapshot("c", tag="t", labels={})


def test_discard_removes_image() -> None:
    docker = FakeDocker({"image": ""})
    snapshot = Snapshot(checkpoint_id=IMAGE_ID, capture_ms=1, captured_at=None)  # type: ignore[arg-type]
    DockerCommitBackend(runner=docker).discard(snapshot)
    assert docker.calls == [["docker", "image", "rm", "--force", IMAGE_ID]]


def test_restore_and_fork_are_out_of_scope() -> None:
    backend = DockerCommitBackend(runner=FakeDocker({}))
    snapshot = Snapshot(checkpoint_id=IMAGE_ID, capture_ms=1, captured_at=None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        backend.restore(snapshot)
    with pytest.raises(NotImplementedError):
        backend.fork(snapshot)


@pytest.mark.parametrize(
    ("trial_name", "seq", "expected"),
    [
        ("hello-world__K3GBok3", 1, "hello-world__K3GBok3.0001"),
        ("org/task__Ab1", 12, "org_task__Ab1.0012"),
        ("-lead", 3, "_-lead.0003"),
        ("x" * 200, 1, "x" * 123 + ".0001"),
    ],
)
def test_image_tag_is_a_valid_docker_tag(trial_name: str, seq: int, expected: str) -> None:
    tag = image_tag(trial_name, seq)
    assert tag == expected
    assert len(tag) <= 128
