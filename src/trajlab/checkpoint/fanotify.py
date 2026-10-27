"""A small fanotify binding over ctypes, for the write recorder (ADR-0021).

One notification group in FID mode with directory and name reporting (`FAN_REPORT_DFID_NAME`):
every event names the directory that holds the object (a file handle) and the object's name in
it, so creates, deletes, and renames are reported like writes, with no file descriptor per event.
The directory's path is read back with `open_by_handle_at` and `/proc/self/fd`. Marking a whole
filesystem (`FAN_MARK_FILESYSTEM`) and an unlimited queue need `CAP_SYS_ADMIN`; resolving a
handle needs `CAP_DAC_READ_SEARCH`. Linux 5.9 or later.

`parse_events` is pure, so the event layout is tested offline on hand-made bytes.
"""

import ctypes
import ctypes.util
import os
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass

FAN_CLOEXEC = 0x00000001
FAN_NONBLOCK = 0x00000002
FAN_CLASS_NOTIF = 0x00000000
FAN_UNLIMITED_QUEUE = 0x00000010
FAN_REPORT_DIR_FID = 0x00000400
FAN_REPORT_NAME = 0x00000800
FAN_REPORT_DFID_NAME = FAN_REPORT_DIR_FID | FAN_REPORT_NAME

FAN_MARK_ADD = 0x00000001
FAN_MARK_REMOVE = 0x00000002
FAN_MARK_FILESYSTEM = 0x00000100

FAN_MODIFY = 0x00000002
FAN_CLOSE_WRITE = 0x00000008
FAN_OPEN = 0x00000020
FAN_MOVED_FROM = 0x00000040
FAN_MOVED_TO = 0x00000080
FAN_CREATE = 0x00000100
FAN_DELETE = 0x00000200
FAN_Q_OVERFLOW = 0x00004000
FAN_EVENT_ON_CHILD = 0x08000000
FAN_ONDIR = 0x40000000

FAN_EVENT_INFO_TYPE_FID = 1
FAN_EVENT_INFO_TYPE_DFID_NAME = 2
FAN_EVENT_INFO_TYPE_DFID = 3
FANOTIFY_METADATA_VERSION = 3

AT_FDCWD = -100
# struct fanotify_event_metadata: event_len, vers, reserved, metadata_len, mask, fd, pid.
_METADATA = struct.Struct("=IBBHQii")
# struct fanotify_event_info_header: info_type, pad, len.
_INFO_HEADER = struct.Struct("=BBH")
_FSID = 8  # __kernel_fsid_t: two ints
_HANDLE_HEADER = struct.Struct("=Ii")  # struct file_handle: handle_bytes, handle_type
# Room for a few thousand events per read.
READ_BYTES = 1 << 20


class FanotifyError(OSError):
    """A fanotify call failed (no privilege, no kernel support, or a bad path)."""


@dataclass(frozen=True)
class RawEvent:
    """One event as the kernel reported it: the directory's file handle (`fsid` and the
    `struct file_handle` that follows it, as `open_by_handle_at` takes it) and the name in it."""

    mask: int
    pid: int
    fsid: bytes
    handle: bytes
    name: bytes


def parse_events(buffer: bytes) -> Iterator[RawEvent]:
    """The events in one `read` of a `FAN_REPORT_DFID_NAME` group. An event without a directory
    record (a queue overflow) has an empty handle and name."""
    offset = 0
    while offset + _METADATA.size <= len(buffer):
        event_len, version, _, metadata_len, mask, _fd, pid = _METADATA.unpack_from(buffer, offset)
        if event_len < _METADATA.size or offset + event_len > len(buffer):
            break
        if version != FANOTIFY_METADATA_VERSION:
            raise FanotifyError(f"fanotify metadata version {version}, not 3")
        fsid = handle = name = b""
        info = offset + metadata_len
        end = offset + event_len
        while info + _INFO_HEADER.size <= end:
            info_type, _, length = _INFO_HEADER.unpack_from(buffer, info)
            if length < _INFO_HEADER.size:
                break
            if info_type in (FAN_EVENT_INFO_TYPE_DFID_NAME, FAN_EVENT_INFO_TYPE_DFID):
                body = info + _INFO_HEADER.size
                fsid = bytes(buffer[body : body + _FSID])
                handle_bytes, _ = _HANDLE_HEADER.unpack_from(buffer, body + _FSID)
                start = body + _FSID
                stop = start + _HANDLE_HEADER.size + handle_bytes
                handle = bytes(buffer[start:stop])
                if info_type == FAN_EVENT_INFO_TYPE_DFID_NAME:
                    raw = bytes(buffer[stop : info + length])
                    name = raw.split(b"\0", 1)[0]
            info += length
        yield RawEvent(mask=mask, pid=pid, fsid=fsid, handle=handle, name=name)
        offset += event_len


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
    libc.fanotify_init.restype = ctypes.c_int
    libc.fanotify_mark.argtypes = [
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint64,
        ctypes.c_int,
        ctypes.c_char_p,
    ]
    libc.fanotify_mark.restype = ctypes.c_int
    libc.open_by_handle_at.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    libc.open_by_handle_at.restype = ctypes.c_int
    return libc


