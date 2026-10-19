import io
import tarfile

from trajlab.checkpoint.diffs import (
    MAX_FILES_PER_CALL,
    MAX_LINE_PRODUCT,
    Candidate,
    candidates,
    file_change,
    is_text,
    prior_versions,
    read_tar,
    sha256,
    tar_members,
    to_read,
    unified_diff,
)
from trajlab.contracts import MAX_TEXT_BYTES


def _entry(kind: str, size: int = 10, link: str = "") -> tuple[str, ...]:
    return (kind, "644", "0", "0", str(size), "1.0", link)


def test_text_rule() -> None:
    assert is_text(None) and is_text(b"") and is_text("héllo\n".encode())
    assert not is_text(b"a\0b")
    assert not is_text(b"\xff\xfe")
    assert not is_text(b"a" * (MAX_TEXT_BYTES + 1))


def test_unified_diff_of_an_edit() -> None:
    diff = unified_diff(
        "/app/parse.py", b"a\nfor i in range(n + 1):\nb\n", b"a\nfor i in range(n):\nb\n"
    )
    assert diff is not None
    assert diff.splitlines()[:2] == ["--- a/app/parse.py", "+++ b/app/parse.py"]
    assert "-for i in range(n + 1):" in diff and "+for i in range(n):" in diff


def test_unified_diff_of_a_new_file_and_the_line_cap() -> None:
    created = unified_diff("/app/new.txt", None, b"x\n")
    assert created is not None and created.startswith("--- /dev/null\n+++ b/app/new.txt")
    big = b"line\n" * 1001
    assert 1001 * 1001 > MAX_LINE_PRODUCT
    assert unified_diff("/app/big.txt", big, big + b"more\n") is None


def test_candidates_skip_directories_and_name_ops() -> None:
    after = {
        "/app/a.py": _entry("f", 12),
        "/app/link": _entry("l", 0, "a.py"),
        "/app/dir": _entry("d"),
        "/app/fifo": _entry("p", 0),
    }
    found = candidates(["+/app/a.py", "~/app/link", "+/app/dir", "~/app/fifo", "-/app/gone"], after)
    assert found == [
        Candidate("/app/a.py", "added", "file", 12),
        Candidate("/app/link", "modified", "symlink", 0),
        Candidate("/app/fifo", "modified", "other", 0),
        Candidate("/app/gone", "removed", "other", None),
    ]


def test_to_read_prefers_quiet_paths_and_caps_the_count() -> None:
    noisy = [
        Candidate(f"/usr/lib/python3/site-packages/p{i}.py", "added", "file", 5) for i in range(30)
    ]
    quiet = [
        Candidate("/app/main.py", "modified", "file", 5),
        Candidate("/app/huge.bin", "added", "file", MAX_TEXT_BYTES + 1),
    ]
    chosen = to_read(noisy + quiet)
    assert chosen[0] == "/app/main.py"
    assert "/app/huge.bin" not in chosen  # over the size limit: never read
    assert len(chosen) == MAX_FILES_PER_CALL


def test_file_change_records_diff_and_both_hashes() -> None:
    change = file_change(
        Candidate("/app/a.txt", "modified", "file", 4),
        after=b"new\n",
        before=b"old\n",
        before_known=True,
    )
    assert (change.sha256, change.before_sha256) == (sha256(b"new\n"), sha256(b"old\n"))
    assert change.is_text and change.skipped is None
    assert change.diff is not None and "+new" in change.diff


def test_file_change_without_a_known_before_version_has_hashes_only() -> None:
    change = file_change(
        Candidate("/app/a.txt", "modified", "file", 4),
        after=b"new\n",
        before=None,
        before_known=False,
    )
    assert change.sha256 == sha256(b"new\n") and change.before_sha256 is None
    assert (change.diff, change.skipped) == (None, "over_budget")


def test_file_change_for_binary_unread_removed_and_symlink() -> None:
    binary = file_change(
        Candidate("/app/x.bin", "added", "file", 3), after=b"\0\1\2", before=None, before_known=True
    )
    assert (binary.is_text, binary.diff, binary.skipped) == (False, None, "not_text")
    unread = file_change(
        Candidate("/app/many.txt", "added", "file", 3),
        after=None,
        before=None,
        before_known=True,
        skipped="over_file_cap",
    )
    assert (unread.sha256, unread.skipped) == (None, "over_file_cap")
    too_large = file_change(
        Candidate("/app/big.log", "added", "file", MAX_TEXT_BYTES + 1),
        after=None,
        before=None,
        before_known=True,
    )
    assert too_large.skipped == "too_large"
    removed = file_change(
        Candidate("/app/gone.txt", "removed", "other", None),
        after=None,
        before=b"was\n",
        before_known=True,
    )
    assert (removed.op, removed.before_sha256, removed.diff) == ("removed", sha256(b"was\n"), None)
    link = file_change(
        Candidate("/app/l", "added", "symlink", 0), after=None, before=None, before_known=True
    )
    assert (link.kind, link.sha256, link.skipped) == ("symlink", None, None)


def test_read_tar_round_trip_and_truncation() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in [("app/a.txt", b"A"), ("./app/b.txt", b"BB")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        directory = tarfile.TarInfo("app/sub")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
    data = buffer.getvalue()

    assert read_tar(data) == {"/app/a.txt": b"A", "/app/b.txt": b"BB"}
    assert read_tar(data[: 512 + 512]) == {"/app/a.txt": b"A"}  # cut after the first member
    assert read_tar(b"") == {}
    assert tar_members(["/app/a.txt", "/etc/x"]) == ["app/a.txt", "etc/x"]


def test_prior_versions_splits_held_from_unknown() -> None:
    held, unknown = prior_versions(["/a", "/b", "/c"], {"/a": b"1", "/c": None})
    assert held == {"/a": b"1", "/c": None}
    assert unknown == ["/b"]
