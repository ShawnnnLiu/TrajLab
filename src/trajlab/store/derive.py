"""The Exp 2 store's derived record: SQL views over its tables (ADR-0018, build-order step 3).

What a rule says about an attempt is a view in `record.duckdb`, beside the rows it is computed
from, so an agent's query reads it like a table, the `record-files` export writes it as one more
file (`trajlab.store.export`), and a retry built into a chain's store is covered at once, with
nothing recomputed or appended. Every view is a function of one trial's rows, so a trial's
derived values do not depend on which other trials share its store.

The views carry ADR-0016's three layers (item 5):

- `step_flags`, behavior flags per step row: a call's change, error marker, tail probe, idle
  wait, test command, and `files_touched` (the technical spec's count of delta rows);
- `delta_flags`, flags per delta row: cache files, paths the instruction names, the row's version
  of its path, reverts, and scripts added and the earlier script each is a variant of;
- `path_versions`, per changed path: its versions, the steps that wrote them, and its last hunk
  (with `names_prompt_path`, the specification views);
- `trial_summary`, occurrence views per trial: counts, the first error step, the last mutating
  step, and `unverified_completion`.

The predicates and thresholds below are candidates until the preregistration freezes them
(ADR-0016, item 5). Changing any of them changes `derived_id()`; a store built under another id
is refused by `create_store` until `trajlab store derive` replaces its views.
"""

import hashlib
from dataclasses import dataclass

from trajlab.contracts import DerivedSpec

DERIVE_VERSION = 1

# Files a tool writes for itself, not the agent's work: Python's bytecode and pytest's cache.
CACHE_PATH = r"/__pycache__/|\.pyc$|/\.pytest_cache/"
# A program the agent wrote to run, by its extension.
SCRIPT_PATH = r"\.(?:py|sh|bash|js|mjs|ts|rb|pl|r|jl)$"
# An absolute path in the task instruction. Its last character is not a period, so a path that
# ends a sentence ("... to /app/plan.json.") is still the path; a trailing slash is dropped, and a
# match starting `//` (the rest of a URL) is not a path.
PROMPT_PATH = r"/[A-Za-z0-9_./-]*[A-Za-z0-9_/-]"
# A tool result that shows a failure: a non-zero exit (Claude Code's `Exit code N`, Harbor's
# `[exit_code] N`), a Python traceback, a failing test line, or an `...Error:` line.
ERROR_MARKER = (
    r"(?m)^Exit code -?[1-9][0-9]*|^\[exit_code\] -?[1-9][0-9]*"
    r"|Traceback \(most recent call last\)"
    r"|^FAILED\b|^FAIL:|\b[1-9][0-9]* failed\b|test result: FAILED"
    r"|^[A-Za-z]*Error:"
)
# A Bash command that runs tests: a test runner in command position (at the start, or after a
# shell separator, `then`, or `do`; past variable assignments, `timeout N`, `time`, `env`, and a
# launcher such as `python -m` or `uv run`). The trace site's hint (trajlab.capture.trace,
# ADR-0015) matches the same runners anywhere in the command, so `pip install pytest` and
# `cat pytest.ini` count there and not here.
TEST_COMMAND = (
    r"(?:^|[;&|(\n]|\bthen\b|\bdo\b)\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:(?:timeout\s+\S+|time|env|sudo)\s+)*"
    r"(?:(?:python3?|uv\s+run|poetry\s+run|pipenv\s+run|npx|bunx)\s+(?:-m\s+)?)?"
    r"(?:pytest|py\.test|unittest|tox|nox|ctest|jest|vitest|mocha"
    r"|(?:npm|yarn|pnpm|bun)(?:\s+run)?\s+test|(?:cargo|go)\s+test"
    r"|make(?:\s+-C\s+\S+)?\s+(?:test|check)"
    r"|(?:(?:ba)?sh\s+)?\S*(?:test\.sh|run_tests))\b"
)
# A message that claims success: the trace site's success-claim pattern (a test keeps the two
# equal), unless the message also says something failed or was not done.
SUCCESS_CLAIM = (
    r"(?i)\b(?:all (?:\d+ )?tests?|tests? (?:now )?(?:pass|passes|passed|passing)|verified"
    r"|works?|working|succeeded|successfully|fixed|done|complete[ds]?)\b"
)
NOT_DONE = (
    r"(?i)\b(?:not|cannot|could ?n.t|could not|did ?n.t|was ?n.t|is ?n.t|unable"
    r"|fail(?:ed|s|ing|ure)?|incomplete|unfinished|unresolved|unfortunately)\b"
)
# A call that ran this long and changed nothing waited on something else.
IDLE_SECONDS = 120
# A script added is a variant of an earlier one when at least this share of its distinct
# non-blank lines are lines of the earlier script, and it has at least this many.
VARIANT_OVERLAP = 0.5
VARIANT_MIN_LINES = 5


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


