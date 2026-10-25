"""Can Waypoint save after every Claude Code tool call? A one-off probe (2026-10-08).

Waypoint's `exec` and `snapshot` take the same per-fork lock, so a `claude -p` started as one
`waypoint exec` (as Harbor does) blocks every save until it exits. This probe starts Claude Code
in the background instead, so the lock is free, and has a PostToolUse hook pause the agent after
each tool call until the host has saved:

    hook (in the environment)            host (this script)
    touch /tmp/hook/<n>.req  ──────────► sees it via `waypoint exec ls`
    waits for <n>.ack                     waypoint snapshot <s> main step<n>   (freezes everything,
                                                                                Claude Code too)
                         ◄──────────────  waypoint exec touch /tmp/hook/<n>.ack

It passes if the agent finishes its task after being frozen and resumed at every tool call.

Result on 2026-10-08 (AWS capture server, Waypoint 0.7.0, CRIU 4.2.1, Claude Code 2.1.294, Haiku):
PASS with `--irmap /work --irmap /root/.claude --irmap /root`: 5 saves, 0.6-0.7 s each, 123-132 MB
of memory images each, Claude Code alive after every one, task finished ("DONE", all files right).
Without those paths every save fails: Claude Code keeps inotify watches on its working folder and
its config folder, and CRIU cannot record a watch on OverlayFS unless told where to find the folder
("fsnotify: Can't dump that handle").

Manual: needs root (passwordless sudo), Waypoint >= 0.7.0, CRIU, Docker, network, and a Claude
login (a credentials file). The login token ends up in the saved memory images; everything under
--state-dir is deleted at the end unless --keep is given. Uses the host network (no namespace).

    uv run python scripts/2026-10-08_waypoint_per_action_probe.py \
        --credentials ~/.claude/.credentials.json --claude ~/.local/bin/claude
"""

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from trajlab.capture.waypoint import Waypoint, root_prefix

