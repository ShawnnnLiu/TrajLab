import pytest

from trajlab.checkpoint.processes import PROBE_SCRIPT, ProbeError, parse_probe
from trajlab.contracts import ListeningPort, ProcessEntry

# What PROBE_SCRIPT prints in a container where the agent started a server: the probe is pid
# 90; 91 is its `$(...)` subshell and 92 the `tr` inside it, all of which must be dropped.
PROBE_OUTPUT = (
    "self\t90\n"
    "p\t1\t0\tsleep infinity \n"
    "p\t40\t1\tnode /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js --print \n"
    "p\t77\t40\tpython3 server.py --port 8080 \n"
    "p\t90\t0\tsh -c echo self \n"
    "p\t91\t90\tsh -c echo self \n"
    "p\t92\t91\ttr \\0   \n"
    "t\ttcp\t00000000:1F90\n"
    "t\ttcp\t0100007F:0016\n"
    "t\ttcp6\t00000000000000000000000000000000:1F90\n"
    "t\ttcp\t00000000:1F90\n"  # the same socket twice (e.g. two listeners): kept once
)


def test_parse_probe_keeps_the_containers_processes_and_drops_the_probe() -> None:
    processes, listening = parse_probe(PROBE_OUTPUT)

    assert processes == (
        ProcessEntry(pid=1, ppid=0, cmd="sleep infinity"),
        ProcessEntry(
            pid=40,
            ppid=1,
            cmd="node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js --print",
        ),
        ProcessEntry(pid=77, ppid=40, cmd="python3 server.py --port 8080"),
    )
    assert listening == (
        ListeningPort(proto="tcp", address="127.0.0.1", port=22),
        ListeningPort(proto="tcp", address="0.0.0.0", port=8080),
        ListeningPort(proto="tcp6", address="0" * 32, port=8080),
    )


def test_parse_probe_needs_the_probes_own_pid() -> None:
    with pytest.raises(ProbeError):
        parse_probe("p\t1\t0\tsleep infinity\n")


def test_parse_probe_tolerates_missing_parents_and_odd_lines() -> None:
    processes, listening = parse_probe("self\t5\np\t9\t\tbash -l\nnoise\nt\ttcp\tzz:qq\n")
    assert processes == (ProcessEntry(pid=9, ppid=None, cmd="bash -l"),)
    assert listening == ()


def test_probe_script_is_posix_sh() -> None:
    # Task images may lack bash: no arrays, no [[ ]], no $'...'.
    assert "[[ " not in PROBE_SCRIPT and "$'" not in PROBE_SCRIPT
    assert "/proc/net/tcp6" in PROBE_SCRIPT