CACHE, SCRIPT, PROMPT, ERROR, TEST, SUCCESS, NOT_DONE_SQL = (
    _literal(pattern)
    for pattern in (
        CACHE_PATH,
        SCRIPT_PATH,
        PROMPT_PATH,
        ERROR_MARKER,
        TEST_COMMAND,
        SUCCESS_CLAIM,
        NOT_DONE,
    )
)

# Scripts the agent added, each with its distinct non-blank lines as first written (the `+`
# lines of an add's hunk), and the earlier script each is a variant of: the one sharing the
# most of its lines, the earliest on a tie.
_SCRIPTS = f"""
    script_adds AS (
        SELECT trial_id, step_idx, path,
               list_distinct(list_filter(list_transform(list_filter(
                   string_split(hunk, chr(10)),
                   line -> starts_with(line, '+') AND NOT starts_with(line, '+++')),
                   line -> trim(line[2:])), line -> line <> '')) AS lines
        FROM deltas
        WHERE op = 'add' AND regexp_matches(path, {SCRIPT}) AND NOT regexp_matches(path, {CACHE})
    ),
    variants AS (
        SELECT n.trial_id, n.step_idx, n.path, e.path AS variant_of
        FROM script_adds n
        JOIN script_adds e ON e.trial_id = n.trial_id AND e.step_idx < n.step_idx
        WHERE len(n.lines) >= {VARIANT_MIN_LINES}
          AND len(list_intersect(n.lines, e.lines)) >= {VARIANT_OVERLAP} * len(n.lines)
        QUALIFY row_number() OVER (
            PARTITION BY n.trial_id, n.step_idx, n.path
            ORDER BY len(list_intersect(n.lines, e.lines)) DESC, e.step_idx, e.path
        ) = 1
    )"""

STEP_FLAGS = f"""
    WITH changed AS (
        SELECT trial_id, step_idx, count(*) AS files_touched,
               count(*) FILTER (WHERE NOT regexp_matches(path, {CACHE})) AS agent_paths
        FROM deltas
        GROUP BY trial_id, step_idx
    ),
    calls AS (
        SELECT
            s.trial_id,
            s.step_idx,
            coalesce(s.tool NOT IN ('Stop', 'StopFailure'), false) AS is_tool_call,
            coalesce(c.files_touched, 0) AS files_touched,
            coalesce(c.agent_paths, 0) > 0 AS changed,
            coalesce(s.is_error, false)
                OR regexp_matches(coalesce(s.output, ''), {ERROR}) AS error_marker,
            coalesce(epoch(s.t_end - s.t_start) > {IDLE_SECONDS}, false) AS long_running,
            coalesce(s.tool = 'Bash' AND regexp_matches(
                coalesce(json_extract_string(s.args_json, '$.command'), ''), {TEST}
            ), false) AS test_like
        FROM steps s
        LEFT JOIN changed c ON c.trial_id = s.trial_id AND c.step_idx = s.step_idx
    ),
    last_change AS (
        SELECT trial_id, max(step_idx) FILTER (WHERE is_tool_call AND changed) AS step_idx
        FROM calls
        GROUP BY trial_id
    )
    SELECT
        c.trial_id,
        c.step_idx,
        c.is_tool_call,
        c.files_touched,
        c.is_tool_call AND c.changed AS is_mutating,
        c.is_tool_call AND c.error_marker AS has_error_marker,
        c.is_tool_call AND NOT c.changed AND c.step_idx > coalesce(l.step_idx, 0)
            AS is_tail_probe,
        c.is_tool_call AND NOT c.changed AND c.long_running AS is_idle_wait,
        c.is_tool_call AND c.test_like AS is_test_command
    FROM calls c
    JOIN last_change l ON l.trial_id = c.trial_id"""

