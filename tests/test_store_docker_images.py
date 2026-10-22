import gzip
import hashlib
import io
import json
import logging
import os
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FIXTURE_TRIAL_NAME, make_checkpoint
from trajlab.checkpoint.backends.docker_commit import REPOSITORY, image_tag
from trajlab.contracts import CheckpointRecord
from trajlab.store.docker_images import (
    CHECKPOINT_REPOSITORY,
    DockerImageProvider,
    DockerImagesError,
    EmptyLayer,
    SavedLayer,
)
from trajlab.store.reconcile import ImageChain, LayerSource


@dataclass(frozen=True)
class Layer:
    """A layer as Docker holds it: its files, its tar, and the blob a save writes."""

    files: tuple[tuple[str, bytes], ...]
    tar: bytes
    blob: bytes

    @property
    def diff_id(self) -> str:
        return "sha256:" + hashlib.sha256(self.tar).hexdigest()

    @property
    def member(self) -> str:
        return "blobs/sha256/" + hashlib.sha256(self.blob).hexdigest()


def make_layer(files: dict[str, bytes], *, compress: bool = True) -> Layer:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    raw = buffer.getvalue()
    blob = gzip.compress(raw, mtime=0) if compress else raw
    return Layer(tuple(files.items()), raw, blob)


@dataclass
class Image:
    id: str
    tags: list[str]
    layers: list[Layer]


@dataclass
class FakeDocker:
    """Answers `docker image inspect` from `images` and writes `docker image save` output in
    the OCI layout Docker 29 writes: blobs first, then index.json, manifest.json, oci-layout."""

    images: list[Image]
    errors: dict[str, str] = field(default_factory=dict)  # subcommand -> stderr of a failure
    vanish: set[str] = field(default_factory=set)  # refs gone by the .RootFS.Layers inspect
    edit_manifest: Callable[[list[dict[str, Any]]], None] | None = None
    calls: list[list[str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Every checkpoint here is a commit over BASE_IMAGE, the start image providers are given.
        if all(image.id != BASE_IMAGE.id for image in self.images):
            self.images.append(BASE_IMAGE)

    def resolve(self, ref: str) -> Image | None:
        return next((i for i in self.images if ref == i.id or ref in i.tags), None)

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.keys() == {"check", "capture_output", "text", "timeout"}
        assert kwargs["check"] is False and kwargs["capture_output"] and kwargs["text"]
        assert command[0] == "docker"
        self.calls.append(command)
        if command[2] in self.errors:
            return subprocess.CompletedProcess(command, 1, "", self.errors[command[2]])
        match command[1:]:
            case ["image", "inspect", "--format", template, ref]:
                image = self.resolve(ref)
                if image is None or (template == "{{json .RootFS.Layers}}" and ref in self.vanish):
                    stderr = f"Error response from daemon: No such image: {ref}\n"
                    return subprocess.CompletedProcess(command, 1, "[]\n", stderr)
                if template == "{{json .RepoTags}}":
                    value: Any = image.tags
                else:
                    assert template == "{{json .RootFS.Layers}}"
                    value = [layer.diff_id for layer in image.layers]
                return subprocess.CompletedProcess(command, 0, json.dumps(value) + "\n", "")
            case ["image", "save", "-o", path, *refs]:
                self._save(Path(path), refs)
                return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command {command}")

    @property
    def saves(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] == ["image", "save"]]

    def _save(self, path: Path, refs: Sequence[str]) -> None:
        blobs: dict[str, bytes] = {}
        manifest = []
        for ref in refs:
            image = self.resolve(ref)
            assert image is not None, ref
            config = json.dumps({"rootfs": [layer.diff_id for layer in image.layers]}).encode()
            config_member = "blobs/sha256/" + hashlib.sha256(config).hexdigest()
            blobs[config_member] = config
            for layer in image.layers:
                blobs[layer.member] = layer.blob
            layers = [layer.member for layer in image.layers]
            manifest.append({"Config": config_member, "RepoTags": [ref], "Layers": layers})
        if self.edit_manifest is not None:
            self.edit_manifest(manifest)
        with tarfile.open(path, mode="w") as tar:
            for directory in ("blobs", "blobs/sha256"):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            members = [*blobs.items(), ("index.json", b"{}")]
            members += [("manifest.json", json.dumps(manifest).encode()), ("oci-layout", b"{}")]
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))


