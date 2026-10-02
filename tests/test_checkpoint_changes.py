import pytest

from trajlab.checkpoint.changes import (
    FIND_FIELDS,
    ListingError,
    changed_paths,
    excluded,
    parse_listing,
)


def _raw(*entries: tuple[str, ...]) -> str:
    return "".join("\0".join(entry) + "\0" for entry in entries)


def _entry(path: str, kind: str = "f", ctime: str = "1.5", mode: str = "644", size: str = "3"):
    return (kind, mode, "0", "0", size, ctime, "", path)


def test_find_fields_match_the_entry_shape() -> None:
    assert len(FIND_FIELDS) == len(_entry("/x"))


def test_parse_listing_handles_any_file_name() -> None:
    weird = "/app/tab\there\nnewline"
    listing = parse_listing(
        _raw(_entry("/"), _entry(weird), ("l", "777", "0", "0", "4", "2.0", "/tgt", "/lnk"))
    )
    assert listing[weird] == ("f", "644", "0", "0", "3", "1.5", "")
    assert listing["/lnk"][6] == "/tgt"


def test_parse_listing_rejects_a_torn_listing() -> None:
    with pytest.raises(ListingError):
        parse_listing("f\0644\0")


def _diff(before: list[tuple[str, ...]], after: list[tuple[str, ...]]) -> list[str]:
    return changed_paths(parse_listing(_raw(*before)), parse_listing(_raw(*after)))


def test_added_removed_and_modified() -> None:
    before = [_entry("/a"), _entry("/b"), _entry("/c")]
    after = [_entry("/a"), _entry("/b", ctime="9.0"), _entry("/d")]
    assert _diff(before, after) == ["+/d", "-/c", "~/b"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("mode", "600"), ("size", "4"), ("ctime", "1.6")],
)
def test_file_metadata_changes_count(field: str, value: str) -> None:
    assert _diff([_entry("/a")], [_entry("/a", **{field: value})]) == ["~/a"]


def test_directory_change_time_and_size_are_ignored() -> None:
    # Overlayfs copy-up and changes to children move these; the children carry the change.
    before = [_entry("/app", kind="d", ctime="1.0", size="4096")]
    after = [_entry("/app", kind="d", ctime="7.0", size="8192")]
    assert _diff(before, after) == []
    assert _diff(before, [_entry("/app", kind="d", mode="700")]) == ["~/app"]


def test_type_change_counts() -> None:
    assert _diff([_entry("/a")], [_entry("/a", kind="l")]) == ["~/a"]


@pytest.mark.parametrize(
    "path",
    [
        "/logs/agent/claude-code.txt",
        "/tmp/claude-0/-app/abc/tasks/b1.output",
        "/tmp/claude-1000/x",
        "/tmp/claude-code-settings/settings.json",
        "/root/.claude/settings.json",
        "/root/.claude.json",
        "/home/agent/.cache/claude/x",
        "/root/.local/state/claude/locks/2.1.278.lock",
    ],
)
def test_harness_paths_are_excluded(path: str) -> None:
    assert excluded(path)
    assert _diff([], [_entry(path)]) == []


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/claude",
        "/tmp/claudette",
        "/tmp/claude-0x",
        "/root/.claude.json.bak",
        "/app/logs/x",
        "/logsx",
    ],
)
def test_lookalike_paths_are_not_excluded(path: str) -> None:
    assert not excluded(path)
    assert _diff([], [_entry(path)]) == [f"+{path}"]


def test_directories_made_only_for_harness_paths_are_ignored() -> None:
    after = [
        _entry("/root/.cache", kind="d"),
        _entry("/root/.cache/claude", kind="d"),
        _entry("/root/.cache/claude/x"),
    ]
    assert _diff([], after) == []


def test_directories_holding_agent_files_still_count() -> None:
    after = [
        _entry("/root/.cache", kind="d"),
        _entry("/root/.cache/claude", kind="d"),
        _entry("/root/.cache/pip", kind="d"),
    ]
    assert _diff([], after) == ["+/root/.cache", "+/root/.cache/pip"]


def test_an_empty_directory_the_agent_made_counts() -> None:
    assert _diff([], [_entry("/app/out", kind="d")]) == ["+/app/out"]
