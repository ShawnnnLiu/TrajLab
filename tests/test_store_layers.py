import gzip
import hashlib
import io
import random
import tarfile
from dataclasses import dataclass
from typing import BinaryIO

import pytest

from trajlab.store.layers import (
    Entry,
    Layer,
    LayerError,
    ancestors,
    for_each_file,
    read_layer,
    scan_names,
)

# -- building layer tars the way Docker 29 with the containerd snapshotter writes them ---------
# Member names have no leading "/", directories may end in "/", whiteouts are empty regular files
# with mode 0, and a hard link is an LNKTYPE member naming an earlier member.

Member = tuple[tarfile.TarInfo, bytes | None]


def _info(name: str, kind: bytes, mode: int, uid: int, gid: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.uid, info.gid = uid, gid
    info.mtime = mtime
    return info


def d(name: str, mode: int = 0o755, uid: int = 0, gid: int = 0, mtime: int = 1) -> Member:
    return _info(name, tarfile.DIRTYPE, mode, uid, gid, mtime), None


def f(
    name: str,
    data: bytes | str,
    mode: int = 0o644,
    uid: int = 0,
    gid: int = 0,
    mtime: int = 1,
) -> Member:
    content = data.encode() if isinstance(data, str) else data
    info = _info(name, tarfile.REGTYPE, mode, uid, gid, mtime)
    info.size = len(content)
    return info, content


def wh(name: str) -> Member:
    """A whiteout or opaque marker: an empty regular file with mode 0."""
    return f(name, b"", mode=0)


def sym(name: str, target: str, mode: int = 0o777, uid: int = 0, gid: int = 0) -> Member:
    info = _info(name, tarfile.SYMTYPE, mode, uid, gid, 1)
    info.linkname = target
    return info, None


def hard(name: str, target: str, mode: int = 0o644) -> Member:
    info = _info(name, tarfile.LNKTYPE, mode, 0, 0, 1)
    info.linkname = target
    return info, None


def fifo(name: str, mode: int = 0o644) -> Member:
    return _info(name, tarfile.FIFOTYPE, mode, 0, 0, 1), None


def tar_bytes(*members: Member, gz: bool = False) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for info, content in members:
            tar.addfile(info, io.BytesIO(content) if content is not None else None)
    data = buffer.getvalue()
    return gzip.compress(data, mtime=0) if gz else data


@dataclass(frozen=True)
class BytesLayer:
    """A `LayerSource` over bytes in memory; counts how often it is opened."""

    data: bytes
    opened: list[int]

    @classmethod
    def of(cls, *members: Member, gz: bool = False) -> "BytesLayer":
        return cls(tar_bytes(*members, gz=gz), [])

    def open(self) -> BinaryIO:
        self.opened.append(1)
        return io.BytesIO(self.data)


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _read(*members: Member, gz: bool = False, **kwargs) -> Layer:
    return read_layer(io.BytesIO(tar_bytes(*members, gz=gz)), **kwargs)


# -- tests --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("etc/motd", "/etc/motd"),
        ("./etc/motd", "/etc/motd"),
        ("etc/", "/etc"),
        ("./etc/apk/", "/etc/apk"),
        ("./.bashrc", "/.bashrc"),  # lstrip("./") would give "bashrc"
        (".bashrc", "/.bashrc"),
        ("..foo", "/..foo"),
        ("/abs/path", "/abs/path"),
        ("a//b/./c", "/a/b/c"),
        (".", None),
        ("./", None),
        ("", None),
    ],
)
def test_member_names_become_absolute_paths(name: str, path: str | None) -> None:
    from trajlab.store.layers import normalize

    assert normalize(name) == path


def test_a_member_outside_the_root_is_an_error() -> None:
    from trajlab.store.layers import normalize

    with pytest.raises(LayerError):
        normalize("etc/../../x")


def test_ancestors_run_to_the_root() -> None:
    assert list(ancestors("/a/b/c")) == ["/a/b", "/a", "/"]
    assert list(ancestors("/a")) == ["/"]
    assert list(ancestors("/")) == []