DELTA_FLAGS = f"""
    WITH named AS (
        SELECT trial_id, list_distinct(list_transform(
                   list_filter(regexp_extract_all(prompt, {PROMPT}), p -> NOT starts_with(p, '//')),
                   p -> rtrim(p, '/'))) AS paths
        FROM trials
    ),
    {_SCRIPTS}
    SELECT
        d.trial_id,
        d.step_idx,
        d.path,
        regexp_matches(d.path, {CACHE}) AS is_cache,
        coalesce(list_contains(n.paths, d.path), false) AS names_prompt_path,
        row_number() OVER (PARTITION BY d.trial_id, d.path ORDER BY d.step_idx) AS version,
        d.sha256_after IS NOT NULL AND d.sha256_before IS DISTINCT FROM d.sha256_after AND EXISTS (
            SELECT 1 FROM deltas e
            WHERE e.trial_id = d.trial_id AND e.path = d.path AND e.step_idx < d.step_idx
              AND e.sha256_before = d.sha256_after
              AND e.sha256_after IS DISTINCT FROM e.sha256_before
        ) AS is_revert,
        a.path IS NOT NULL AS is_script_add,
        v.variant_of
    FROM deltas d
    LEFT JOIN named n ON n.trial_id = d.trial_id
    LEFT JOIN script_adds a
        ON a.trial_id = d.trial_id AND a.step_idx = d.step_idx AND a.path = d.path
    LEFT JOIN variants v
        ON v.trial_id = d.trial_id AND v.step_idx = d.step_idx AND v.path = d.path"""

PATH_VERSIONS = """
    SELECT
        d.trial_id,
        d.path,
        count(*) AS versions,
        list(d.step_idx ORDER BY d.step_idx) AS steps,
        min(d.step_idx) AS first_step,
        max(d.step_idx) AS last_step,
        (list(d.op ORDER BY d.step_idx DESC))[1] AS last_op,
        (list(d.hunk ORDER BY d.step_idx DESC))[1] AS last_hunk,
        bool_or(f.is_cache) AS is_cache,
        bool_or(f.names_prompt_path) AS names_prompt_path
    FROM deltas d
    JOIN delta_flags f ON f.trial_id = d.trial_id AND f.step_idx = d.step_idx AND f.path = d.path
    GROUP BY d.trial_id, d.path"""

TRIAL_SUMMARY = f"""
    WITH calls AS (
        SELECT
            trial_id,
            count(*) FILTER (WHERE is_tool_call) AS tool_calls,
            count(*) FILTER (WHERE is_mutating) AS mutating_calls,
            min(step_idx) FILTER (WHERE has_error_marker) AS first_error_step,
            max(step_idx) FILTER (WHERE is_mutating) AS last_mutating_step,
            count(*) FILTER (WHERE is_tail_probe) AS tail_probes,
            count(*) FILTER (WHERE is_idle_wait) AS idle_waits,
            max(step_idx) FILTER (WHERE is_test_command) AS last_test_step,
            arg_max(has_error_marker, step_idx) FILTER (WHERE is_test_command)
                AS last_test_failed
        FROM step_flags
        GROUP BY trial_id
    ),
    paths AS (
        SELECT
            trial_id,
            count(DISTINCT path) FILTER (WHERE is_script_add) AS scripts_added,
            count(*) FILTER (WHERE variant_of IS NOT NULL) AS script_variants,
            count(*) FILTER (WHERE names_prompt_path) AS prompt_path_versions
        FROM delta_flags
        GROUP BY trial_id
    ),
    final AS (
        SELECT trial_id, arg_max(message, step_idx) AS message
        FROM steps
        WHERE source = 'agent' AND trim(coalesce(message, '')) <> ''
        GROUP BY trial_id
    )
    SELECT
        t.trial_id,
        coalesce(c.tool_calls, 0) AS tool_calls,
        coalesce(c.mutating_calls, 0) AS mutating_calls,
        c.first_error_step,
        c.last_mutating_step,
        coalesce(c.tail_probes, 0) AS tail_probes,
        coalesce(c.idle_waits, 0) AS idle_waits,
        coalesce(p.scripts_added, 0) AS scripts_added,
        coalesce(p.script_variants, 0) AS script_variants,
        coalesce(p.prompt_path_versions, 0) AS prompt_path_versions,
        coalesce(
            regexp_matches(f.message, {SUCCESS}) AND NOT regexp_matches(f.message, {NOT_DONE_SQL}),
            false
        ) AND (
            c.last_test_step IS NULL
            OR c.last_test_step < coalesce(c.last_mutating_step, 0)
            OR c.last_test_failed
        ) AS unverified_completion
    FROM trials t
    LEFT JOIN calls c ON c.trial_id = t.trial_id
    LEFT JOIN paths p ON p.trial_id = t.trial_id
    LEFT JOIN final f ON f.trial_id = t.trial_id"""


