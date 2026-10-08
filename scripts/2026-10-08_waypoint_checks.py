"""Checks ADR-0013 requires on the capture server before a Waypoint corpus run.

Manual: needs root (passwordless sudo), Waypoint >= 0.7.0, CRIU >= 4.0, and Docker. Never run by
the tests, never imported by `src/`.

    uv run python scripts/2026-10-08_waypoint_checks.py parallel [--image python:3.12-slim]
        Check 2. A save is made with a web server running on port 8000, then opened three times
        at once, each copy in its own network. Every copy must answer on port 8000 and reach the
        internet. Cleans up after itself; the exported image stays under <state>/rootfs/ unless
        --remove-rootfs is given.

    uv run python scripts/2026-10-08_waypoint_checks.py save corpus/jobs/<job>/<trial>
        Checks 1 and 4 for one finished trial: its save exists, holds no test files and no
        verifier output, and how much disk the session uses.
"""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from trajlab.capture.waypoint import (
    DEFAULT_STATE_DIR,
    Network,
    Waypoint,
    create_network,
    delete_network,
    export_rootfs,
    nameservers,
    root_prefix,
)
from trajlab.contracts import WAYPOINT_RECORD_FILENAME, WaypointRecord

SERVER = "nohup python3 -m http.server 8000 >/dev/null 2>&1 &"
LOCAL = (
    'python3 -c "import urllib.request as u; '
    "print(u.urlopen('http://127.0.0.1:8000', timeout=5).status)\""
)
INTERNET = (
    'python3 -c "import urllib.request as u; '
    "print(u.urlopen('https://pypi.org/simple/', timeout=15).status)\""
)


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


async def run_in(w: Waypoint, session: str, fork: str, command: str) -> str:
    result = await w("exec", session, fork, "--", command, check=False, timeout=120)
    return (result.stdout + result.stderr).strip()


async def parallel(args: argparse.Namespace) -> int:
    w = Waypoint(Path(args.state_dir), args.waypoint_bin)
    if subprocess.run(["docker", "image", "inspect", args.image], capture_output=True).returncode:
        docker("pull", args.image)
    image_id = docker("image", "inspect", "--format", "{{.Id}}", args.image).strip()
    rootfs = await export_rootfs(w, args.image, image_id)
    networks: list[Network] = []
    session = None
    forks: list[str] = []
    failed = False
    try:
        source_net = await create_network(w, "check-source")
        networks.append(source_net)
        out = await w("init", str(rootfs), "--quiet", "--shell", network=source_net.name)
        session = out.stdout.strip().splitlines()[-1].split(",")[0]
        print(f"session {session}, source network {source_net.name}")
        servers = "".join(f"nameserver {s}\\n" for s in nameservers())
        await run_in(
            w, session, "main", f"rm -f /etc/resolv.conf; printf '{servers}' >/etc/resolv.conf"
        )
        await run_in(w, session, "main", SERVER)
        await asyncio.sleep(2)
        print(f"source answers on :8000 -> {await run_in(w, session, 'main', LOCAL)}")
        await w("snapshot", session, "main", "final", timeout=600, network=source_net.name)
        print("saved final")

        async def open_copy(index: int) -> str:
            network = await create_network(w, f"check-copy-{index}")
            networks.append(network)
            fork = f"copy{index}"
            await w("fork", session, "final", "--id", fork, timeout=600, network=network.name)
            forks.append(fork)
            return fork

        opened = await asyncio.gather(*(open_copy(i) for i in range(args.copies)))
        for fork in opened:
            local = await run_in(w, session, fork, LOCAL)
            internet = await run_in(w, session, fork, INTERNET)
            ok = local.endswith("200") and internet.endswith("200")
            failed |= not ok
            print(
                f"{fork}: port 8000 -> {local!r}; internet -> {internet!r}; "
                f"{'PASS' if ok else 'FAIL'}"
            )
    finally:
        for fork in forks:
            assert session is not None
            await w("destroy", session, fork, check=False)
        if session is not None:
            await w("cleanup", session, "--force", check=False)
        for network in networks:
            await delete_network(w, network)
        if args.remove_rootfs:
            subprocess.run([*root_prefix(), "rm", "-rf", str(rootfs)])
    print("check 2:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


def root_output(*args: str) -> str:
    return subprocess.run([*root_prefix(), *args], capture_output=True, text=True).stdout.strip()


def save(args: argparse.Namespace) -> int:
    trial = Path(args.trial_dir)
    record = WaypointRecord.model_validate_json((trial / WAYPOINT_RECORD_FILENAME).read_text())
    print(json.dumps(record.model_dump(mode="json"), indent=2))
    if record.save is None:
        print("check 1: FAIL (no save)")
        return 1
    upper = Path(record.save.path) / "upper"
    problems = []
    if root_output("sh", "-c", f"[ -e {upper}/tests ] && echo yes") == "yes":
        problems.append("the save holds /tests")
    verifier_files = root_output("find", f"{upper}/logs/verifier", "-type", "f")
    if verifier_files:
        problems.append(f"the save holds verifier output: {verifier_files.splitlines()[:3]}")
    session_dir = Path(record.sessions_dir) / record.session
    print("top level of the save's files:", root_output("ls", "-A", str(upper)).split())
    for part in ("original", f"checkpoints/{record.save.checkpoint_id}/upper"):
        print(f"disk {part}: {root_output('du', '-sh', str(session_dir / part)).split()[:1]}")
    print(
        f"disk criu: {root_output('du', '-shL', str(Path(record.save.path) / 'criu')).split()[:1]}"
    )
    print("check 1:", "FAIL: " + "; ".join(problems) if problems else "PASS")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--waypoint-bin", default="waypoint")
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("parallel")
    one.add_argument("--image", default="python:3.12-slim")
    one.add_argument("--copies", type=int, default=3)
    one.add_argument("--remove-rootfs", action="store_true")
    two = commands.add_parser("save")
    two.add_argument("trial_dir")
    args = parser.parse_args()
    if args.command == "parallel":
        return asyncio.run(parallel(args))
    return save(args)


if __name__ == "__main__":
    sys.exit(main())