BASE = [
    make_layer({"etc/os-release": b"NAME=test\n", "bin/tool": bytes(range(256)) * 64}),
    make_layer({"usr/lib/libx.so": b"\x7fELF" + bytes(5000)}),
]
TOPS = {
    seq: make_layer(
        {f"root/file{seq}.txt": f"seq {seq}\n".encode(), "root/big": b"x" * seq * 70000}
    )
    for seq in (1, 2, 3)
}
BASE_IMAGE = Image("sha256:" + "b" * 64, ["trajlab-preinstalled:hello"], list(BASE))
START = BASE_IMAGE.tags[0]


def tag(seq: int) -> str:
    return f"{REPOSITORY}:{image_tag(FIXTURE_TRIAL_NAME, seq)}"


def checkpoint_image(seq: int, layers: list[Layer] | None = None, tags: list[str] | None = None):
    return Image(
        make_checkpoint(seq).checkpoint_id,
        [tag(seq)] if tags is None else tags,
        [*BASE, TOPS[seq]] if layers is None else layers,
    )


def records(*seqs: int) -> list[CheckpointRecord]:
    return [make_checkpoint(seq, tool_call_id=f"toolu_{seq:02d}") for seq in seqs]


def read(source: LayerSource) -> bytes:
    with source.open() as stream:
        return stream.read()


def layer_files(source: LayerSource) -> dict[str, bytes]:
    """The files of a layer, read the way reconcile reads one: a stream, gzip or not."""
    with source.open() as stream, tarfile.open(fileobj=stream, mode="r|*") as tar:
        files = {}
        for member in tar:
            extracted = tar.extractfile(member)
            assert extracted is not None
            files[member.name] = extracted.read()
        return files


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    return tmp_path / "work"


def provide(docker: FakeDocker, work_dir: Path, *seqs: int, start_image: str = START) -> ImageChain:
    with DockerImageProvider(work_dir, runner=docker, start_image=start_image) as provider:
        chain = provider.chain(records(*seqs))
        # Read everything before the provider deletes its save file.
        for source in [*chain.start_layers, *chain.tops.values()]:
            assert read(source) is not None
        return chain


def test_repository_is_the_backends() -> None:
    assert CHECKPOINT_REPOSITORY == REPOSITORY


def test_chain_yields_exactly_the_saved_layers(work_dir: Path) -> None:
    docker = FakeDocker([BASE_IMAGE, *(checkpoint_image(seq) for seq in (1, 2, 3))])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        chain = provider.chain(records(1, 2, 3))

        assert [read(source) for source in chain.start_layers] == [layer.blob for layer in BASE]
        assert sorted(chain.tops) == [1, 2, 3]
        for seq, layer in TOPS.items():
            assert read(chain.tops[seq]) == layer.blob
            assert layer_files(chain.tops[seq]) == dict(layer.files)
        assert [layer_files(source) for source in chain.start_layers] == [
            dict(layer.files) for layer in BASE
        ]


def test_layer_source_opens_twice_and_independently(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1), checkpoint_image(2)])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        source = provider.chain(records(1, 2)).tops[2]
        expected = TOPS[2].blob
        with source.open() as first, source.open() as second:
            head = first.read(100)
            assert second.read() == expected
            assert head + first.read() == expected
        assert read(source) == expected
        assert layer_files(source) == layer_files(source) == dict(TOPS[2].files)