BASE_IMAGE = "debian:bookworm-slim"
TASK = (
    "Work in /work. Make exactly five separate Bash tool calls, one command per call, in this "
    "order: `echo one > a.txt`, `echo two > b.txt`, `echo three > c.txt`, `echo four > d.txt`, "
    "`cat a.txt b.txt c.txt d.txt > all.txt`. Do not combine them. Then reply with the single "
    "word DONE."
)
HOOK = """#!/bin/bash
cat > /dev/null
mkdir -p /tmp/hook
n=$(ls /tmp/hook/ | grep -c '\\.req$'); n=$((n+1))
touch /tmp/hook/$n.req
for i in $(seq 1 3000); do [ -e /tmp/hook/$n.ack ] && exit 0; sleep 0.2; done
exit 0
"""
# Which files Claude Code's inotify watches point at: inode -> path, then the count of watch fds.
WATCHES = r"""p=$(cat /work/claude.pid)
for f in /proc/$p/fdinfo/*; do grep -h '^inotify' "$f"; done | sed -E 's/.*ino:([0-9a-f]+) .*/\1/' \
  | sort -u | while read -r i; do
    echo "watch: $(find / -xdev -inum $((16#$i)) 2>/dev/null | head -1)"
  done
echo "inotify fds: $(ls -l /proc/$p/fd | grep -c inotify)"
"""
SETTINGS = {
    "hooks": {
        "PostToolUse": [
            {
                "matcher": "Bash|Write|Edit",
                "hooks": [{"type": "command", "command": "/root/hook.sh", "timeout": 900}],
            }
        ]
    }
}


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def root(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return sh(*root_prefix(), *args, check=check)


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.state = Path(args.state_dir)
        self.wp = Waypoint(self.state, args.waypoint_bin)
        self.session: str | None = None
        self.rows: list[dict[str, object]] = []

    def waypoint(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = self.wp.argv(*args)
        if self.args.irmap:
            # CRIU reads extra options from CRIU_CONFIG_FILE; only this probe's commands see it.
            config = self.state / "criu.conf"
            argv.insert(argv.index("env") + 1, f"CRIU_CONFIG_FILE={config}")
        done = subprocess.run(argv, capture_output=True, text=True)
        if check and done.returncode != 0:
            raise RuntimeError(f"waypoint {' '.join(args[:3])}: {done.stdout}{done.stderr}")
        return done

    def run(self, command: str) -> str:
        assert self.session is not None
        done = self.waypoint("exec", self.session, "main", "--", command, check=False)
        return (done.stdout + done.stderr).strip()

    def copy_in(self, source: Path, target: str) -> None:
        assert self.session is not None
        self.waypoint("cp", self.session, str(source), f"main:{target}")

    def build_rootfs(self) -> Path:
        rootfs = self.state / "rootfs"
        if subprocess.run(
            ["docker", "image", "inspect", BASE_IMAGE], capture_output=True
        ).returncode:
            sh("docker", "pull", "-q", BASE_IMAGE)
        root("mkdir", "-p", str(rootfs))
        cid = sh("docker", "create", BASE_IMAGE, "/bin/true").stdout.strip()
        try:
            export = f"docker export {cid} | tar -x -C {shlex.quote(str(rootfs))} --numeric-owner"
            root("sh", "-c", export)
        finally:
            sh("docker", "rm", "-f", cid, check=False)
        claude = Path(self.args.claude).resolve()
        root("install", "-m", "755", str(claude), str(rootfs / "usr/local/bin/claude"))
        root("mkdir", "-p", str(rootfs / "etc/ssl/certs"), str(rootfs / "work"))
        root("cp", "/etc/ssl/certs/ca-certificates.crt", str(rootfs / "etc/ssl/certs/"))
        return rootfs

    def criu_errors(self, checkpoint: str) -> str:
        assert self.session is not None
        log = self.state / "sessions" / self.session / "checkpoints" / checkpoint / "criu"
        found = root(
            "sh", "-c", f"grep -h -B6 -E 'Error|irmap' {log}/dump.log | tail -40", check=False
        ).stdout
        return found

    def start(self) -> None:
        print(f"state dir {self.state}; free disk {shutil.disk_usage('/').free / 1e9:.1f} GB")
        rootfs = self.build_rootfs()
        if self.args.irmap:
            text = "".join(f"irmap-scan-path {path}\n" for path in self.args.irmap)
            root("sh", "-c", f"printf %s {shlex.quote(text)} > {self.state / 'criu.conf'}")
        out = self.waypoint("init", str(rootfs), "--quiet", "--shell").stdout
        self.session = out.strip().splitlines()[-1].split(",")[0]
        print(f"session {self.session}")
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "hook.sh").write_text(HOOK)
            (stage / "settings.json").write_text(json.dumps(SETTINGS))
            (stage / "task.txt").write_text(TASK)
            env = (
                "HOME=/root IS_SANDBOX=1 DISABLE_AUTOUPDATER=1 "
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 PATH=/usr/local/bin:/usr/bin:/bin"
            )
            (stage / "start.sh").write_text(
                "cd /work\n"
                f'nohup env {env} claude -p "$(cat /root/task.txt)" --model {self.args.model} '
                "--settings /root/settings.json --dangerously-skip-permissions "
                "--output-format stream-json --verbose "
                "> /work/claude.out 2> /work/claude.err < /dev/null &\n"
                "echo $! > /work/claude.pid\n"
            )
            for name in ("hook.sh", "settings.json", "task.txt", "start.sh"):
                self.copy_in(stage / name, f"/root/{name}")
        self.run("mkdir -p /root/.claude && chmod 755 /root/hook.sh")
        self.copy_in(Path(self.args.credentials).expanduser(), "/root/.claude/.credentials.json")
        self.run("chmod 600 /root/.claude/.credentials.json")
        print(self.run("claude --version"))
        baseline = self.waypoint("snapshot", self.session, "main", "baseline", check=False)
        print(f"save before Claude Code starts: {'ok' if baseline.returncode == 0 else 'FAILED'}")
        if baseline.returncode != 0:
            print(self.criu_errors("baseline"))
            raise RuntimeError("the environment itself cannot be saved")
        self.run("bash /root/start.sh")
        print("Claude Code started in the background")

    def loop(self) -> None:
        assert self.session is not None
        acked: set[int] = set()
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            state = self.run(
                "ls /tmp/hook 2>/dev/null | tr '\\n' ' '; echo; "
                "[ -e /proc/$(cat /work/claude.pid)/status ] && echo alive || echo exited"
            )
            files, _, status = state.rpartition("\n")
            pending = sorted(
                int(name.removesuffix(".req"))
                for name in files.split()
                if name.endswith(".req") and int(name.removesuffix(".req")) not in acked
            )
            for n in pending:
                if n == 1:
                    print(self.run(WATCHES))
                started = time.monotonic()
                done = self.waypoint("snapshot", self.session, "main", f"step{n}", check=False)
                save_s = time.monotonic() - started
                criu = self.state / "sessions" / self.session / "checkpoints" / f"step{n}" / "criu"
                upper = criu.parent / "upper"
                size = root("du", "-sbL", str(criu), check=False).stdout.split()[:1]
                files_size = root("du", "-sb", str(upper), check=False).stdout.split()[:1]
                alive = "alive" in self.run(
                    "[ -e /proc/$(cat /work/claude.pid)/status ] && echo alive || echo exited"
                )
                row = {
                    "call": n,
                    "save_ok": done.returncode == 0,
                    "save_s": round(save_s, 2),
                    "memory_mb": round(int(size[0]) / 1e6, 1) if size else None,
                    "files_kb": round(int(files_size[0]) / 1e3, 1) if files_size else None,
                    "claude_alive_after": alive,
                    "error": None if done.returncode == 0 else self.criu_errors(f"step{n}"),
                }
                self.rows.append(row)
                print(json.dumps(row))
                self.run(f"touch /tmp/hook/{n}.ack")
                acked.add(n)
                if done.returncode != 0:
                    return
            if status.strip() == "exited" and not pending:
                return
            time.sleep(1)
        print("timed out waiting for Claude Code")

    def report(self) -> int:
        print("\n=== result")
        last = self.run("tail -n 1 /work/claude.out")
        try:
            result = json.loads(last)
            print(
                json.dumps(
                    {
                        k: result.get(k)
                        for k in ("type", "subtype", "is_error", "result", "num_turns")
                    }
                )
            )
        except ValueError:
            print("last output line:", last[:500])
            print("stderr:", self.run("tail -n 20 /work/claude.err")[:2000])
        print("files:", self.run("ls /work; echo; cat /work/all.txt 2>/dev/null"))
        ok = bool(self.rows) and all(r["save_ok"] and r["claude_alive_after"] for r in self.rows)
        ok = ok and "four" in self.run("cat /work/all.txt 2>/dev/null")
        print(f"saves: {len(self.rows)}; probe {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    def cleanup(self) -> None:
        if self.args.keep:
            print(f"kept {self.state} (contains the login token in memory images)")
            return
        if self.session is not None:
            self.waypoint("cleanup", self.session, "--force", check=False)
        root("rm", "-rf", str(self.state), check=False)
        print(f"removed {self.state}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--claude", required=True, help="a Claude Code binary to copy in")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--state-dir", default="/var/tmp/tl-probe")
    parser.add_argument("--waypoint-bin", default="waypoint")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--irmap",
        action="append",
        help="CRIU irmap-scan-path, repeatable: where to look up files Claude Code watches",
    )
    args = parser.parse_args()
    probe = Probe(args)
    try:
        probe.start()
        probe.loop()
        return probe.report()
    finally:
        probe.cleanup()


if __name__ == "__main__":
    sys.exit(main())
