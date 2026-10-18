import difflib
import hashlib
import random
import re
import tarfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from uuid import UUID

import pytest

import trajlab.store.reconcile as reconcile_module
from tests.conftest import make_checkpoint
from tests.test_store_layers import (
    BytesLayer,
    Member,
    d,
    f,
    fifo,
    hard,
    sha,
    sym,
    tar_bytes,
    wh,
)
from trajlab.contracts import DeltaRow
from trajlab.store.exclusions import excluded
from trajlab.store.layers import ancestors
from trajlab.store.reconcile import (
    MAX_TEXT_BYTES,
    ImageChain,
    ReconcileError,
    ReconcileResult,
    _edit_script,
    _unified_diff,
    reconcile,
)

TRIAL = UUID("00000000-0000-4000-8000-000000000016")


def call_id(seq: int) -> str:
    return f"toolu_{seq:02d}"


def step(seq: int) -> int:
    """The step_idx the tests' owner_step gives checkpoint `seq`."""
    return 10 * seq


def run(
    tmp_path: Path,
    base: Sequence[BytesLayer],
    tops: Mapping[int, BytesLayer],
    n: int | None = None,
    **kwargs,
) -> ReconcileResult:
    n = max(tops) if n is None else n
    records = [make_checkpoint(seq, tool_call_id=call_id(seq)) for seq in range(1, n + 1)]
    owner_step = {call_id(seq): step(seq) for seq in range(1, n + 1)}
    return reconcile(
        ImageChain(base_layers=tuple(base), tops=dict(tops)),
        records,
        owner_step,
        trial_id=TRIAL,
        blobs_dir=tmp_path / "blobs",
        work_dir=tmp_path / "work",
        **kwargs,
    )


def rows_of(result: ReconcileResult, seq: int = 1) -> dict[str, DeltaRow]:
    return {row.path: row for row in result.deltas if row.step_idx == step(seq)}


def ops_of(result: ReconcileResult, seq: int = 1) -> dict[str, str]:
    return {path: row.op for path, row in rows_of(result, seq).items()}


MOTD = "Welcome to Alpine!\n\nThe Alpine Wiki contains a large amount of how-to guides.\n"
OS_RELEASE = 'NAME="Alpine Linux"\nID=alpine\nVERSION_ID=3.20.3\n'
BUSYBOX = b"\x7fELF\x02\x01\x01\x00busybox\x00\x00"


def alpine(gz: bool = True) -> BytesLayer:
    """A small base layer shaped like the Alpine image the 2026-10-18 probe used."""
    return BytesLayer.of(
        d("bin"),
        f("bin/busybox", BUSYBOX, mode=0o755),
        d("etc"),
        d("etc/apk"),
        d("etc/apk/protected_paths.d"),
        f("etc/apk/protected_paths.d/alpine.list", "/etc/apk\n"),
        f("etc/issue", "Welcome to Alpine Linux 3.20\n"),
        f("etc/motd", MOTD),
        f("etc/os-release", OS_RELEASE),
        f("etc/shells", "# valid login shells\n/bin/sh\n/bin/ash\n"),
        d("root", mode=0o700),
        d("usr"),
        d("usr/share"),
        d("usr/share/apk"),
        f("usr/share/apk/README", "apk data\n"),
        d("usr/share/apk/keys"),
        f("usr/share/apk/keys/a.rsa.pub", "KEY A\n"),
        f("usr/share/apk/keys/b.rsa.pub", "KEY B\n"),
        gz=gz,
    )


def top(*members: Member, gz: bool = True) -> BytesLayer:
    return BytesLayer.of(*members, gz=gz)


# -- the layers observed on Docker 29 (containerd snapshotter), 2026-10-18 ----------------------


def _observed_layer_1() -> list[Member]:
    return [
        d("etc", mtime=50),
        d("etc/apk", mtime=50),
        wh("etc/apk/.wh.protected_paths.d"),
        wh("etc/.wh.issue"),
        f("etc/motd", MOTD + "appended\n", mtime=50),
        d("root", mode=0o700, mtime=50),
        f("root/bin.dat", b"\x00\x01\x02\x03\xff"),
        d("root/emptydir"),
        f("root/hard", "hello\n"),
        sym("root/link", "/root/new.txt"),
        hard("root/new.txt", "root/hard"),
        d("usr", mtime=1),
        d("usr/share", mtime=50),
        d("usr/share/apk", mtime=50),
        wh("usr/share/apk/.wh.README"),
        wh("usr/share/apk/.wh.keys"),
        f("usr/share/apk/x", "apk x\n"),
    ]


def _observed_layer_2() -> list[Member]:
    layer = [
        m
        for m in _observed_layer_1()
        if m[0].name not in ("root/bin.dat", "root/emptydir", "root/hard")
    ]
    hard_link = next(i for i, m in enumerate(layer) if m[0].name == "root/new.txt")
    layer.insert(hard_link, f("root/hard", "rewritten\n"))
    return [*layer, f("etc/os-release", OS_RELEASE, mtime=99), wh("etc/.wh.shells")]


def test_the_observed_docker_layers(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {1: top(*_observed_layer_1()), 2: top(*_observed_layer_2())})
    assert result.computed_seqs == (1, 2) and result.skipped_seqs == ()
    assert ops_of(result, 1) == {
        "/etc/apk/protected_paths.d": "delete",  # rm -rf: one whiteout, subtree not listed
        "/etc/apk/protected_paths.d/alpine.list": "delete",
        "/etc/issue": "delete",
        "/etc/motd": "modify",
        "/root/bin.dat": "add",
        "/root/emptydir": "add",
        "/root/hard": "add",
        "/root/link": "add",
        "/root/new.txt": "add",
        # rm -rf + mkdir of usr/share/apk: an explicit whiteout per removed child
        "/usr/share/apk/README": "delete",
        "/usr/share/apk/keys": "delete",
        "/usr/share/apk/keys/a.rsa.pub": "delete",
        "/usr/share/apk/keys/b.rsa.pub": "delete",
        "/usr/share/apk/x": "add",
    }
    assert ops_of(result, 2) == {
        "/etc/shells": "delete",
        "/root/bin.dat": "delete",  # gone without a whiteout: it was never in the base
        "/root/emptydir": "delete",
        "/root/hard": "modify",
        "/root/new.txt": "modify",
        # not /etc/os-release: copied up with the base's bytes
    }
    one, two = rows_of(result, 1), rows_of(result, 2)
    assert one["/root/new.txt"].sha256_after == one["/root/hard"].sha256_after == sha("hello\n")
    assert two["/root/new.txt"].sha256_before == sha("hello\n")
    assert two["/root/new.txt"].sha256_after == sha("rewritten\n")
    assert two["/root/bin.dat"].is_text is False
    assert two["/root/bin.dat"].hunk is None and two["/root/bin.dat"].blob_ref is None
    assert one["/etc/motd"].hunk == (
        "--- a/etc/motd\n+++ b/etc/motd\n@@ -1,3 +1,4 @@\n"
        " Welcome to Alpine!\n \n The Alpine Wiki contains a large amount of how-to guides.\n"
        "+appended\n"
    )


# -- one change at a time -----------------------------------------------------------------------


def test_an_added_text_file(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {1: top(d("root", mode=0o700), f("root/new.txt", "hi\n"))})
    assert list(rows_of(result)) == ["/root/new.txt"]  # not /root: a parent, unchanged
    row = rows_of(result)["/root/new.txt"]
    assert row == DeltaRow(
        trial_id=TRIAL,
        step_idx=10,
        path="/root/new.txt",
        op="add",
        kind_after="file",
        is_text=True,
        hunk="--- /dev/null\n+++ b/root/new.txt\n@@ -0,0 +1 @@\n+hi\n",
        sha256_after=sha("hi\n"),
        bytes_after=3,
        mode_after="0644",
        owner_after="0:0",
    )


def test_a_modified_base_file_diffs_against_the_base(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {1: top(d("etc"), f("etc/shells", "/bin/sh\n"))})
    row = rows_of(result)["/etc/shells"]
    assert (row.op, row.kind_before, row.kind_after) == ("modify", "file", "file")
    assert row.sha256_before == sha("# valid login shells\n/bin/sh\n/bin/ash\n")
    assert row.bytes_before == 38 and row.bytes_after == 8
    assert row.hunk == (
        "--- a/etc/shells\n+++ b/etc/shells\n@@ -1,3 +1 @@\n"
        "-# valid login shells\n /bin/sh\n-/bin/ash\n"
    )