def test_layer_source_is_seekable(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1)])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        source = provider.chain(records(1)).tops[1]
        with source.open() as stream:
            assert stream.seekable()
            assert stream.read() == TOPS[1].blob
            assert stream.seek(-10, io.SEEK_END) == len(TOPS[1].blob) - 10
            assert stream.read() == TOPS[1].blob[-10:]
            stream.seek(0)
            # Random access, as tarfile's "r:gz" mode needs.
            with tarfile.open(fileobj=stream, mode="r:gz") as tar:
                member = tar.extractfile("root/big")
                assert member is not None and member.read() == b"x" * 70000


def test_closing_a_stream_closes_the_save_file(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1)])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        source = provider.chain(records(1)).tops[1]
        before = len(os.listdir("/dev/fd"))
        streams = [source.open() for _ in range(10)]
        assert len(os.listdir("/dev/fd")) >= before + 10
        for stream in streams:
            stream.read(1)
            stream.close()
        assert len(os.listdir("/dev/fd")) == before


def test_relative_work_dir_survives_a_change_of_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    docker = FakeDocker([checkpoint_image(1)])
    with DockerImageProvider(Path("work"), runner=docker, start_image=START) as provider:
        source = provider.chain(records(1)).tops[1]
        monkeypatch.chdir(tmp_path.parent)
        assert read(source) == TOPS[1].blob
    assert list((tmp_path / "work").iterdir()) == []


def test_picks_the_checkpoint_tag_among_several(work_dir: Path) -> None:
    tags = ["zzz:latest", tag(1), "aaa:1", "trajlab-preinstalled:other"]
    docker = FakeDocker([checkpoint_image(1, tags=tags)])
    chain = provide(docker, work_dir, 1)

    assert list(chain.tops) == [1]
    assert docker.saves[0][5:] == [tag(1)]
    # The image is found by its id, then referenced by the tag.
    assert docker.calls[0][1:] == [
        "image", "inspect", "--format", "{{json .RepoTags}}", make_checkpoint(1).checkpoint_id,
    ]  # fmt: skip
    assert docker.calls[1][1:] == [
        "image",
        "inspect",
        "--format",
        "{{json .RootFS.Layers}}",
        tag(1),
    ]


def test_missing_or_untagged_image_has_no_top(
    work_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    docker = FakeDocker([checkpoint_image(1), checkpoint_image(3, tags=["other:3"])])
    chain = provide(docker, work_dir, 1, 2, 3)

    assert list(chain.tops) == [1]
    assert docker.saves[0][5:] == [tag(1)]
    assert "checkpoint 2 of hello-world__K3GBok3" in caplog.text
    assert "image not found" in caplog.text
    assert f"checkpoint 3 of hello-world__K3GBok3 ({make_checkpoint(3).checkpoint_id})" in (
        caplog.text
    )
    assert "has no trajlab-checkpoint: tag" in caplog.text


def test_image_gone_between_inspects_has_no_top(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1), checkpoint_image(2)], vanish={tag(2)})
    assert list(provide(docker, work_dir, 1, 2).tops) == [1]


def test_save_runs_once_with_every_found_tag_into_work_dir(
    work_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    docker = FakeDocker([checkpoint_image(seq) for seq in (1, 2, 3)])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        provider.chain(list(reversed(records(1, 2, 3))))

    assert len(docker.saves) == 1
    save = docker.saves[0]
    assert save[1:4] == ["image", "save", "-o"]
    assert Path(save[4]).parent.parent == work_dir  # the provider's own save directory
    assert Path(save[4]).parent.name.startswith("trajlab-images-")
    assert save[5:] == [tag(1), tag(2), tag(3)]
    assert "saved 3 checkpoint images in" in caplog.text and " MB in " in caplog.text


def test_closing_removes_every_save_file(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1), checkpoint_image(2)])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        first = provider.chain(records(1)).tops[1]
        second = provider.chain(records(1, 2)).tops[2]
        assert isinstance(first, SavedLayer) and isinstance(second, SavedLayer)
        assert first.archive != second.archive
        assert first.archive.exists() and second.archive.exists()

    assert list(work_dir.iterdir()) == []
    with pytest.raises(FileNotFoundError):
        first.open()


