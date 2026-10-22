"""Which programs run and which TCP ports listen in a container (ADR-0015 §2).

The backend runs `PROBE_SCRIPT` with `sh -c` as root in the container. It reads `/proc`
directly because task images may lack `ps`, `ss`, and `netstat`, as the hook may lack `jq`.
This module holds the script and parses its output; it is pure: no Docker, no I/O.

Output lines, tab-separated:
    self <pid>                       the probe's own shell
    p <pid> <ppid> <command line>    one per process with a command line (kernel threads have none)
    t <file> <local address>         one per listening socket in /proc/net/tcp or tcp6
The probe's own shell and everything it starts (sed, tr, cut, subshells) are dropped here.
"""

from trajlab.contracts import ListeningPort, ProcessEntry

MAX_COMMAND_CHARS = 500

PROBE_SCRIPT = r"""echo "self	$$"
for p in /proc/[0-9]*; do
  pid=${p#/proc/}
  cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null | tr '\t\n' '  ' | cut -c1-500)
  [ -n "$cmd" ] || continue
  ppid=$(sed -n 's/^PPid:[[:space:]]*//p' "$p/status" 2>/dev/null)
  printf 'p\t%s\t%s\t%s\n' "$pid" "$ppid" "$cmd"
done
for f in /proc/net/tcp /proc/net/tcp6; do
  [ -r "$f" ] || continue
  sed -n '2,$p' "$f" | while read -r sl local remote st rest; do
    [ "$st" = 0A ] && printf 't\t%s\t%s\n' "${f##*/}" "$local"
  done
done
exit 0
"""


class ProbeError(ValueError):
    """The probe output has no `self` line."""


def parse_probe(raw: str) -> tuple[tuple[ProcessEntry, ...], tuple[ListeningPort, ...]]:
    """Processes (by pid) and listening ports (sorted, unique) from `PROBE_SCRIPT` output."""
    probe: int | None = None
    entries: dict[int, ProcessEntry] = {}
    ports: set[ListeningPort] = set()
    for line in raw.splitlines():
        fields = line.split("\t")
        if fields[0] == "self" and len(fields) == 2 and fields[1].isdigit():
            probe = int(fields[1])
        elif fields[0] == "p" and len(fields) >= 4 and fields[1].isdigit():
            ppid = int(fields[2]) if fields[2].strip().isdigit() else None
            cmd = "\t".join(fields[3:]).strip()[:MAX_COMMAND_CHARS]
            entries[int(fields[1])] = ProcessEntry(pid=int(fields[1]), ppid=ppid, cmd=cmd)
        elif fields[0] == "t" and len(fields) == 3 and fields[1] in ("tcp", "tcp6"):
            port = _listening(fields[1], fields[2])
            if port is not None:
                ports.add(port)
    if probe is None:
        raise ProbeError("probe output has no self line")
    kept = tuple(
        entry for pid, entry in sorted(entries.items()) if not _descends_from(pid, probe, entries)
    )
    return kept, tuple(sorted(ports, key=lambda p: (p.port, p.proto, p.address)))


def _descends_from(pid: int, ancestor: int, entries: dict[int, ProcessEntry]) -> bool:
    seen: set[int] = set()
    current: int | None = pid
    while current is not None and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        entry = entries.get(current)
        current = entry.ppid if entry is not None else None
    return False


def _listening(proto: str, local: str) -> ListeningPort | None:
    """`0100007F:1F90` -> 127.0.0.1:8080; IPv6 addresses stay as /proc writes them."""
    address_hex, _, port_hex = local.partition(":")
    try:
        port = int(port_hex, 16)
    except ValueError:
        return None
    if proto == "tcp" and len(address_hex) == 8:
        octets = bytes.fromhex(address_hex)[::-1]
        address = ".".join(str(octet) for octet in octets)
    else:
        address = address_hex.lower()
    return ListeningPort(proto=proto, address=address, port=port)  # type: ignore[arg-type]