def test_a_copy_up_with_the_same_bytes_is_no_change(tmp_path: Path) -> None:
    layer = top(d("etc", mtime=12345), f("etc/os-release", OS_RELEASE, mtime=999), d("root", 0o700))
    result = run(tmp_path, [alpine()], {1: layer})
    assert result.deltas == () and result.computed_seqs == (1,)


def test_chmod_only_is_a_modify_without_a_hunk(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {1: top(d("etc"), f("etc/motd", MOTD, mode=0o600))})
    row = rows_of(result)["/etc/motd"]
    assert (row.op, row.is_text, row.hunk) == ("modify", True, None)
    assert (row.mode_before, row.mode_after) == ("0644", "0600")
    assert row.sha256_before == row.sha256_after == sha(MOTD)


def test_owner_only_is_a_modify(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {1: top(d("etc", uid=1000, gid=1000))})
    row = rows_of(result)["/etc"]
    assert (row.op, row.kind_before, row.is_text, row.hunk) == ("modify", "dir", None, None)
    assert (row.owner_before, row.owner_after) == ("0:0", "1000:1000")
    assert row.sha256_before is None and row.bytes_before is None


def test_a_whiteout_deletes_a_base_file_with_a_hunk(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {1: top(d("etc"), wh("etc/.wh.issue"))})
    row = rows_of(result)["/etc/issue"]
    assert (row.op, row.kind_before, row.kind_after, row.is_text) == ("delete", "file", None, True)
    assert (
        row.hunk == "--- a/etc/issue\n+++ /dev/null\n@@ -1 +0,0 @@\n-Welcome to Alpine Linux 3.20\n"
    )
    assert row.mode_after is None and row.sha256_after is None


def test_one_whiteout_deletes_a_whole_base_directory(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {1: top(d("usr"), d("usr/share"), wh("usr/share/.wh.apk"))})
    assert ops_of(result) == {
        "/usr/share/apk": "delete",
        "/usr/share/apk/README": "delete",
        "/usr/share/apk/keys": "delete",
        "/usr/share/apk/keys/a.rsa.pub": "delete",
        "/usr/share/apk/keys/b.rsa.pub": "delete",
    }
    assert rows_of(result)["/usr/share/apk/keys"].is_text is None
    assert rows_of(result)["/usr/share/apk/keys/a.rsa.pub"].hunk.endswith("-KEY A\n")


def test_a_file_added_then_gone_without_a_whiteout(tmp_path: Path) -> None:
    tops = {
        1: top(d("root", 0o700), f("root/tmp.txt", "x\n"), d("root/emptydir")),
        2: top(d("root", 0o700)),
    }
    result = run(tmp_path, [alpine()], tops)
    assert ops_of(result, 1) == {"/root/tmp.txt": "add", "/root/emptydir": "add"}
    assert ops_of(result, 2) == {"/root/tmp.txt": "delete", "/root/emptydir": "delete"}
    gone = rows_of(result, 2)["/root/emptydir"]
    assert (gone.kind_before, gone.is_text, gone.hunk) == ("dir", None, None)
    assert rows_of(result, 2)["/root/tmp.txt"].hunk == (
        "--- a/root/tmp.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )


def test_a_second_change_diffs_against_the_first(tmp_path: Path) -> None:
    tops = {
        1: top(d("etc"), f("etc/motd", "one\n")),
        2: top(d("etc"), f("etc/motd", "one\ntwo\n")),
    }
    result = run(tmp_path, [alpine()], tops)
    row = rows_of(result, 2)["/etc/motd"]
    assert row.sha256_before == sha("one\n")
    assert row.hunk == "--- a/etc/motd\n+++ b/etc/motd\n@@ -1 +1,2 @@\n one\n+two\n"


def test_a_path_no_longer_hidden_shows_the_base_again(tmp_path: Path) -> None:
    # Not a layer containerd writes (a whiteout stays until an entry replaces it), but the view
    # rule covers it: view 2 is the base wherever layer 2 is silent.
    tops = {
        1: top(d("etc"), wh("etc/.wh.issue"), d("bin"), wh("bin/.wh.busybox")),
        2: top(d("etc")),
    }
    result = run(tmp_path, [alpine()], tops)
    assert ops_of(result, 2) == {"/etc/issue": "add", "/bin/busybox": "add"}
    rows = rows_of(result, 2)
    assert rows["/etc/issue"].hunk == (
        "--- /dev/null\n+++ b/etc/issue\n@@ -0,0 +1 @@\n+Welcome to Alpine Linux 3.20\n"
    )
    assert rows["/bin/busybox"].blob_ref == f"blobs/{sha(BUSYBOX)}"
    assert (tmp_path / "blobs" / sha(BUSYBOX)).read_bytes() == BUSYBOX  # fetched from the base


def test_an_opaque_directory_hides_the_base_below_it(tmp_path: Path) -> None:
    layer = top(
        d("usr"),
        d("usr/share"),
        d("usr/share/apk"),
        wh("usr/share/apk/.wh..wh..opq"),
        d("usr/share/apk/keys"),
        f("usr/share/apk/keys/a.rsa.pub", "KEY A\n"),
        f("usr/share/apk/new", "n\n"),
    )
    result = run(tmp_path, [alpine()], {1: layer})
    assert ops_of(result) == {
        "/usr/share/apk/README": "delete",
        "/usr/share/apk/keys/b.rsa.pub": "delete",
        "/usr/share/apk/new": "add",
        # keys and keys/a.rsa.pub are in the layer itself, equal to the base: no row
    }


def test_a_hard_link_is_a_file_with_its_targets_content(tmp_path: Path) -> None:
    layer = top(
        d("root", 0o700), f("root/hard", "hello\n", mode=0o600), hard("root/x", "root/hard")
    )
    rows = rows_of(run(tmp_path, [alpine()], {1: layer}))
    assert rows["/root/x"].sha256_after == sha("hello\n")
    assert rows["/root/x"].mode_after == "0600"
    assert rows["/root/x"].hunk == "--- /dev/null\n+++ b/root/x\n@@ -0,0 +1 @@\n+hello\n"


def test_a_binary_hard_link_to_an_unchanged_file_gets_its_blob(tmp_path: Path) -> None:
    # `ln /bin/busybox /bin/sh` copies busybox up unchanged; the new link needs its bytes,
    # which only the copied-up member carries.
    layer = top(d("bin"), f("bin/busybox", BUSYBOX, mode=0o755), hard("bin/sh", "bin/busybox"))
    result = run(tmp_path, [alpine()], {1: layer})
    assert ops_of(result) == {"/bin/sh": "add"}
    row = rows_of(result)["/bin/sh"]
    assert (row.is_text, row.hunk, row.blob_ref) == (False, None, f"blobs/{sha(BUSYBOX)}")
    assert (tmp_path / "blobs" / sha(BUSYBOX)).read_bytes() == BUSYBOX


def test_a_base_hard_link_to_an_uninteresting_member_is_resolved(tmp_path: Path) -> None:
    base = BytesLayer.of(
        d("usr"),
        d("usr/lib"),
        f("usr/lib/libz.so.1.3", b"\x7fELF libz\x00"),
        hard("usr/lib/libz.so", "usr/lib/libz.so.1.3"),
    )
    layer = top(d("usr"), d("usr/lib"), f("usr/lib/libz.so", b"\x7fELF patched\x00"))
    result = run(tmp_path, [base], {1: layer})
    row = rows_of(result)["/usr/lib/libz.so"]
    assert row.sha256_before == sha(b"\x7fELF libz\x00")
    assert len(base.opened) == 2  # read again to keep the link's target


def test_a_symlink_added_then_retargeted(tmp_path: Path) -> None:
    tops = {
        1: top(d("root", 0o700), sym("root/link", "/root/a")),
        2: top(d("root", 0o700), sym("root/link", "/root/bb")),
    }
    result = run(tmp_path, [alpine()], tops)
    added, moved = rows_of(result, 1)["/root/link"], rows_of(result, 2)["/root/link"]
    assert (added.op, added.kind_after, added.is_text) == ("add", "symlink", True)
    assert (added.sha256_after, added.bytes_after, added.mode_after) == (sha("/root/a"), 7, "0777")
    assert added.hunk == (
        "--- /dev/null\n+++ b/root/link\n@@ -0,0 +1 @@\n+/root/a\n\\ No newline at end of file\n"
    )
    assert moved.op == "modify" and moved.bytes_after == 8
    assert moved.hunk == (
        "--- a/root/link\n+++ b/root/link\n@@ -1 +1 @@\n"
        "-/root/a\n\\ No newline at end of file\n+/root/bb\n\\ No newline at end of file\n"
    )


def test_a_file_replaced_by_a_symlink_is_one_modify(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {1: top(d("etc"), sym("etc/motd", "/etc/issue"))})
    assert ops_of(result) == {"/etc/motd": "modify"}
    row = rows_of(result)["/etc/motd"]
    assert (row.kind_before, row.kind_after, row.is_text) == ("file", "symlink", True)
    assert (row.mode_before, row.mode_after) == ("0644", "0777")
    assert row.hunk is not None and row.hunk.endswith("+/etc/issue\n\\ No newline at end of file\n")


def test_a_text_file_replaced_by_a_directory_diffs_its_content_away(tmp_path: Path) -> None:
    # A version without content counts as text; the file's lines go to /dev/null.
    result = run(tmp_path, [alpine()], {1: top(d("etc"), wh("etc/.wh.issue"), d("etc/issue"))})
    row = rows_of(result)["/etc/issue"]
    assert (row.op, row.kind_before, row.kind_after, row.is_text) == ("modify", "file", "dir", True)
    assert row.hunk is not None and row.hunk.startswith("--- a/etc/issue\n+++ /dev/null\n")
    assert row.sha256_after is None and row.blob_ref is None


def test_a_binary_file_writes_its_later_version_as_a_blob(tmp_path: Path) -> None:
    new = b"\x7fELF\x02 new busybox \x00"
    tops = {1: top(d("bin"), f("bin/busybox", new, mode=0o755), d("root"), f("root/b.dat", b"\0"))}
    result = run(tmp_path, [alpine()], tops)
    rows = rows_of(result)
    assert rows["/bin/busybox"].blob_ref == f"blobs/{sha(new)}"
    assert rows["/bin/busybox"].sha256_before == sha(BUSYBOX)
    assert (rows["/bin/busybox"].is_text, rows["/bin/busybox"].hunk) == (False, None)
    assert rows["/root/b.dat"].blob_ref == f"blobs/{sha(b'\0')}"
    blobs = sorted(p.name for p in (tmp_path / "blobs").iterdir())
    assert blobs == sorted([sha(new), sha(b"\0")])  # earlier versions are never stored


def test_a_binary_chmod_stores_no_blob(tmp_path: Path) -> None:
    # Like a hunk, a blob is the content of a change; a mode-only change has none.
    result = run(tmp_path, [alpine()], {1: top(d("bin"), f("bin/busybox", BUSYBOX, mode=0o700))})
    row = rows_of(result)["/bin/busybox"]
    assert (row.op, row.is_text, row.hunk, row.blob_ref) == ("modify", False, None, None)
    assert (row.mode_before, row.mode_after) == ("0755", "0700")
    assert not (tmp_path / "blobs" / sha(BUSYBOX)).exists()


def test_text_over_the_size_limit_is_not_text(tmp_path: Path) -> None:
    at_limit = b"a" * MAX_TEXT_BYTES
    over = b"b" * (MAX_TEXT_BYTES + 1)
    layer = top(d("root", 0o700), f("root/limit.txt", at_limit), f("root/over.txt", over))
    rows = rows_of(run(tmp_path, [alpine()], {1: layer}))
    assert rows["/root/limit.txt"].is_text is True
    assert rows["/root/limit.txt"].hunk.endswith(
        "+" + "a" * MAX_TEXT_BYTES + "\n\\ No newline at end of file\n"
    )
    assert rows["/root/over.txt"].is_text is False and rows["/root/over.txt"].hunk is None
    assert rows["/root/over.txt"].blob_ref == f"blobs/{sha(over)}"
    assert (tmp_path / "blobs" / sha(over)).read_bytes() == over


def test_large_binaries_are_fetched_by_a_second_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reconcile_module, "MAX_TEXT_BYTES", 8)
    base = BytesLayer.of(d("data"), f("data/a.bin", b"0123456789abcdef"))
    same_size = b"0123456789ABCDEF"
    tops = {1: top(d("data"), f("data/a.bin", same_size), f("data/new.txt", "long enough text\n"))}
    result = run(tmp_path, [base], tops)
    rows = rows_of(result)
    assert rows["/data/a.bin"].blob_ref == f"blobs/{sha(same_size)}"
    assert rows["/data/new.txt"].is_text is False  # over the patched limit
    for content in (same_size, b"long enough text\n"):
        assert (tmp_path / "blobs" / sha(content)).read_bytes() == content
    assert not any(p.name.startswith(".tmp-") for p in (tmp_path / "blobs").iterdir())