def _check(result: int, what: str) -> int:
    if result < 0:
        errno = ctypes.get_errno()
        raise FanotifyError(errno, f"{what}: {os.strerror(errno)}")
    return result


class Fanotify:
    """A notification group reporting directory handles and names. Not thread-safe."""

    def __init__(self, *, unlimited_queue: bool = True) -> None:
        self._libc = _libc()
        flags = FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK | FAN_REPORT_DFID_NAME
        if unlimited_queue:
            flags |= FAN_UNLIMITED_QUEUE
        self.fd = _check(
            self._libc.fanotify_init(flags, os.O_RDONLY | os.O_LARGEFILE), "fanotify_init"
        )
        self._mount_fds: list[int] = []

    def mark_filesystem(self, path: str, mask: int) -> None:
        """Report `mask` events on the whole filesystem that holds `path`."""
        _check(
            self._libc.fanotify_mark(
                self.fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, mask, AT_FDCWD, os.fsencode(path)
            ),
            f"fanotify_mark {path}",
        )
        self._mount_fds.append(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))

    def mark_directory(self, path: str, mask: int, *, remove: bool = False) -> None:
        """Report (or stop reporting) `mask` events on `path` and the files in it."""
        flags = FAN_MARK_REMOVE if remove else FAN_MARK_ADD
        _check(
            self._libc.fanotify_mark(
                self.fd, flags, mask | FAN_EVENT_ON_CHILD, AT_FDCWD, os.fsencode(path)
            ),
            f"fanotify_mark {path}",
        )

    def read(self) -> bytes:
        """The queued events, or b"" when none are queued."""
        try:
            return os.read(self.fd, READ_BYTES)
        except BlockingIOError:
            return b""

    def open_handle(self, handle: bytes) -> int:
        """An O_PATH descriptor for a handle of the marked filesystem; the caller closes it."""
        return _check(
            self._libc.open_by_handle_at(
                self._mount_fds[0], handle, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
            ),
            "open_by_handle_at",
        )

    def close(self) -> None:
        for fd in (*self._mount_fds, self.fd):
            os.close(fd)
        self._mount_fds = []


class DirectoryPaths:
    """Directory handle to path, cached a short while: most events land in a few directories.
    A rename or removal of a directory clears the cache, since a cached path may be stale."""

    TTL_S = 5.0

    def __init__(self, group: Fanotify) -> None:
        self._group = group
        self._cache: dict[bytes, tuple[bytes | None, float]] = {}

    def resolve(self, handle: bytes) -> bytes | None:
        """The directory's path on the host, or None if it is gone."""
        now = time.monotonic()
        cached = self._cache.get(handle)
        if cached is not None and now - cached[1] < self.TTL_S:
            return cached[0]
        try:
            fd = self._group.open_handle(handle)
        except OSError:
            path = None
        else:
            try:
                path = os.readlink(b"/proc/self/fd/%d" % fd)
            except OSError:
                path = None
            finally:
                os.close(fd)
            if path is not None and path.endswith(b" (deleted)"):
                path = None
        if len(self._cache) > 100_000:
            self._cache.clear()
        self._cache[handle] = (path, now)
        return path

    def forget(self) -> None:
        self._cache.clear()
