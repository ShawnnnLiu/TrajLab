"""The checker (ADR-0022) on hand-written records: each check, each exclusion, and the prefix
rule. No trial data, no Docker."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from trajlab.checkpoint.patterns import (
    CallView,
    Checker,
    StopFacts,
    TrialRecords,
    crash_line,
    find_self_kill,
    instruction_outputs,
    read_command,
    replay,
)
from trajlab.checkpoint.shell import parse
from trajlab.contracts import (
    CallChanges,
    CallRecord,
    FileChange,
    ProcessEntry,
    ProcessSnapshot,
)

T0 = datetime(2026, 11, 1, tzinfo=UTC)
HARNESS = (ProcessEntry(pid=1, cmd="sh -c sleep infinity"), ProcessEntry(pid=7, cmd="claude"))


def record(seq: int, *, changed: tuple[str, ...] = (), total: int | None = None, **kw: Any):
    defaults: dict[str, Any] = dict(
        tool_call_id=f"toolu_{seq:02d}",
        trial_name="t__x",
        tool_name="Bash",
        call_seq=seq,
        outcome="checkpoint" if changed else "unchanged",
        change="changed" if changed else "unchanged",
        changed_paths=changed,
        changed_paths_total=len(changed) if total is None else total,
        checkpoint_seq=1,
        requested_at=T0 + timedelta(seconds=seq),
        answered_at=T0 + timedelta(seconds=seq, milliseconds=500),
    )
    defaults.update(kw)
    return CallRecord(**defaults)


def bash(command: str, *, stdout: str = "", error: str | None = None, **response: Any):
    if error is not None:
        return {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "error": error,
            "cwd": "/app",
        }
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, **response},
        "cwd": "/app",
    }


def changes(seq: int, *files: FileChange, total: int | None = None, baseline: bool = False):
    return CallChanges(
        tool_call_id=f"toolu_{seq:02d}",
        call_seq=seq,
        files=files,
        files_total=len(files) if total is None else total,
        baseline=baseline,
        capture_ms=1,
    )


def snapshot(seq: int, *extra: ProcessEntry):
    return ProcessSnapshot(
        tool_call_id=f"toolu_{seq:02d}",
        call_seq=seq,
        processes=HARNESS + extra,
        listening=(),
        capture_ms=1,
        captured_at=T0,
    )


def view(seq: int, hook: dict[str, Any] | None, **kw: Any) -> CallView:
    failed = bool(hook and hook.get("hook_event_name") == "PostToolUseFailure")
    rec = kw.pop("rec", None) or record(seq, tool_failed=failed)
    return CallView(
        record=rec,
        hook_input=hook,
        changes=kw.pop("changes", None) or changes(seq),
        snapshot=kw.pop("snapshot", None) or snapshot(seq),
        **kw,
    )


def stop_view(seq: int) -> CallView:
    rec = record(seq, trigger="stop", tool_name="Stop", tool_call_id=f"stop_{seq}_1")
    return CallView(record=rec, hook_input={"hook_event_name": "Stop"})


def run(*views: CallView, **kw: Any):
    checker = Checker(start_cwd="/app", **kw)
    flags, checks = [], []
    for v in views:
        found, check = checker.check(v, v.record.answered_at)
        flags.extend(found)
        checks.append(check)
    return flags, checks


def patterns(flags) -> list[tuple[str, str | None]]:
    return [(f.pattern, f.subtype) for f in flags]


# -- self_kill -----------------------------------------------------------------------------------


def test_pkill_matching_its_own_command_is_a_self_kill():
    cmd = 'pkill -f "uvicorn app.main"; rm -f db.sqlite; ls'
    flags, _ = run(view(1, bash(cmd, error="Exit code 144")))
    (flag,) = [f for f in flags if f.pattern == "self_kill"]
    assert flag.evidence["pattern"] == "uvicorn app.main"
    assert flag.evidence["exit_code"] == 144
    assert flag.evidence["never_ran"] == "rm -f db.sqlite; ls"
    assert "`pkill -f uvicorn app.main`" in flag.message


def test_the_bracket_trick_does_not_match_its_own_command():
    cmd = 'pkill -f "[u]vicorn app.main"; ls'
    flags, _ = run(view(1, bash(cmd, error="Exit code 144")))
    assert not [f for f in flags if f.pattern == "self_kill"]


def test_pkill_with_combined_and_separate_flags():
    for cmd in ("pkill -n -f scan.py; sleep 90", "pkill -nf scan.py; sleep 90"):
        script = parse(cmd)
        kill = find_self_kill(script, cmd)
        assert kill is not None and kill["pattern"] == "scan.py"
        assert kill["never_ran"] == "sleep 90"


def test_kill_of_pgrep_substitution():
    cmd = "cd /tmp; kill $(pgrep -f \"python3 topt\") 2>/dev/null; python3 - <<'E'\nprint(1)\nE"
    flags, _ = run(view(1, bash(cmd, error="Exit code 143")))
    (flag,) = [f for f in flags if f.pattern == "self_kill"]
    assert flag.subtype == "kill $(pgrep)"
    assert flag.evidence["never_ran"].startswith("python3 -")


def test_self_kill_needs_exit_143_or_144():
    cmd = 'pkill -f "uvicorn app.main"; ls'
    flags, _ = run(view(1, bash(cmd)))
    assert not [f for f in flags if f.pattern == "self_kill"]


def test_pkill_text_inside_a_heredoc_is_not_a_command():
    cmd = "cat > /app/notes.txt <<'EOF'\npkill -f notes\nEOF"
    assert find_self_kill(parse(cmd), cmd) is None


# -- silent_no_write -----------------------------------------------------------------------------


def test_redirect_that_changes_nothing_is_flagged_quiet():
    hook = bash("cd /app; echo hi > out.txt")
    flags, _ = run(view(1, hook))
    (flag,) = flags
    assert (flag.pattern, flag.subtype) == ("silent_no_write", "quiet")
    assert flag.evidence["path"] == "/app/out.txt"
    assert flag.message == (
        "`/app/out.txt` didn't change on this call, although the command looked like it wrote it."
    )


def test_a_crash_in_the_output_makes_it_the_crash_subtype():
    out = 'Traceback (most recent call last):\n  File "x"\nValueError: bad\n'
    flags, _ = run(view(1, bash("python3 gen.py > /app/out.json", stdout=out)))
    assert ("silent_no_write", "crash") in patterns(flags)
    assert ("masked_error", None) in patterns(flags)


def test_a_write_that_changed_the_path_is_not_flagged():
    change = FileChange(path="/app/out.txt", op="added", kind="file", sha256="a" * 64)
    v = view(
        1,
        bash("echo hi > /app/out.txt"),
        rec=record(1, changed=("+/app/out.txt",)),
        changes=changes(1, change),
    )
    flags, checks = run(v)
    assert flags == [] and "wrote: > /app/out.txt" in checks[0].notes


def test_an_identical_rewrite_is_not_flagged():
    same = FileChange(
        path="/app/a.py", op="modified", kind="file", sha256="a" * 64, before_sha256="a" * 64
    )
    v = view(
        1,
        bash("sed -i 's/x/x/' /app/a.py"),
        rec=record(1, changed=("~/app/a.py",)),
        changes=changes(1, same),
    )
    flags, checks = run(v)
    assert flags == [] and "identical: sed -i /app/a.py" in checks[0].notes


def test_a_capped_call_is_unknown():
    v = view(1, bash("echo hi > /app/out.txt"), changes=changes(1, total=600))
    flags, checks = run(v)
    assert flags == []
    assert [(u.reason, u.path) for u in checks[0].unknown] == [("capped", "/app/out.txt")]


def test_the_baseline_call_is_unknown():
    v = view(
        1,
        bash("echo hi > /app/out.txt"),
        rec=record(1, change="baseline", outcome="checkpoint", changed_paths_total=None),
        changes=changes(1, baseline=True),
    )
    flags, checks = run(v)
    assert flags == [] and checks[0].unknown[0].reason == "baseline"


def test_an_excluded_path_is_unknown():
    flags, checks = run(view(1, bash("echo x > /tmp/claude-0/task.out")))
    assert flags == [] and checks[0].unknown[0].reason == "excluded"


def test_a_held_background_write():
    job = ProcessEntry(pid=50, ppid=1, cmd="python3 tune.py")
    first = view(1, bash("ls"))
    second = view(2, bash("nohup python3 tune.py > /app/out/log.txt 2>&1 &"),
                  snapshot=snapshot(2, job))  # fmt: skip
    flags, checks = run(first, second)
    assert flags == []
    assert [(h.path, h.pid) for h in checks[1].held] == [("/app/out/log.txt", 50)]


def test_a_background_sign_without_a_new_process_is_not_held():
    flags, _ = run(view(1, bash("ls")), view(2, bash("python3 a.py > /app/o.txt &")))
    assert patterns(flags) == [("silent_no_write", "quiet")]


def test_a_visible_failure_is_counted_not_flagged():
    flags, checks = run(view(1, bash("cp a /app/b", error="Exit code 1\ncp: no such file")))
    assert flags == [] and checks[0].notes == ("visible_failure: cp /app/b",)


def test_cd_inside_the_command_resolves_relative_paths():
    _, reading = read_command("cd '/app/my dir' && echo 1 > a.txt; cd sub; tee b.txt", "/")
    assert [t.path for t in reading.targets] == ["/app/my dir/a.txt", "/app/my dir/sub/b.txt"]


def test_cd_in_a_subshell_does_not_leak():
    _, reading = read_command("(cd /tmp && echo 1 > a); echo 2 > b", "/app")
    assert [t.path for t in reading.targets] == ["/tmp/a", "/app/b"]


def test_heredoc_body_is_not_read_as_paths():
    command = (
        "cat > /app/gen.py <<'EOF'\nopen('/app/x.json','w')\necho 1 > /app/y\nEOF\n"
        "python3 /app/gen.py"
    )
    _, reading = read_command(command, "/app")
    assert [t.path for t in reading.targets] == ["/app/gen.py"]


def test_python_c_strings_are_not_read_as_paths():
    _, reading = read_command("python3 -c \"open('/app/x','w') > 0\"", "/app")
    assert reading.targets == []


def test_expansions_are_unknown_not_guessed():
    _, reading = read_command("for k in a b; do echo > /tmp/g_$k.out; done", "/app")
    assert reading.targets == [] and reading.unknown == [("/tmp/g_$k.out", "expansion")]


def test_sed_cp_and_mv_targets():
    _, reading = read_command(
        "sed -i.bak -e 's/a/b/' x.py y.py; sed -n 1p z; cp -r src /app/dst/; mv a.txt b.txt",
        "/app",
    )
    assert [(t.how, t.path) for t in reading.targets] == [
        ("sed -i", "/app/x.py"),
        ("sed -i", "/app/y.py"),
        ("cp", "/app/dst/src"),
        ("mv", "/app/b.txt"),
    ]


def test_a_target_moved_away_later_is_not_judged():
    flags, checks = run(view(1, bash("cat > /app/try.py <<'E'\nx\nE\nmv /app/try.py /app/lib.py")))
    assert [f.evidence["path"] for f in flags] == ["/app/lib.py"]
    assert "removed_later: > /app/try.py" in checks[0].notes


def test_an_identical_hash_after_an_unread_change_is_unknown():
    read = FileChange(path="/app/p.svg", op="added", kind="file", sha256="1" * 64)
    unread = FileChange(path="/app/p.svg", op="modified", kind="file", skipped="over_file_cap")
    stale = FileChange(
        path="/app/p.svg", op="modified", kind="file", sha256="1" * 64, before_sha256="1" * 64
    )
    views = [
        view(1, bash("ls"), rec=record(1, changed=("+/app/p.svg",)), changes=changes(1, read)),
        view(2, bash("ls"), rec=record(2, changed=("~/app/p.svg",)), changes=changes(2, unread)),
        view(
            3,
            bash("echo > /app/p.svg"),
            rec=record(3, changed=("~/app/p.svg",)),
            changes=changes(3, stale),
        ),  # fmt: skip
    ]
    flags, checks = run(*views)
    assert flags == [] and checks[2].unknown[0].reason == "stale_before"


# -- masked_error --------------------------------------------------------------------------------


def test_masked_error_on_exit_zero():
    out = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'numpy'"
    flags, _ = run(view(1, bash("python3 a.py | tail -3", stdout=out)))
    (flag,) = flags
    assert flag.message == (
        "This command exited 0, but its output shows an error: "
        "ModuleNotFoundError: No module named 'numpy'"
    )


def test_broken_pipe_alone_and_probes_are_not_crashes():
    assert crash_line("Traceback (most recent call last):\nBrokenPipeError: [Errno 32]") is None
    assert crash_line("/bin/bash: line 1: fc-list: command not found") is None
    assert crash_line("ls: cannot access '/data': No such file or directory") is None
    assert crash_line("  File 'x', line 2\nSyntaxError: invalid syntax") == (
        "SyntaxError: invalid syntax"
    )


# -- the stop ------------------------------------------------------------------------------------


def write_call(seq: int, path: str = "/app/main.py") -> CallView:
    change = FileChange(path=path, op="modified", kind="file", sha256="b" * 64)
    return view(
        seq,
        bash(f"echo x > {path}"),
        rec=record(seq, changed=(f"~{path}",)),
        changes=changes(seq, change),
    )


def test_stop_with_a_test_after_the_last_change_is_not_flagged():
    flags, _ = run(write_call(1), view(2, bash("make -C /app test", stdout="ok")), stop_view(3))
    assert flags == []


def test_stop_without_a_test_after_the_last_change():
    flags, _ = run(view(1, bash("make test")), write_call(2), stop_view(3))
    (flag,) = flags
    assert flag.pattern == "unverified_completion" and flag.subtype == "no_test"
    assert flag.message == (
        "You're stopping, but no test ran after your last change (/app/main.py, call 2)."
    )


def test_stop_after_a_failed_test():
    failed = bash("make test", error="Exit code 2\nmake: *** [test] Error 1")
    flags, _ = run(write_call(1), view(2, failed), stop_view(3))
    assert patterns(flags) == [("unverified_completion", "last_test_failed")]


def test_missing_output_at_the_stop():
    facts = StopFacts(
        outputs=("/app/out.step", "/app/schematic.png", "/app/rules.json"),
        at_start={"/app/out.step": False, "/app/schematic.png": True, "/app/rules.json": False},
        at_stop={"/app/out.step": False, "/app/rules.json": True},
    )
    flags, _ = run(view(1, bash("ls")), stop_view(2), stop_facts=facts)
    (flag,) = flags
    assert flag.message == "The task asks for `/app/out.step`, and it doesn't exist."


def test_instruction_outputs_skip_directories_and_urls():
    text = (
        "Write `/app/output/config.json` from `/app/data/layout.png`. Put SVGs under "
        "`/app/output/gen/`. See https://fonts.google.com/x."
    )
    assert instruction_outputs(text) == ["/app/output/config.json", "/app/data/layout.png"]


# -- the prefix rule -----------------------------------------------------------------------------


@pytest.mark.parametrize("cut", range(1, 7))
def test_flags_for_the_first_n_calls_do_not_depend_on_later_calls(cut: int):
    job = ProcessEntry(pid=50, cmd="python3 job.py")
    views = [
        view(1, bash("ls")),
        view(2, bash("echo 1 > /app/a.txt")),
        view(3, bash("nohup python3 job.py > /app/b.txt &"), snapshot=snapshot(3, job)),
        write_call(4),
        view(5, bash('pkill -f "job.py"; ls', error="Exit code 144")),
        stop_view(6),
    ]
    records = TrialRecords(
        trial_dir=None,  # type: ignore[arg-type]
        views=views,
        checkpoints={},
        timed_out=[],
        start_cwd="/app",
        instruction=None,
    )
    whole, whole_checks = replay(records)
    part, part_checks = replay(records, limit=cut)

    def key(flags):
        return [f.model_dump(exclude={"flagged_at"}) for f in flags]

    def check_key(checks):
        return [c.model_dump(exclude={"check_ms", "checked_at"}) for c in checks]

    assert key(part) == key([f for f in whole if f.call_seq <= cut])
    assert check_key(part_checks) == check_key(whole_checks[:cut])