def test_crlf_and_lone_carriage_returns_stay_in_their_lines(tmp_path: Path) -> None:
    base = BytesLayer.of(d("etc"), f("etc/dos.txt", "a\r\nb\r\n"))
    result = run(tmp_path, [base], {1: top(d("etc"), f("etc/dos.txt", "a\r\nB\r\nc\rd\n"))})
    assert rows_of(result)["/etc/dos.txt"].hunk == (
        "--- a/etc/dos.txt\n+++ b/etc/dos.txt\n@@ -1,2 +1,3 @@\n a\r\n-b\r\n+B\r\n+c\rd\n"
    )


def test_a_missing_final_newline_is_marked(tmp_path: Path) -> None:
    base = BytesLayer.of(d("etc"), f("etc/conf", "x\ny"))
    result = run(tmp_path, [base], {1: top(d("etc"), f("etc/conf", "x\nz"))})
    assert rows_of(result)["/etc/conf"].hunk == (
        "--- a/etc/conf\n+++ b/etc/conf\n@@ -1,2 +1,2 @@\n x\n"
        "-y\n\\ No newline at end of file\n+z\n\\ No newline at end of file\n"
    )


def test_an_empty_file_gets_an_empty_hunk(tmp_path: Path) -> None:
    rows = rows_of(run(tmp_path, [alpine()], {1: top(d("root", 0o700), f("root/empty", ""))}))
    assert (rows["/root/empty"].is_text, rows["/root/empty"].hunk) == (True, "")
    assert rows["/root/empty"].bytes_after == 0


def test_a_non_directory_entry_replaces_the_lower_subtree(tmp_path: Path) -> None:
    # Docker 29 (containerd), 2026-10-18: after `rm -rf /usr/share/apk && echo x >
    # /usr/share/apk` and `rm -rf /etc/apk && ln -s /tmp /etc/apk`, the top layer holds only
    # the file and the symlink, with no whiteouts for the old children. Seq 1 first chmods a
    # base child, so it is in layer 1 and must not show through layer 2 either.
    layer1 = top(d("usr"), d("usr/share"), d("usr/share/apk"), d("usr/share/apk/keys", 0o700))
    layer2 = top(
        d("usr"), d("usr/share"), f("usr/share/apk", "x\n"), d("etc"), sym("etc/apk", "/tmp")
    )
    result = run(tmp_path, [alpine()], {1: layer1, 2: layer2})
    assert ops_of(result, 1) == {"/usr/share/apk/keys": "modify"}
    assert ops_of(result, 2) == {
        "/etc/apk": "modify",
        "/etc/apk/protected_paths.d": "delete",
        "/etc/apk/protected_paths.d/alpine.list": "delete",
        "/usr/share/apk": "modify",
        "/usr/share/apk/README": "delete",
        "/usr/share/apk/keys": "delete",
        "/usr/share/apk/keys/a.rsa.pub": "delete",
        "/usr/share/apk/keys/b.rsa.pub": "delete",
    }
    rows = rows_of(result, 2)
    assert (rows["/usr/share/apk"].kind_before, rows["/usr/share/apk"].kind_after) == (
        "dir",
        "file",
    )
    assert rows["/usr/share/apk/keys"].mode_before == "0700"  # the version layer 1 held
    assert (rows["/etc/apk"].kind_after, rows["/etc/apk"].is_text) == ("symlink", True)


def test_a_base_layer_replacing_a_directory_hides_its_lower_children(tmp_path: Path) -> None:
    lower = BytesLayer.of(d("opt"), d("opt/tool"), f("opt/tool/old.txt", "old\n"))
    upper = BytesLayer.of(d("opt"), f("opt/tool", "now a file\n"))
    layer = top(d("opt"), d("opt/tool"), f("opt/tool/new.txt", "new\n"))
    result = run(tmp_path, [lower, upper], {1: layer})
    # The base view has /opt/tool as a file, so old.txt never existed in view 0.
    assert ops_of(result) == {"/opt/tool": "modify", "/opt/tool/new.txt": "add"}


def test_other_kinds_have_no_content(tmp_path: Path) -> None:
    rows = rows_of(run(tmp_path, [alpine()], {1: top(d("root", 0o700), fifo("root/pipe"))}))
    row = rows["/root/pipe"]
    assert (row.kind_after, row.is_text, row.sha256_after, row.bytes_after) == (
        "other",
        None,
        None,
        None,
    )