def test_no_records_runs_no_docker(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1)])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        assert provider.chain([]) == ImageChain(start_layers=(), tops={})
    assert docker.calls == []


def test_no_image_found_runs_no_save(work_dir: Path) -> None:
    docker = FakeDocker([BASE_IMAGE])
    chain = provide(docker, work_dir, 1, 2, start_image=BASE_IMAGE.id)

    assert chain == ImageChain(start_layers=(), tops={})
    assert docker.saves == []
    assert not work_dir.exists() or list(work_dir.iterdir()) == []


def test_start_image_with_matching_layers(work_dir: Path) -> None:
    docker = FakeDocker([BASE_IMAGE, checkpoint_image(1), checkpoint_image(2)])
    chain = provide(docker, work_dir, 1, 2, start_image="trajlab-preinstalled:hello")

    assert len(chain.start_layers) == 2 and sorted(chain.tops) == [1, 2]
    assert ["image", "inspect", "--format", "{{json .RootFS.Layers}}", BASE_IMAGE.tags[0]] in [
        call[1:] for call in docker.calls
    ]


def test_start_image_mismatch_raises(work_dir: Path) -> None:
    shorter = Image("sha256:" + "c" * 64, ["trajlab-preinstalled:short"], [BASE[0]])
    docker = FakeDocker([shorter, checkpoint_image(1)])
    with pytest.raises(DockerImagesError, match="not a commit over the start image"):
        provide(docker, work_dir, 1, start_image=shorter.id)
    assert docker.saves == []


def test_missing_start_image_raises(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1)])
    with pytest.raises(DockerImagesError, match="start image trajlab-preinstalled:gone not found"):
        provide(docker, work_dir, 1, start_image="trajlab-preinstalled:gone")


def test_different_start_layers_raise(work_dir: Path) -> None:
    other_base = [BASE[0], make_layer({"usr/lib/liby.so": b"other"})]
    docker = FakeDocker([checkpoint_image(1), checkpoint_image(2, [*other_base, TOPS[2]])])
    with pytest.raises(DockerImagesError, match="not a commit over the start image"):
        provide(docker, work_dir, 1, 2)
    assert docker.saves == []


def test_image_with_two_layers_over_the_start_image_raises(work_dir: Path) -> None:
    extra = make_layer({"opt/extra": b"extra"})
    docker = FakeDocker(
        [BASE_IMAGE, checkpoint_image(1), checkpoint_image(2, [*BASE, extra, TOPS[2]])]
    )
    with pytest.raises(DockerImagesError, match="a commit adds at most one layer"):
        provide(docker, work_dir, 1, 2)


def test_commit_without_a_layer_has_an_empty_top_given_the_base(work_dir: Path) -> None:
    docker = FakeDocker([BASE_IMAGE, checkpoint_image(1, list(BASE)), checkpoint_image(2)])
    with DockerImageProvider(work_dir, runner=docker, start_image=BASE_IMAGE.id) as provider:
        chain = provider.chain(records(1, 2))
        assert isinstance(chain.tops[1], EmptyLayer)
        assert layer_files(chain.tops[1]) == {}
        assert layer_files(chain.tops[2]) == dict(TOPS[2].files)
        assert [read(source) for source in chain.start_layers] == [layer.blob for layer in BASE]
    assert docker.saves[0][5:] == [tag(1), tag(2)]


