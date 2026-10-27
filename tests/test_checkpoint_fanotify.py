"""The fanotify event layout (ADR-0021), on hand-made bytes: no kernel, no privilege."""

import struct

import pytest

from trajlab.checkpoint.fanotify import (
    FAN_CLOSE_WRITE,
    FAN_CREATE,
    FAN_EVENT_INFO_TYPE_DFID,
    FAN_EVENT_INFO_TYPE_DFID_NAME,
    FAN_MODIFY,
    FAN_ONDIR,
    FAN_Q_OVERFLOW,
    FanotifyError,
    RawEvent,
    parse_events,
)

FSID = struct.pack("=ii", 7, 9)
HANDLE = struct.pack("=Ii", 8, 1) + b"\x01\x02\x03\x04\x05\x06\x07\x08"


def info(info_type: int, name: bytes | None) -> bytes:
    body = FSID + HANDLE + (b"" if name is None else name + b"\0")
    body += b"\0" * (-(len(body) + 4) % 4)  # records are padded to 4 bytes
    return struct.pack("=BBH", info_type, 0, 4 + len(body)) + body


def event(mask: int, pid: int, *infos: bytes, version: int = 3) -> bytes:
    rest = b"".join(infos)
    return struct.pack("=IBBHQii", 24 + len(rest), version, 0, 24, mask, -1, pid) + rest


def test_events_carry_the_directory_handle_and_the_name() -> None:
    data = event(
        FAN_CREATE | FAN_MODIFY | FAN_CLOSE_WRITE,
        4242,
        info(FAN_EVENT_INFO_TYPE_DFID_NAME, b"probe.txt"),
    ) + event(FAN_CREATE | FAN_ONDIR, 4243, info(FAN_EVENT_INFO_TYPE_DFID_NAME, b"d"))
    assert list(parse_events(data)) == [
        RawEvent(
            mask=FAN_CREATE | FAN_MODIFY | FAN_CLOSE_WRITE,
            pid=4242,
            fsid=FSID,
            handle=HANDLE,
            name=b"probe.txt",
        ),
        RawEvent(mask=FAN_CREATE | FAN_ONDIR, pid=4243, fsid=FSID, handle=HANDLE, name=b"d"),
    ]


def test_a_directory_record_without_a_name_and_an_overflow() -> None:
    data = event(FAN_MODIFY, 5, info(FAN_EVENT_INFO_TYPE_DFID, None)) + event(FAN_Q_OVERFLOW, 0)
    assert list(parse_events(data)) == [
        RawEvent(mask=FAN_MODIFY, pid=5, fsid=FSID, handle=HANDLE, name=b""),
        RawEvent(mask=FAN_Q_OVERFLOW, pid=0, fsid=b"", handle=b"", name=b""),
    ]


def test_a_cut_event_ends_the_batch() -> None:
    whole = event(FAN_MODIFY, 5, info(FAN_EVENT_INFO_TYPE_DFID_NAME, b"a"))
    assert [e.name for e in parse_events(whole + whole[:30])] == [b"a"]


def test_another_metadata_version_is_refused() -> None:
    with pytest.raises(FanotifyError, match="version 2"):
        list(parse_events(event(FAN_MODIFY, 5, version=2)))