# -- exclusions ----------------------------------------------------------------------------------

SECRET = b"SECRET-HARNESS-CONTENT"


def _harness_members() -> list[Member]:
    return [
        d("root", 0o700),
        d("root/.cache"),
        d("root/.cache/claude"),
        f("root/.cache/claude/state.json", SECRET + b" text"),
        f("root/.cache/claude/state.bin", SECRET + b"\0 binary"),
        d("logs"),
        d("logs/agent"),
        f("logs/agent/claude-code.txt", SECRET),
        d("tmp", 0o1777),
        d("tmp/claude-0"),
        f("tmp/claude-0/out", SECRET),
    ]


def _no_secret_written(tmp_path: Path) -> bool:
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    return not any(SECRET in p.read_bytes() for p in files)


def test_harness_paths_and_directories_made_only_for_them_give_no_row(tmp_path: Path) -> None:
    base = BytesLayer.of(d("root", 0o700), d("tmp", 0o1777))
    result = run(tmp_path, [base], {1: top(*_harness_members())})
    assert result.deltas == ()
    assert _no_secret_written(tmp_path)


def test_a_directory_with_agent_work_beside_harness_paths_gets_a_row(tmp_path: Path) -> None:
    base = BytesLayer.of(d("root", 0o700), d("tmp", 0o1777))
    layer = top(*_harness_members(), d("root/.cache/pip"), f("root/.cache/pip/wheel", "w\n"))
    result = run(tmp_path, [base], {1: layer})
    assert ops_of(result) == {
        "/root/.cache": "add",
        "/root/.cache/pip": "add",
        "/root/.cache/pip/wheel": "add",
    }
    assert _no_secret_written(tmp_path)


def test_deleting_a_directory_that_only_held_harness_paths_gives_no_row(tmp_path: Path) -> None:
    base = BytesLayer.of(
        d("root", 0o700),
        d("root/.cache"),
        d("root/.cache/claude"),
        f("root/.cache/claude/x", SECRET),
        f("root/kept", "k\n"),
    )
    assert (
        run(tmp_path / "a", [base], {1: top(d("root", 0o700), wh("root/.wh..cache"))}).deltas == ()
    )
    both = top(d("root", 0o700), wh("root/.wh..cache"), wh("root/.wh.kept"))
    assert ops_of(run(tmp_path / "b", [base], {1: both})) == {"/root/kept": "delete"}


def test_a_custom_exclusion_predicate(tmp_path: Path) -> None:
    layer = top(d("srv"), f("srv/a", "1"), f("srv/b", "2"))
    result = run(tmp_path, [], {1: layer}, excluded=lambda p: p == "/srv/b")
    assert ops_of(result) == {"/srv": "add", "/srv/a": "add"}


# -- base layers and layer encodings -------------------------------------------------------------


def test_a_later_base_layer_whites_out_an_earlier_ones_file(tmp_path: Path) -> None:
    base = [
        BytesLayer.of(
            d("etc"),
            f("etc/old", "old\n"),
            f("etc/keep", "keep\n"),
            d("opt"),
            d("opt/app"),
            f("opt/app/a", "a\n"),
        ),
        BytesLayer.of(
            d("etc"),
            wh("etc/.wh.old"),
            f("etc/new", "new\n"),
            d("opt"),
            d("opt/app"),
            wh("opt/app/.wh..wh..opq"),
            f("opt/app/b", "b\n"),
        ),
    ]
    layer = top(d("etc"), f("etc/old", "back\n"), wh("etc/.wh.new"), d("opt"), wh("opt/.wh.app"))
    result = run(tmp_path, base, {1: layer})
    assert ops_of(result) == {
        "/etc/old": "add",  # the second base layer had removed it
        "/etc/new": "delete",
        "/opt/app": "delete",
        "/opt/app/b": "delete",  # not /opt/app/a: the base's opaque directory hid it
    }
    assert rows_of(result)["/etc/new"].hunk.endswith("-new\n")


def test_a_path_both_whited_out_and_present_in_one_layer(tmp_path: Path) -> None:
    base = BytesLayer.of(d("srv"), f("srv/a", "1\n"), f("srv/b", "2\n"), d("srv/sub"))
    layer = top(wh(".wh.srv"), d("srv", mode=0o750), f("srv/b", "2\n"))
    assert ops_of(run(tmp_path, [base], {1: layer})) == {
        "/srv": "modify",  # the layer's entry wins over its own whiteout
        "/srv/a": "delete",
        "/srv/sub": "delete",
    }


@pytest.mark.parametrize("gz", [False, True])
def test_member_name_spellings(tmp_path: Path, gz: bool) -> None:
    base = BytesLayer(
        tar_bytes(d("./"), d("./root/", mode=0o700), f("./root/.profile", "p\n"), gz=gz), []
    )
    layer = top(
        d("."),
        d("./root/", mode=0o700),
        f("./root/.bashrc", "alias ll='ls -l'\n"),
        f("./.bashrc", "root-level dotfile\n"),
        wh("./root/.wh..profile"),
        gz=gz,
    )
    assert ops_of(run(tmp_path, [base], {1: layer})) == {
        "/.bashrc": "add",
        "/root/.bashrc": "add",
        "/root/.profile": "delete",
    }


def test_gzip_and_plain_layers_give_the_same_rows(tmp_path: Path) -> None:
    def members() -> list[Member]:
        return [d("etc"), f("etc/motd", "changed\n"), wh("etc/.wh.issue"), f("etc/b", b"\0")]

    plain = run(tmp_path / "plain", [alpine(gz=False)], {1: top(*members(), gz=False)})
    zipped = run(tmp_path / "gz", [alpine(gz=True)], {1: top(*members(), gz=True)})
    assert plain.deltas == zipped.deltas and len(plain.deltas) == 3


# -- the checkpoint chain --------------------------------------------------------------------------


def test_a_missing_image_skips_its_delta_and_the_next(tmp_path: Path) -> None:
    tops = {
        1: top(d("root", 0o700), f("root/a", "1\n")),
        2: top(d("root", 0o700), f("root/a", "1\n"), f("root/b", "2\n")),
        # 3 is gone
        4: top(d("root", 0o700), f("root/a", "1\n"), f("root/b", "2\n"), f("root/c", "c4\n")),
        5: top(d("root", 0o700), f("root/a", "1\n"), f("root/c", "c5\n")),
    }
    result = run(tmp_path, [alpine()], tops, n=5)
    assert result.computed_seqs == (1, 2, 5)
    assert result.skipped_seqs == (3, 4)
    assert {row.step_idx for row in result.deltas} == {step(1), step(2), step(5)}
    assert ops_of(result, 5) == {"/root/b": "delete", "/root/c": "modify"}
    # The before version comes from the skipped checkpoint 4's layer.
    assert rows_of(result, 5)["/root/c"].hunk == (
        "--- a/root/c\n+++ b/root/c\n@@ -1 +1 @@\n-c4\n+c5\n"
    )


def test_missing_first_and_last_images(tmp_path: Path) -> None:
    tops = {2: top(d("root", 0o700), f("root/a", "1\n")), 3: top(d("root", 0o700))}
    result = run(tmp_path, [alpine()], tops, n=4)
    assert (result.computed_seqs, result.skipped_seqs) == ((3,), (1, 2, 4))
    assert ops_of(result, 3) == {"/root/a": "delete"}


def test_no_images_at_all_reads_nothing(tmp_path: Path) -> None:
    base = alpine()
    result = run(tmp_path, [base], {}, n=3)
    assert result == ReconcileResult(deltas=(), computed_seqs=(), skipped_seqs=(1, 2, 3))
    assert base.opened == []


def test_no_checkpoints(tmp_path: Path) -> None:
    result = run(tmp_path, [alpine()], {}, n=0)
    assert result == ReconcileResult(deltas=(), computed_seqs=(), skipped_seqs=())


def test_layers_are_read_a_bounded_number_of_times(tmp_path: Path) -> None:
    base = alpine()
    tops = {seq: top(d("root", 0o700), f(f"root/{seq}", f"{seq}\n")) for seq in range(1, 5)}
    run(tmp_path, [base], tops)
    assert len(base.opened) == 1
    assert all(len(layer.opened) == 2 for layer in tops.values())  # names, then compare


def _records(n: int, **overrides: object) -> list:
    return [make_checkpoint(seq, tool_call_id=call_id(seq), **overrides) for seq in range(1, n + 1)]


def _reconcile(tmp_path: Path, records: list, owner: Mapping[str, int], tops=None):
    return reconcile(
        ImageChain(base_layers=(), tops=tops or {}),
        records,
        owner,
        trial_id=TRIAL,
        blobs_dir=tmp_path / "blobs",
        work_dir=tmp_path / "work",
    )