@pytest.mark.parametrize("gz", [False, True])
def test_entries_of_every_kind(gz: bool) -> None:
    layer = _read(
        d("./", mode=0o755),
        d("etc/", mode=0o755),
        f("etc/motd", "hi\n", mode=0o100644, uid=1000, gid=1001),
        sym("etc/link", "/etc/motd"),
        fifo("etc/pipe", mode=0o600),
        d("root", mode=0o700),
        gz=gz,
    )
    assert set(layer.entries) == {"/etc", "/etc/motd", "/etc/link", "/etc/pipe", "/root"}
    assert layer.entries["/etc/motd"] == Entry("file", 0o644, 1000, 1001, 3, sha("hi\n"))
    assert layer.entries["/etc/link"] == Entry(
        "symlink", 0o777, 0, 0, len("/etc/motd"), sha("/etc/motd"), "/etc/motd"
    )
    assert layer.entries["/etc/pipe"] == Entry("other", 0o600, 0, 0)
    assert layer.entries["/root"] == Entry("dir", 0o700, 0, 0)
    assert not layer.whiteouts and not layer.opaque


def test_permission_bits_keep_setuid_and_sticky() -> None:
    layer = _read(f("bin/x", b"\x7fELF", mode=0o104755), d("tmp", mode=0o41777))
    assert layer.entries["/bin/x"].mode == 0o4755
    assert layer.entries["/tmp"].mode == 0o1777


def test_whiteouts_and_opaque_markers_are_not_entries() -> None:
    layer = _read(
        d("etc"),
        wh("etc/.wh.issue"),
        d("etc/apk"),
        wh("etc/apk/.wh.protected_paths.d"),
        d("usr/share/apk"),
        wh("usr/share/apk/.wh..wh..opq"),
        wh(".wh.toplevel"),
        wh("./.wh..wh..opq"),
    )
    assert set(layer.entries) == {"/etc", "/etc/apk", "/usr/share/apk"}
    assert layer.whiteouts == {"/etc/issue", "/etc/apk/protected_paths.d", "/toplevel"}
    assert layer.opaque == {"/usr/share/apk", "/"}


def test_unknown_aufs_markers_are_ignored_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    layer = _read(d("a"), wh("a/.wh..wh.plnk"), wh("a/.wh..wh.aufs"), f("a/x", "1"))
    assert set(layer.entries) == {"/a", "/a/x"}
    assert not layer.whiteouts and not layer.opaque
    assert "a/.wh..wh.plnk" in caplog.text


def test_a_hard_link_has_its_targets_entry() -> None:
    layer = _read(
        d("root", mode=0o700),
        f("root/hard", "hello\n", mode=0o640, uid=7, gid=8),
        sym("root/link", "/root/new.txt"),
        hard("root/new.txt", "root/hard"),
        hard("./root/third", "./root/new.txt"),
    )
    assert layer.entries["/root/new.txt"] == layer.entries["/root/hard"]
    assert layer.entries["/root/third"] == layer.entries["/root/hard"]
    assert layer.entries["/root/hard"].mode == 0o640


def test_a_hard_link_keeps_the_content_its_target_had_when_linked() -> None:
    layer = _read(f("a", "one"), hard("b", "a"), f("a", "two"))
    assert layer.entries["/b"].sha256 == sha("one")
    assert layer.entries["/a"].sha256 == sha("two")


def test_a_hard_link_to_no_earlier_member_is_an_error() -> None:
    with pytest.raises(LayerError, match="no earlier member"):
        _read(hard("b", "a"), f("a", "x"))


def test_a_hard_link_to_an_unkept_member_is_unresolved() -> None:
    members = (f("lib/big", "content"), hard("etc/alias", "lib/big"))
    first = _read(*members, keep=lambda p: p.startswith("/etc"))
    assert "/etc/alias" not in first.entries
    assert first.unresolved_links == {"/lib/big"}
    again = _read(*members, keep=lambda p: p.startswith("/etc") or p == "/lib/big")
    assert again.entries["/etc/alias"].sha256 == sha("content")
    assert not again.unresolved_links


def test_a_duplicated_member_takes_the_last() -> None:
    layer = _read(f("a", "first"), d("b"), f("a", "second", mode=0o600), f("b", "now a file"))
    assert layer.entries["/a"] == Entry("file", 0o600, 0, 0, 6, sha("second"))
    assert layer.entries["/b"].kind == "file"