@dataclass(frozen=True)
class View:
    """A derived view: what each row is, its columns in order, and the SQL that computes it."""

    name: str
    description: str
    columns: tuple[DerivedSpec, ...]
    sql: str

    def ddl(self) -> str:
        return f"CREATE VIEW {self.name} AS {self.sql}"


def _columns(view: str, *columns: tuple[str, str]) -> tuple[DerivedSpec, ...]:
    return tuple(DerivedSpec(view=view, name=n, description=d) for n, d in columns)


_TRIAL = ("trial_id", "The attempt (trials.trial_id).")
_STEP = ("step_idx", "The step row (steps.step_idx).")
_PATH = ("path", "The changed path (deltas.path).")

# In creation order: a view reads only tables and the views before it.
VIEWS: tuple[View, ...] = (
    View(
        "step_flags",
        "One row per step row: what the step did, by rules over its row and its delta rows.",
        _columns(
            "step_flags",
            _TRIAL,
            _STEP,
            ("is_tool_call", "The row is a tool call, not a message or a stop record."),
            ("files_touched", "Its delta rows."),
            ("is_mutating", "A tool call with a delta row outside cache files (is_cache)."),
            (
                "has_error_marker",
                "A tool call flagged as an error, or whose output shows a non-zero exit, a "
                "traceback, a failing test line, or an `...Error:` line.",
            ),
            ("is_tail_probe", "A tool call that is not mutating, after the attempt's last one."),
            (
                "is_idle_wait",
                f"A tool call that is not mutating and ran over {IDLE_SECONDS} s.",
            ),
            ("is_test_command", "A Bash call whose command runs a test runner."),
        ),
        STEP_FLAGS,
    ),
    View(
        "delta_flags",
        "One row per delta row: what the changed path is, by rules over the attempt's rows.",
        _columns(
            "delta_flags",
            _TRIAL,
            _STEP,
            _PATH,
            ("is_cache", "A file a tool wrote for itself: Python bytecode or pytest's cache."),
            ("names_prompt_path", "The path appears in the attempt's instruction (trials.prompt)."),
            ("version", "This row's position among the attempt's delta rows of the path, from 1."),
            (
                "is_revert",
                "The row changes the content back to what an earlier delta row of the path "
                "replaced.",
            ),
            ("is_script_add", "The path is a script (by extension) that this row adds."),
            (
                "variant_of",
                "For a script added, an earlier script it mostly repeats: at least "
                f"{VARIANT_OVERLAP:.0%} of its distinct lines (and at least {VARIANT_MIN_LINES}) "
                "are that script's lines as first written.",
            ),
        ),
        DELTA_FLAGS,
    ),
    View(
        "path_versions",
        "One row per path an attempt changed: its versions in step order.",
        _columns(
            "path_versions",
            _TRIAL,
            _PATH,
            ("versions", "Delta rows of the path."),
            ("steps", "Their step_idx values, in order."),
            ("first_step", "The first of them."),
            ("last_step", "The last of them."),
            ("last_op", "The last delta row's op."),
            ("last_hunk", "The last delta row's hunk."),
            ("is_cache", "As delta_flags.is_cache."),
            ("names_prompt_path", "As delta_flags.names_prompt_path."),
        ),
        PATH_VERSIONS,
    ),
    View(
        "trial_summary",
        "One row per attempt: counts and positions over its step_flags and delta_flags.",
        _columns(
            "trial_summary",
            _TRIAL,
            ("tool_calls", "Rows with is_tool_call."),
            ("mutating_calls", "Rows with is_mutating."),
            ("first_error_step", "The first step_idx with has_error_marker."),
            ("last_mutating_step", "The last step_idx with is_mutating."),
            ("tail_probes", "Rows with is_tail_probe."),
            ("idle_waits", "Rows with is_idle_wait."),
            ("scripts_added", "Distinct paths with is_script_add."),
            ("script_variants", "Delta rows with a variant_of."),
            ("prompt_path_versions", "Delta rows with names_prompt_path."),
            (
                "unverified_completion",
                "The attempt's last message claims success and says nothing failed or was not "
                "done, and no test command ran at or after its last mutating step, or the last "
                "test command has an error marker.",
            ),
        ),
        TRIAL_SUMMARY,
    ),
)


def derived_id() -> str:
    """The derived views' version and the sha256 of their SQL: what `store_meta` records."""
    sql = "\n".join(view.ddl() for view in VIEWS)
    return f"{DERIVE_VERSION}+{hashlib.sha256(sql.encode()).hexdigest()[:12]}"