def test_a_seq_gap_is_an_error(tmp_path: Path) -> None:
    records = [make_checkpoint(1, tool_call_id="a"), make_checkpoint(3, tool_call_id="c")]
    with pytest.raises(ReconcileError, match=r"missing \[2\]"):
        _reconcile(tmp_path, records, {"a": 1, "c": 3})


def test_a_repeated_seq_is_an_error(tmp_path: Path) -> None:
    records = [make_checkpoint(1, tool_call_id="a"), make_checkpoint(1, tool_call_id="b")]
    with pytest.raises(ReconcileError, match="repeated"):
        _reconcile(tmp_path, records, {"a": 1, "b": 2})


def test_a_checkpoint_without_an_owning_step_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ReconcileError, match="toolu_02"):
        _reconcile(tmp_path, _records(2), {call_id(1): 1})


def test_two_checkpoints_owned_by_one_step_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ReconcileError, match="both owned by step 4"):
        _reconcile(tmp_path, _records(2), {call_id(1): 4, call_id(2): 4})


def test_a_top_layer_for_an_unknown_seq_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ReconcileError, match=r"seqs \[3\]"):
        _reconcile(tmp_path, _records(2), {call_id(1): 1, call_id(2): 2}, {3: top(d("x"))})


def test_an_unreadable_top_layer_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ReconcileError, match="top layer of checkpoint 1"):
        _reconcile(tmp_path, _records(1), {call_id(1): 1}, {1: BytesLayer(b"garbage" * 200, [])})


def test_a_stop_checkpoint_is_keyed_by_its_owner_step(tmp_path: Path) -> None:
    stop_id = "stop_1760000000_42"
    records = [
        make_checkpoint(1, tool_call_id="toolu_a"),
        make_checkpoint(2, tool_call_id=stop_id, trigger="stop", tool_name="Stop"),
    ]
    tops = {
        1: top(d("root", 0o700), f("root/a", "1\n")),
        2: top(d("root", 0o700), f("root/a", "1\n"), f("root/bg.log", "from a background job\n")),
    }
    result = reconcile(
        ImageChain(base_layers=(alpine(),), tops=tops),
        list(reversed(records)),  # any order
        {"toolu_a": 3, stop_id: 7},
        trial_id=TRIAL,
        blobs_dir=tmp_path / "blobs",
        work_dir=tmp_path / "work",
    )
    assert [(row.step_idx, row.path) for row in result.deltas] == [
        (3, "/root/a"),
        (7, "/root/bg.log"),
    ]


def test_rows_are_ordered_by_step_then_path_even_when_steps_run_backwards(tmp_path: Path) -> None:
    records = _records(2)
    tops = {1: top(d("z"), d("a")), 2: top(d("z"), d("a"), d("m"))}
    result = reconcile(
        ImageChain(base_layers=(), tops=tops),
        records,
        {call_id(1): 9, call_id(2): 2},
        trial_id=TRIAL,
        blobs_dir=tmp_path / "blobs",
        work_dir=tmp_path / "work",
    )
    assert [(row.step_idx, row.path) for row in result.deltas] == [(2, "/m"), (9, "/a"), (9, "/z")]


def test_two_runs_give_equal_results(tmp_path: Path) -> None:
    def tops() -> dict[int, BytesLayer]:
        return {1: top(*_observed_layer_1()), 2: top(*_observed_layer_2())}

    first = run(tmp_path / "one", [alpine()], tops())
    second = run(tmp_path / "two", [alpine()], tops())
    again = run(tmp_path / "one", [alpine()], tops())  # into the same dirs: blobs already there
    assert first == second == again
    names = sorted(p.name for p in (tmp_path / "one" / "blobs").iterdir())
    assert names == sorted(p.name for p in (tmp_path / "two" / "blobs").iterdir())


# -- hunks ---------------------------------------------------------------------------------------

_LINE = re.compile(r"[^\n]*\n|[^\n]+$")
_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n")
NO_NEWLINE = "\\ No newline at end of file\n"


def apply_patch(before: str, hunk: str) -> str:
    """Apply a unified diff the way `patch` does, checking every context and removed line."""
    if hunk == "":
        return before
    lines = _LINE.findall(hunk)
    assert lines[0].startswith("--- ") and lines[1].startswith("+++ "), hunk
    source = _LINE.findall(before)
    out: list[str] = []
    pos, i = 0, 2
    while i < len(lines):
        header = _HUNK_HEADER.fullmatch(lines[i])
        assert header, lines[i]
        old_start, old_len = int(header[1]), int(header[2] or 1)
        new_len = int(header[4] or 1)
        i += 1
        body: list[tuple[str, str]] = []
        while i < len(lines) and not lines[i].startswith("@@ "):
            if lines[i] == NO_NEWLINE:
                op, text = body[-1]
                assert text.endswith("\n")
                body[-1] = (op, text[:-1])
            else:
                assert lines[i][0] in " -+", lines[i]
                body.append((lines[i][0], lines[i][1:]))
            i += 1
        assert sum(op != "+" for op, _ in body) == old_len
        assert sum(op != "-" for op, _ in body) == new_len
        start = old_start - 1 if old_len else old_start
        assert start >= pos
        out.extend(source[pos:start])
        pos = start
        for op, text in body:
            if op != "+":
                assert source[pos] == text, (source[pos], text)
                pos += 1
            if op != "-":
                out.append(text)
    out.extend(source[pos:])
    return "".join(out)


_PIECES = ["x", "def f():", "    return 1", "", "é", "tab\tin", " ", "\x0b", "\x1c", "\x85"]


def _random_text(rng: random.Random, lines: int) -> str:
    eol = rng.choice(["\n", "\n", "\r\n"])
    body = [rng.choice(_PIECES) + rng.choice(["", "", "\r", " " + str(rng.randint(0, 9))])]
    body += [rng.choice(_PIECES) for _ in range(lines - 1)]
    text = eol.join(body[:lines])
    return text + eol if lines and rng.random() < 0.7 else text


def _edit(rng: random.Random, text: str) -> str:
    lines = _LINE.findall(text)
    for _ in range(rng.randint(0, 4)):
        at = rng.randint(0, len(lines))
        action = rng.choice(["insert", "delete", "replace"])
        if action == "insert" or not lines:
            lines.insert(at, rng.choice(_PIECES) + "\n")
        elif action == "delete":
            del lines[min(at, len(lines) - 1)]
        else:
            lines[min(at, len(lines) - 1)] = rng.choice(_PIECES) + rng.choice(["\n", ""])
    edited = "".join(lines)
    if rng.random() < 0.2:
        edited = edited.rstrip("\n") if edited.endswith("\n") else edited + "\n"
    return edited


def check_hunk_shape(before: str, hunk: str, context: int = 3) -> None:
    """Each hunk has `context` unchanged lines around its changes where the file has them, no
    run of more than 2 * `context` unchanged lines inside it, and more than that between it and
    the next: the grouping GNU diff and difflib use."""
    total = len(_LINE.findall(before))
    previous_end = None
    for chunk in re.split(r"(?m)^(?=@@ )", hunk)[1:]:
        header = _HUNK_HEADER.match(chunk)
        assert header, chunk
        old_start, old_len = int(header[1]), int(header[2] or 1)
        ops = [line[0] for line in _LINE.findall(chunk)[1:] if line != NO_NEWLINE]
        assert set(ops) - {" "}, chunk
        lead = len(ops) - len("".join(ops).lstrip(" "))
        trail = len(ops) - len("".join(ops).rstrip(" "))
        inner = "".join(ops).strip(" ")
        assert max((len(run) for run in re.findall(" +", inner)), default=0) <= 2 * context
        first = old_start - 1 if old_len else old_start
        change_start = first + lead
        change_end = first + sum(op != "+" for op in ops) - trail
        assert lead == min(context, change_start), chunk
        assert trail == min(context, total - change_end), chunk
        if previous_end is not None:
            assert change_start - previous_end > 2 * context
        previous_end = change_end