def test_closing_removes_what_a_killed_save_left(work_dir: Path) -> None:
    # The docker CLI saves to `.tmp-<name><random>` beside the target and renames it at the
    # end; killed (the save timeout, a signal), it leaves that file behind.
    docker = FakeDocker([checkpoint_image(1)])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        archive = provider.chain(records(1)).tops[1]
        assert isinstance(archive, SavedLayer)
        (archive.archive.parent / f".tmp-{archive.archive.name}123").write_bytes(b"partial")
    assert list(work_dir.iterdir()) == []


def test_base_stored_as_different_blobs_raises(work_dir: Path) -> None:
    # Same diff ids (uncompressed content), different blobs: the base is not one set of members.
    recompressed = [make_layer(dict(layer.files), compress=False) for layer in BASE]
    assert [layer.diff_id for layer in recompressed] == [layer.diff_id for layer in BASE]
    docker = FakeDocker([checkpoint_image(1), checkpoint_image(2, [*recompressed, TOPS[2]])])
    with pytest.raises(DockerImagesError, match="different blobs"):
        provide(docker, work_dir, 1, 2)
    assert list(work_dir.iterdir()) == []


def test_manifest_disagreeing_with_inspect_raises(work_dir: Path) -> None:
    def drop_a_base_layer(manifest: list[dict[str, Any]]) -> None:
        manifest[1]["Layers"] = manifest[1]["Layers"][1:]

    docker = FakeDocker([checkpoint_image(1), checkpoint_image(2)], edit_manifest=drop_a_base_layer)
    with pytest.raises(DockerImagesError, match="has 2 layers in manifest.json but 3"):
        provide(docker, work_dir, 1, 2)
    assert list(work_dir.iterdir()) == []


def test_manifest_without_a_tag_raises(work_dir: Path) -> None:
    def untag(manifest: list[dict[str, Any]]) -> None:
        manifest[0]["RepoTags"] = None

    docker = FakeDocker([checkpoint_image(1)], edit_manifest=untag)
    with pytest.raises(DockerImagesError, match="does not list"):
        provide(docker, work_dir, 1)


def test_uncompressed_layers_are_returned_as_saved(work_dir: Path) -> None:
    raw_base = [make_layer(dict(layer.files), compress=False) for layer in BASE]
    raw_top = make_layer(dict(TOPS[1].files), compress=False)
    docker = FakeDocker([checkpoint_image(1, [*raw_base, raw_top])])
    with DockerImageProvider(work_dir, runner=docker, start_image=START) as provider:
        chain = provider.chain(records(1))
        assert read(chain.tops[1]) == raw_top.tar
        assert layer_files(chain.tops[1]) == dict(TOPS[1].files)


def test_docker_failure_raises_with_command_and_stderr(work_dir: Path) -> None:
    docker = FakeDocker(
        [checkpoint_image(1)],
        errors={"inspect": "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"},
    )
    with pytest.raises(DockerImagesError) as raised:
        provide(docker, work_dir, 1)
    message = str(raised.value)
    assert "docker image inspect --format '{{json .RepoTags}}' sha256:" in message
    assert "exited 1: Cannot connect to the Docker daemon" in message


def test_failed_save_leaves_no_file(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1)], errors={"save": "write /x: no space left on device"})
    with pytest.raises(DockerImagesError, match="docker image save -o .* no space left"):
        provide(docker, work_dir, 1)
    assert list(work_dir.iterdir()) == []


def test_missing_docker_binary_raises(work_dir: Path) -> None:
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", command[0])

    with pytest.raises(DockerImagesError, match="no-such-docker image inspect"):
        DockerImageProvider(
            work_dir, docker="no-such-docker", runner=runner, start_image=START
        ).chain(records(1))


def test_repeated_seq_raises(work_dir: Path) -> None:
    docker = FakeDocker([checkpoint_image(1)])
    with pytest.raises(DockerImagesError, match="repeat a seq"):
        DockerImageProvider(work_dir, runner=docker, start_image=START).chain(
            [*records(1), *records(1)]
        )
    assert docker.calls == []