def test_keep_filters_entries_but_never_markers() -> None:
    hooked: list[str] = []

    def hook(path: str, info: tarfile.TarInfo, data: BinaryIO) -> str:
        hooked.append(path)
        return sha(data.read())

    layer = _read(
        f("keep/a", "1"),
        f("drop/b", "2"),
        wh("drop/.wh.c"),
        wh("drop/d/.wh..wh..opq"),
        keep=lambda p: p.startswith("/keep"),
        on_file=hook,
    )
    assert set(layer.entries) == {"/keep/a"}
    assert hooked == ["/keep/a"]
    assert layer.whiteouts == {"/drop/c"}
    assert layer.opaque == {"/drop/d"}


def test_the_hook_sees_each_file_once_and_its_digest_is_the_entrys() -> None:
    seen: list[tuple[str, int]] = []

    def hook(path: str, info: tarfile.TarInfo, data: BinaryIO) -> str:
        seen.append((path, info.size))
        return "0" * 64

    layer = _read(f("a", "abc"), hard("b", "a"), sym("c", "a"), on_file=hook)
    assert seen == [("/a", 3)]  # not for the hard link or the symlink
    assert layer.entries["/a"].sha256 == "0" * 64
    assert layer.entries["/b"].sha256 == "0" * 64


def test_scan_names_lists_entries_and_markers_without_content() -> None:
    names = scan_names(
        io.BytesIO(
            tar_bytes(d("./"), d("a/"), f("a/x", "1"), hard("a/y", "a/x"), wh("a/.wh.z"), gz=True)
        )
    )
    assert names.paths == {"/a", "/a/x", "/a/y"}
    assert names.whiteouts == {"/a/z"}
    assert names.opaque == frozenset()


def test_for_each_file_skips_markers_links_and_directories() -> None:
    seen: list[tuple[str, bytes]] = []
    for_each_file(
        io.BytesIO(tar_bytes(d("a"), f("a/x", "1"), wh("a/.wh.z"), hard("a/y", "a/x"))),
        lambda path, info, data: seen.append((path, data.read())),
    )
    assert seen == [("/a/x", b"1")]


@pytest.mark.parametrize(
    "data",
    [b"", b"not a tar at all, just some text" * 40, None],
    ids=["empty", "garbage", "truncated"],
)
def test_an_unreadable_layer_is_a_layer_error(data: bytes | None) -> None:
    if data is None:
        whole = tar_bytes(f("a", b"x" * 10_000), gz=True)
        data = whole[: len(whole) // 2]
    with pytest.raises(LayerError):
        read_layer(io.BytesIO(data))


def test_a_truncated_member_read_by_the_hook_is_a_layer_error() -> None:
    whole = tar_bytes(f("a", b"x" * 100_000))
    with pytest.raises(LayerError):
        read_layer(io.BytesIO(whole[:50_000]), on_file=lambda p, i, data: sha(data.read()))


def test_hidden_follows_whiteouts_and_opaque_directories() -> None:
    layer = Layer(
        entries={"/w": Entry("dir", 0o755, 0, 0), "/o/kept": Entry("dir", 0o755, 0, 0)},
        whiteouts=frozenset({"/w", "/x"}),
        opaque=frozenset({"/o"}),
    )
    assert not layer.hidden("/w")  # an entry of the same layer is never hidden
    assert layer.hidden("/w/child")  # though its whiteout still hides what was under it
    assert layer.hidden("/x") and layer.hidden("/x/y/z")
    assert not layer.hidden("/o")  # opaque hides strictly below
    assert layer.hidden("/o/lower")
    assert not layer.hidden("/o/kept")
    assert not layer.hidden("/xy") and not layer.hidden("/elsewhere")


def test_hidden_in_agrees_with_hidden() -> None:
    rng = random.Random(7)
    alphabet = ["a", "b", "a b", "a.b", "ab"]
    for _ in range(300):
        paths = sorted(
            {"/" + "/".join(rng.choices(alphabet, k=rng.randint(1, 4))) for _ in range(40)}
        )
        layer = Layer(
            entries={p: Entry("dir", 0o755, 0, 0) for p in rng.sample(paths, 5)},
            whiteouts=frozenset(rng.sample(paths, 3)),
            opaque=frozenset(rng.sample(paths, 2) + (["/"] if rng.random() < 0.1 else [])),
        )
        assert layer.hidden_in(paths) == {p for p in paths if layer.hidden(p)}