@pytest.mark.parametrize("matching", ["exact", "edit script", "autojunk"])
def test_hunks_apply_and_keep_their_shape(matching: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if matching != "exact":
        monkeypatch.setattr(reconcile_module, "_EXACT_MATCH_LIMIT", -1)
    if matching == "autojunk":
        monkeypatch.setattr(reconcile_module, "_EDIT_SCRIPT_BUDGET", -1)
    rng = random.Random(16)
    for _ in range(1500):
        before = _random_text(rng, rng.randint(0, 40))
        after = _edit(rng, before) if rng.random() < 0.9 else _random_text(rng, 5)
        hunk = _unified_diff("/p/q r", before, after)
        assert apply_patch(before, hunk) == after, (before, after, hunk)
        assert (hunk == "") == (_LINE.findall(before) == _LINE.findall(after))
        check_hunk_shape(before, hunk)


def _edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Insertions plus deletions, by dynamic programming over the LCS."""
    lcs = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            lcs[i][j] = lcs[i + 1][j + 1] + 1 if a[i] == b[j] else max(lcs[i + 1][j], lcs[i][j + 1])
    return len(a) + len(b) - 2 * lcs[0][0]


def test_the_edit_script_is_a_shortest_one() -> None:
    rng = random.Random(1986)
    for _ in range(2000):
        a = rng.choices("abc", k=rng.randint(0, 12))
        b = rng.choices("abc", k=rng.randint(0, 12))
        codes = _edit_script(a, b, budget=10**6)
        assert codes is not None
        rebuilt, i, j = [], 0, 0
        for tag, i1, i2, j1, j2 in codes:
            assert (i1, j1) == (i, j) and tag in ("equal", "replace", "delete", "insert")
            if tag == "equal":
                assert a[i1:i2] == b[j1:j2] and i2 > i1
            elif tag == "delete":
                assert i2 > i1 and j1 == j2
            elif tag == "insert":
                assert i1 == i2 and j2 > j1
            else:
                assert i2 > i1 and j2 > j1
            rebuilt += b[j1:j2]
            i, j = i2, j2
        assert (i, j) == (len(a), len(b)) and rebuilt == b
        changed = sum(i2 - i1 + j2 - j1 for tag, i1, i2, j1, j2 in codes if tag != "equal")
        assert changed == _edit_distance(a, b), (a, b, codes)
        # Runs alternate: neighbouring equal runs, or neighbouring edits, are merged.
        assert all((x[0] == "equal") != (y[0] == "equal") for x, y in pairwise(codes))


def test_the_edit_script_gives_up_past_its_budget() -> None:
    assert _edit_script(list("abcdefgh"), list("hgfedcba"), budget=10) is None


def test_hunks_of_distinct_lines_equal_difflibs() -> None:
    # With no repeated line the alignment is unambiguous, so the output must be difflib's.
    rng = random.Random(6113)
    fresh = iter(range(10**9))
    for _ in range(500):
        old = [f"line {next(fresh)}\n" for _ in range(rng.randint(0, 60))]
        new = list(old)
        for _ in range(rng.randint(1, 5)):
            at = rng.randint(0, len(new))
            if rng.random() < 0.5 or not new:
                new[at:at] = [f"new {next(fresh)}\n" for _ in range(rng.randint(1, 3))]
            else:
                del new[at : at + rng.randint(1, 3)]
        expected = "".join(difflib.unified_diff(old, new, "a/p/q r", "b/p/q r"))
        assert _unified_diff("/p/q r", "".join(old), "".join(new)) == expected


def test_a_small_edit_to_a_repetitive_file_is_a_small_hunk() -> None:
    # difflib's autojunk would rewrite every line here, and matching without it is quadratic.
    rng = random.Random(1)
    lines = [rng.choice(["}\n", "\n", "    return x\n", "  else:\n"]) for _ in range(20000)]
    before = "".join(lines)
    after = "".join(lines[:9000] + ["inserted\n"] + lines[9000:15000] + lines[15001:])
    hunk = _unified_diff("/src/x.c", before, after)
    assert apply_patch(before, hunk) == after
    assert hunk.count("\n+") + hunk.count("\n-") <= 4  # one insertion, one deletion
    check_hunk_shape(before, hunk)


def test_a_large_rewrite_of_ordinary_code_is_matched_exactly() -> None:
    # Too many edits for the edit script's budget, cheap for difflib without autojunk; with
    # autojunk the braces and blank lines (each over 1% of lines) would go unmatched.
    rng = random.Random(5)

    def code(n: int) -> list[str]:
        return [
            rng.choice(["}\n", "\n"])
            if rng.random() < 0.3
            else f"    v{rng.randrange(10**6)} = f()\n"
            for _ in range(n)
        ]

    old = code(3000)
    new = [line if rng.random() < 0.6 else code(1)[0] for line in old]
    hunk = _unified_diff("/src/x.py", "".join(old), "".join(new))
    assert apply_patch("".join(old), hunk) == "".join(new)

    def changed(diff: str) -> int:
        return sum(1 for line in _LINE.findall(diff)[2:] if line[0] in "+-")

    assert changed(hunk) < changed("".join(difflib.unified_diff(old, new, "a", "b")))


def test_a_rewritten_repetitive_file_still_gives_a_valid_hunk() -> None:
    rng = random.Random(2)
    before = "".join(f"{rng.randrange(4)},{rng.randrange(4)}\n" for _ in range(5000))
    after = "".join(f"{rng.randrange(4)},{rng.randrange(4)}\n" for _ in range(5000))
    hunk = _unified_diff("/data.csv", before, after)
    assert apply_patch(before, hunk) == after
    check_hunk_shape(before, hunk)


def test_hunk_headers_name_the_absent_side_dev_null() -> None:
    assert _unified_diff("/x", None, "a\n") == "--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+a\n"
    assert (
        _unified_diff("/x", "a\nb\n", None) == "--- a/x\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    assert _unified_diff("/x", None, "") == ""


def test_separate_hunks_keep_three_lines_of_context() -> None:
    before = "".join(f"{i}\n" for i in range(20))
    after = before.replace("2\n", "two\n", 1).replace("17\n", "seventeen\n")
    hunk = _unified_diff("/n", before, after)
    assert hunk.count("@@ -") == 2
    assert "@@ -1,6 +1,6 @@\n" in hunk and "@@ -15,6 +15,6 @@\n" in hunk
    assert apply_patch(before, hunk) == after


# -- differential test: reconcile against a filesystem simulator --------------------------------
#
# The simulator keeps whole filesystem states (path -> Node) and writes cumulative top layers the
# way containerd does: every path that differs from the base or was touched since the container
# started, with its parent directories; one whiteout at the top-most removed base path, except
# under a path that is now a file or symlink, whose one entry replaces the subtree; for a base
# directory removed and recreated, either explicit whiteouts per removed child, an opaque
# marker, or a whiteout of the directory beside its own entry. The expected rows come from
# comparing consecutive states directly, so they share no logic with reconcile.


@dataclass(frozen=True)
class Node:
    kind: str  # file, dir, symlink
    mode: int
    uid: int = 0
    gid: int = 0
    data: bytes = b""
    target: str = ""


State = dict[str, Node]
_NAMES = ["a", "b", "c d", ".bashrc", "x.txt", "bin.dat", "é", "lib", "z"]
_HARNESS_DIRS = ["/root/.cache/claude", "/tmp/claude-0", "/logs/agent", "/home/u/.claude"]
_SECRET = b"EXCLUDED-SECRET"
_OWNERS = [(0, 0), (1000, 1000), (0, 1000)]


def _parent(path: str) -> str:
    return path.rpartition("/")[0] or "/"


def _join(parent: str, name: str) -> str:
    return f"{'' if parent == '/' else parent}/{name}"


def _sim_text(rng: random.Random) -> bytes:
    return _random_text(rng, rng.randint(0, 6)).encode()


def _sim_binary(rng: random.Random) -> bytes:
    noise = bytes(rng.randrange(256) for _ in range(rng.randint(0, 50)))
    return noise + b"\0" if rng.random() < 0.6 else b"\xff\xfe" + noise.replace(b"\0", b"")


@dataclass
class Sim:
    rng: random.Random
    state: State
    lower: State  # the state the emitted layer is relative to
    touched: set[str] = field(default_factory=set)
    styles: dict[str, str] = field(default_factory=dict)  # recreated lower dir -> encoding

    def kept(self, *kinds: str) -> list[str]:
        return sorted(p for p, n in self.state.items() if n.kind in kinds and not excluded(p))

    def touch(self, path: str) -> None:
        while path != "/":
            self.touched.add(path)
            path = _parent(path)

    def fresh(self, parent: str) -> str | None:
        for _ in range(8):
            name = self.rng.choice(_NAMES) + self.rng.choice(["", "", str(self.rng.randint(0, 9))])
            path = _join(parent, name)
            if path not in self.state and not excluded(path):
                return path
        return None

    def create(self, path: str, node: Node) -> None:
        lower = self.lower.get(path)
        if node.kind == "dir" and lower is not None and lower.kind == "dir":
            self.styles[path] = self.rng.choice(["explicit", "explicit", "opaque", "whiteout"])
        self.state[path] = node
        self.touch(path)

    def remove(self, path: str) -> None:
        for p in [p for p in self.state if p == path or p.startswith(path + "/")]:
            del self.state[p]
            self.styles.pop(p, None)
        self.touch(_parent(path))

    def a_file(self, rng: random.Random) -> Node:
        data = _sim_text(rng) if rng.random() < 0.7 else _sim_binary(rng)
        return Node("file", rng.choice([0o644, 0o755, 0o600]), *rng.choice(_OWNERS), data=data)

    # Operations; each does nothing when it has no target.

    def write(self) -> None:
        path = self.fresh(self.rng.choice(["/", *self.kept("dir")]))
        if path:
            self.create(path, self.a_file(self.rng))

    def append(self) -> None:
        if files := self.kept("file"):
            path = self.rng.choice(files)
            node = self.state[path]
            self.state[path] = replace(node, data=node.data + (_sim_text(self.rng) or b"\n"))
            self.touch(path)

    def flip_a_byte(self) -> None:  # same size, other content
        if files := [p for p in self.kept("file") if self.state[p].data]:
            path = self.rng.choice(files)
            data = bytearray(self.state[path].data)
            data[self.rng.randrange(len(data))] ^= 0x01
            self.state[path] = replace(self.state[path], data=bytes(data))
            self.touch(path)

    def rewrite_same(self) -> None:  # touch, `cp -p` onto itself, chmod to the same mode
        if paths := self.kept("file", "dir", "symlink"):
            self.touch(self.rng.choice(paths))

    def chmod(self) -> None:
        if paths := self.kept("file", "dir"):
            path = self.rng.choice(paths)
            mode = self.rng.choice([0o644, 0o600, 0o755, 0o700, 0o4755, 0o1777])
            self.state[path] = replace(self.state[path], mode=mode)
            self.touch(path)

    def chown(self) -> None:
        if paths := self.kept("file", "dir", "symlink"):
            path = self.rng.choice(paths)
            uid, gid = self.rng.choice(_OWNERS)
            self.state[path] = replace(self.state[path], uid=uid, gid=gid)
            self.touch(path)

    def rm(self) -> None:
        if paths := self.kept("file", "symlink"):
            self.remove(self.rng.choice(paths))

    def rm_rf(self) -> None:
        if dirs := self.kept("dir"):
            self.remove(self.rng.choice(dirs))

    def mkdir(self) -> None:
        path = self.fresh(self.rng.choice(["/", *self.kept("dir")]))
        if path:
            self.create(
                path, Node("dir", self.rng.choice([0o755, 0o700]), *self.rng.choice(_OWNERS))
            )

    def recreate(self) -> None:  # mkdir of a removed lower directory
        dirs = {"/", *self.kept("dir")}
        gone = sorted(
            p
            for p, n in self.lower.items()
            if n.kind == "dir" and p not in self.state and _parent(p) in dirs and not excluded(p)
        )
        if gone:
            path = self.rng.choice(gone)
            mode = self.rng.choice([self.lower[path].mode, 0o700])
            self.create(path, Node("dir", mode, self.lower[path].uid, self.lower[path].gid))

    def rmdir(self) -> None:
        dirs = self.kept("dir")
        if empty := [p for p in dirs if not any(_parent(q) == p for q in self.state)]:
            self.remove(self.rng.choice(empty))

    def symlink(self) -> None:
        target = self.rng.choice(["/etc/motd", "../x", "a b", "é/ü", "/" + "t" * 30])
        links = self.kept("symlink")
        if links and self.rng.random() < 0.5:
            path = self.rng.choice(links)
            self.state[path] = replace(self.state[path], target=target)
            self.touch(path)
        elif path := self.fresh(self.rng.choice(["/", *self.kept("dir")])):
            self.create(path, Node("symlink", 0o777, *self.rng.choice(_OWNERS), target=target))

    def change_type(self) -> None:  # e.g. `rm -rf d && echo x > d`: any directory, full or not
        if paths := self.kept("file", "symlink", "dir"):
            path = self.rng.choice(paths)
            old = self.state[path].kind
            self.remove(path)
            kind = self.rng.choice([k for k in ("file", "dir", "symlink") if k != old])
            if kind == "file":
                self.create(path, self.a_file(self.rng))
            elif kind == "dir":
                self.create(path, Node("dir", 0o755))
            else:
                self.create(path, Node("symlink", 0o777, target="/elsewhere"))

    def link(self) -> None:  # `ln`: same content, mode, and owner; may be written as LNKTYPE
        files = self.kept("file")
        if files and (path := self.fresh(self.rng.choice(["/", *self.kept("dir")]))):
            source = self.rng.choice(files)
            self.create(path, self.state[source])
            self.touch(source)

    def harness(self) -> None:  # what Harbor and Claude Code write
        root = self.rng.choice(_HARNESS_DIRS)
        for p in [*list(reversed(list(ancestors(root))))[1:], root]:  # top down, not /
            if p not in self.state:
                self.create(p, Node("dir", 0o755))
            elif self.state[p].kind != "dir":
                return
        path = _join(root, self.rng.choice(["state.json", "out", "x.bin"]))
        data = _SECRET + bytes(self.rng.randrange(256) for _ in range(self.rng.randint(0, 9)))
        self.create(path, Node("file", 0o644, data=data))

    def step(self) -> None:
        operations: list[Callable[[], None]] = [
            self.write,
            self.write,
            self.append,
            self.flip_a_byte,
            self.rewrite_same,
            self.chmod,
            self.chown,
            self.rm,
            self.rm_rf,
            self.mkdir,
            self.recreate,
            self.recreate,
            self.rmdir,
            self.symlink,
            self.change_type,
            self.link,
            self.harness,
        ]
        self.rng.choice(operations)()


def _view(lower: State, entries: State, whiteouts: set[str], opaque: set[str]) -> State:
    """The docstring's view rule, written out naively, to check the simulator's layers."""
    nondirs = {p for p, n in entries.items() if n.kind != "dir"}
    out = {
        p: n
        for p, n in lower.items()
        if p not in whiteouts
        and not any(a in whiteouts or a in opaque or a in nondirs for a in ancestors(p))
    }
    out.update(entries)
    return out


def _emit(rng: random.Random, sim: Sim) -> bytes:
    """The layer containerd would write for `sim.state` over `sim.lower`."""
    lower, state = sim.lower, sim.state
    covered = {
        p: style
        for p, style in sim.styles.items()
        if style != "explicit" and p in state and state[p].kind == "dir" and p in lower
    }

    def under_covered(path: str) -> bool:
        return any(a in covered for a in ancestors(path))

    entries: State = {}

    def add(path: str) -> None:
        for p in [path, *ancestors(path)]:
            if p != "/":
                entries[p] = state[p]

    for path, node in state.items():
        if lower.get(path) != node or path in sim.touched or path in covered or under_covered(path):
            add(path)

    def replaced(path: str) -> bool:  # under a path that is now a file or symlink
        return any(a in state and state[a].kind != "dir" for a in ancestors(path))

    removed = {p for p in lower if p not in state}
    whiteouts = {
        p for p in removed if _parent(p) not in removed and not under_covered(p) and not replaced(p)
    }
    for path in whiteouts:
        if _parent(path) != "/":
            add(_parent(path))
    whiteouts |= {p for p, style in covered.items() if style == "whiteout"}
    opaque = {p for p, style in covered.items() if style == "opaque"}
    assert _view(lower, entries, whiteouts, opaque) == state  # the simulator's own check

    dot = rng.random() < 0.5
    slash = rng.random() < 0.5

    def name(path: str, directory: bool = False) -> str:
        return ("./" if dot else "") + path[1:] + ("/" if directory and slash else "")

    items: list[tuple[str, Node | None]] = [(path, node) for path, node in entries.items()]
    items += [(_join(_parent(p), ".wh." + p.rpartition("/")[2]), None) for p in whiteouts]
    items += [(_join(p, ".wh..wh..opq"), None) for p in opaque]
    members: list[Member] = []
    if rng.random() < 0.3:
        members.append(d(rng.choice([".", "./"])))
    first_copy: dict[tuple[bytes, int, int, int], str] = {}
    for path, node in sorted(items, key=lambda item: item[0]):
        if node is None:
            members.append(wh(name(path)))
            continue
        mtime = rng.randint(0, 2**31)
        if node.kind == "dir":
            members.append(d(name(path, True), node.mode, node.uid, node.gid, mtime))
        elif node.kind == "symlink":
            members.append(sym(name(path), node.target, node.mode, node.uid, node.gid))
        else:
            key = (node.data, node.mode, node.uid, node.gid)
            if key in first_copy and rng.random() < 0.5:
                members.append(hard(name(path), first_copy[key], node.mode))
            else:
                members.append(f(name(path), node.data, node.mode, node.uid, node.gid, mtime))
                first_copy.setdefault(key, name(path))
    return tar_bytes(*members, gz=rng.random() < 0.5)


def _base(rng: random.Random) -> tuple[list[BytesLayer], State]:
    """One to three base layers and the base they make."""
    tree: State = {
        "/etc": Node("dir", 0o755),
        "/usr": Node("dir", 0o755),
        "/root": Node("dir", 0o700),
        "/tmp": Node("dir", 0o1777),
        "/home": Node("dir", 0o755),
    }
    first = Sim(rng, tree, lower={})
    for _ in range(rng.randint(5, 25)):
        rng.choice([first.write, first.write, first.mkdir, first.symlink, first.link])()
    if rng.random() < 0.3:
        first.harness()
    layers = [BytesLayer(_emit(rng, first), [])]
    state = first.state
    for _ in range(rng.choice([0, 0, 1, 2])):
        later = Sim(rng, state, lower=dict(state))
        for _ in range(rng.randint(1, 4)):
            later.step()
        layers.append(BytesLayer(_emit(rng, later), []))
        state = later.state
    return layers, state


def _sha(node: Node | None) -> str | None:
    if node is None or node.kind == "dir":
        return None
    return hashlib.sha256(node.data if node.kind == "file" else node.target.encode()).hexdigest()


def _size(node: Node | None) -> int | None:
    if node is None or node.kind == "dir":
        return None
    return len(node.data if node.kind == "file" else node.target.encode())


def _key(path: str, b: Node | None, a: Node | None) -> tuple:
    return (
        path,
        "add" if b is None else "delete" if a is None else "modify",
        b.kind if b else None,
        a.kind if a else None,
        _sha(b),
        _sha(a),
        _size(b),
        _size(a),
        f"{b.mode:04o}" if b else None,
        f"{a.mode:04o}" if a else None,
        f"{b.uid}:{b.gid}" if b else None,
        f"{a.uid}:{a.gid}" if a else None,
    )


def _row_key(row: DeltaRow) -> tuple:
    return (
        row.path,
        row.op,
        row.kind_before,
        row.kind_after,
        row.sha256_before,
        row.sha256_after,
        row.bytes_before,
        row.bytes_after,
        row.mode_before,
        row.mode_after,
        row.owner_before,
        row.owner_after,
    )


def _expected(before: State, after: State) -> dict[str, tuple[Node | None, Node | None]]:
    changes = {
        p: (before.get(p), after.get(p))
        for p in before.keys() | after.keys()
        if before.get(p) != after.get(p)
    }
    kept = [p for p in changes if not excluded(p)]
    harness = [p for p in changes if excluded(p)]

    def only_holds_excluded(path: str) -> bool:
        b, a = changes[path]
        if b is not None and a is not None:
            return False
        under = path + "/"
        return not any(p.startswith(under) for p in kept) and any(
            p.startswith(under) for p in harness
        )

    return {p: changes[p] for p in kept if not only_holds_excluded(p)}


def _text(node: Node | None, limit: int) -> bool:
    if node is None or node.kind == "symlink":
        return True
    if len(node.data) > limit or b"\0" in node.data:
        return False
    try:
        node.data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _content(node: Node | None) -> str | None:
    if node is None or node.kind == "dir":
        return None
    return node.data.decode("utf-8") if node.kind == "file" else node.target


def _check_sequence(seed: int, root: Path, limit: int) -> None:
    rng = random.Random(seed)
    base_layers, base = _base(rng)
    sim = Sim(rng, dict(base), lower=base)
    states = [dict(base)]
    tops: dict[int, BytesLayer] = {}
    n = rng.randint(1, 6)
    for seq in range(1, n + 1):
        for _ in range(rng.randint(1, 3)):
            sim.step()
        tops[seq] = BytesLayer(_emit(rng, sim), [])
        states.append(dict(sim.state))
    if rng.random() < 0.2:
        del tops[rng.randint(1, n)]
    result = run(root, base_layers, tops, n=n)

    computed = [k for k in range(1, n + 1) if k in tops and (k == 1 or k - 1 in tops)]
    assert result.computed_seqs == tuple(computed), seed
    assert result.skipped_seqs == tuple(k for k in range(1, n + 1) if k not in computed), seed
    assert [(r.step_idx, r.path) for r in result.deltas] == sorted(
        (r.step_idx, r.path) for r in result.deltas
    )
    for k in range(1, n + 1):
        rows = rows_of(result, k)
        if k not in computed:
            assert rows == {}, (seed, k)
            continue
        expected = _expected(states[k - 1], states[k])
        assert {_row_key(r) for r in rows.values()} == {
            _key(p, b, a) for p, (b, a) in expected.items()
        }, (seed, k)
        for path, (b, a) in expected.items():
            row = rows[path]
            versions = [x for x in (b, a) if x is not None and x.kind != "dir"]
            is_text = all(_text(x, limit) for x in versions) if versions else None
            assert row.is_text == is_text, (seed, k, path)
            if is_text and _sha(b) != _sha(a):
                assert row.hunk is not None, (seed, k, path)
                assert apply_patch(_content(b) or "", row.hunk) == (_content(a) or "")
            else:
                assert row.hunk is None, (seed, k, path)
            content_changed = b is None or b.kind != "file" or _sha(b) != _sha(a)
            if a is not None and a.kind == "file" and not _text(a, limit) and content_changed:
                assert row.blob_ref == f"blobs/{_sha(a)}", (seed, k, path)
                assert (root / "blobs" / str(_sha(a))).read_bytes() == a.data
            else:
                assert row.blob_ref is None, (seed, k, path)
    for written in root.rglob("*"):
        if written.is_file():
            assert _SECRET not in written.read_bytes(), (seed, written)


@pytest.mark.parametrize("chunk", range(8))
def test_reconcile_agrees_with_a_filesystem_simulator(
    chunk: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Odd chunks shrink the text limit so larger files take the non-text, fetch-again path.
    limit = 40 if chunk % 2 else MAX_TEXT_BYTES
    monkeypatch.setattr(reconcile_module, "MAX_TEXT_BYTES", limit)
    for seed in range(chunk * 40, (chunk + 1) * 40):
        _check_sequence(seed, tmp_path / str(seed), limit)


def test_the_simulator_reaches_every_kind_of_change(tmp_path: Path) -> None:
    """The differential test is only as good as the cases it reaches."""
    seen: set[tuple[str, str | None, str | None]] = set()
    encodings: set[str] = set()
    for seed in range(120):
        rng = random.Random(seed)
        _, base = _base(rng)
        sim = Sim(rng, dict(base), lower=base)
        previous = dict(base)
        for _ in range(rng.randint(1, 6)):
            for _ in range(rng.randint(1, 3)):
                sim.step()
            encodings |= {
                style
                for p, style in sim.styles.items()
                if p in sim.state and sim.state[p].kind == "dir"
            }
            with tarfile.open(fileobj=BytesLayer(_emit(rng, sim), []).open(), mode="r|*") as tar:
                for info in tar:
                    if info.islnk():
                        encodings.add("hard link")
                    elif info.name.rpartition("/")[2].startswith(".wh."):
                        encodings.add("whiteout member")
            if any(
                previous[p].kind == "dir"
                and p in sim.state
                and sim.state[p].kind != "dir"
                and any(q.startswith(p + "/") for q in previous)
                for p in previous
            ):
                encodings.add("non-directory over a subtree")
            expected = _expected(previous, sim.state)
            for b, a in expected.values():
                op = "add" if b is None else "delete" if a is None else "modify"
                seen.add((op, b.kind if b else None, a.kind if a else None))
            changed = {p for p in previous.keys() | sim.state.keys() if not excluded(p)}
            changed = {p for p in changed if previous.get(p) != sim.state.get(p)}
            if changed - expected.keys():
                encodings.add("directory holding only harness paths")
            previous = dict(sim.state)
    assert encodings >= {
        "explicit",
        "opaque",
        "whiteout",
        "hard link",
        "whiteout member",
        "directory holding only harness paths",
        "non-directory over a subtree",
    }
    for kinds in [
        ("add", None, "file"),
        ("add", None, "dir"),
        ("add", None, "symlink"),
        ("delete", "file", None),
        ("delete", "dir", None),
        ("delete", "symlink", None),
        ("modify", "file", "file"),
        ("modify", "dir", "dir"),
        ("modify", "symlink", "symlink"),
        ("modify", "file", "symlink"),
        ("modify", "file", "dir"),
        ("modify", "dir", "file"),
    ]:
        assert kinds in seen, kinds
