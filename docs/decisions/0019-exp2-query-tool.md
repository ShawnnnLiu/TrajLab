# ADR-0019: The Exp 2 query tool: a file channel through the bind mount, served per attempt

Status: proposed (2026-10-26).

## Context

Build-order step 4 of the technical spec (§7) is the query tool end to end in a Harbor container: a `record-engine` attempt issues a query, the host answers it, the call is logged, and a statement that is not a SELECT is rejected.
The spec's guarantees (§4.1) are read-only access, `checks` unreachable, a row cap of 200 and a statement timeout of 10 s, a `QueryLogEntry` per call in the trial dir, and the schema given once in the prompt.
Its §8 question 3, which ADR-0014 carries as open question 2, leaves three things to an ADR: the mechanism (a file channel through the existing watcher, or an MCP server), the result format, and whether the cap truncates or errors.

What was read and probed for this ADR, all on 2026-10-26 against the pinned DuckDB 1.5.6 and Harbor 0.23.0:

- Harbor bind-mounts a trial's `agent/` dir at `/logs/agent` and creates that dir in the trial's constructor (`harbor/trial/trial.py`, `self.paths.mkdir()`), before the container starts; task images guarantee neither Python nor `jq` nor network (`docs/harbor-facts.md`, spec §4.2).
- `connect_agent` (ADR-0014 item 2, ADR-0018 item 4) opens `record.duckdb` read-only with `enable_external_access` off and the configuration locked, and refuses a store whose derived views are stale; its docstring records that `enable_logging()` runs past the lock.
- DuckDB's parser (`duckdb.extract_statements`) classifies `PRAGMA version`, `DESCRIBE`, `SHOW`, `SUMMARIZE`, `FROM steps`, `VALUES (1)`, and `(SELECT 1)` as `SELECT` statements, so the statement type alone does not keep PRAGMA out.
- Under `connect_agent`'s lock these still run: `PRAGMA enable_profiling` (which then prints profiling trees to stdout), `PRAGMA enable_verification`, `PRAGMA force_checkpoint`, `CALL enable_logging()`, `CALL checkpoint()`, and `SET VARIABLE`.
  Their table-function spellings run from inside a SELECT: `enable_logging()`, `disable_logging()`, `truncate_duckdb_logs()`, `enable_profiling()`, `disable_profiling()`, `checkpoint()`, `force_checkpoint()`.
  `query('...')` and `json_execute_serialized_sql(...)` run SQL given as a string.
  With external access off, `read_blob`, `read_text`, `glob`, `read_csv`, `sniff_csv`, a quoted file name, `COPY`, and `INSTALL` fail with `file system operations are disabled`; `ATTACH` fails in read-only mode; `getenv` and `sleep` do not exist.
- `to_json` of a row struct gives byte for byte the text `COPY ... (FORMAT json)` writes, on every type the store uses: UUID, TIMESTAMPTZ, lists, structs, NULL, Infinity, NaN, HUGEINT, BLOB, DATE, INTERVAL; a column name repeated in one row is suffixed `_1`.
- Fetching 201 rows of a 10^8-row range takes 2 ms (streaming); the same range under ORDER BY takes 2.8 s.
- A DuckDB file open read-only in one process cannot be opened for writing by another, nor the reverse (`IO Error: Could not set lock on file`).
- A child Python process that imports DuckDB and the store's schema module starts in 0.13 s warm (0.55 s cold).
- The repo's hooks skill: the query tool is not a hook; keep it apart from the watcher so a slow query never delays an ack; log on the host.

## Decision

0. **Scope.** The `record-engine` arm's query tool: `trajlab.store.query` (the statement check and the runner), `trajlab.store.query_child` (the process a query runs in), `trajlab.store.serve` with `query.sh` (the host server and the container-side command), `QueryLogEntry` in `trajlab.contracts.store`, and the commands `trajlab store query` and `trajlab store serve`.
   It changes nothing in capture: the hook, the watcher, the backend, and postprocess are untouched, and the first-attempt corpus keeps its `corpus_id`.

1. **The channel is files through the bind mount, not an MCP server.**
   The agent runs `/logs/agent/bin/query "SELECT ..."`; the command writes `/logs/agent/queries/<id>.sql` and waits for `<id>.result` or `<id>.err` from the host.
   Task images guarantee neither Python nor network, and the bind mount is the channel the hook already proved (ADR-0006).
   An MCP server would need the container to reach the host over the network and a client entry in Claude Code's settings, and its calls would not be Bash steps in the trajectory; through the file channel every query is an ordinary Bash step whose arguments carry the SQL, so the `record-files` arm's file reads and the `record-engine` arm's queries are the same kind of trajectory event (spec §4.3).
   The command is POSIX `sh` (`src/trajlab/store/query.sh`), with the hook's atomic write and wait loop: the SQL, from the arguments or stdin, goes to `<id>.sql.tmp` and is renamed to `<id>.sql`; the id is `q_<epoch>_<pid>`; it polls every 0.1 s for `TRAJLAB_QUERY_WAIT` seconds (default 60); exit 0 with the rows on stdout, 1 with the error on stderr, 2 when no answer came.

2. **A separate host process serves it: `trajlab store serve STORE JOB_DIR`, one per `record-engine` attempt, started before that attempt's `harbor run`.**
   Not the watcher: `store/` may not import `checkpoint/` (CLAUDE.md constraints 3 and 6), changing the watcher is a capture change (constraint 5), and a 10 s query inside it would delay an ack the agent is waiting on.
   The server takes the job dir rather than the trial dir because Harbor names the trial dir with a random suffix when the trial starts; it sees the new trial dir on its next pass (0.1 s) and, since Harbor makes `agent/` before the container starts, the tool is in place before the agent can run.
   In each trial's `agent/` it writes `bin/query` (mode 755, written whole and renamed), makes `queries/` (mode 777, since the container's user is not the host's), and creates `query_log.jsonl` empty, so every `record-engine` attempt has a query log even when it never queried (spec §9).
   A SessionStart hook that writes the script from the settings file, the way the hook script is delivered inline, was considered and not chosen: the server must exist anyway, writing one file is the smaller mechanism, and it keeps a second settings file and that event's behavior under `--print` out of the experiment.
   A retry captured with the checkpoint hook runs the watcher and the server side by side on different file patterns.

3. **The answer is rows as JSON lines in DuckDB's own encoding, then a count line.**
   `<id>.result` holds one JSON object per row, `to_json` of the row struct, which is the function `COPY ... (FORMAT json)` applies in `trajlab store export`, so both record arms see one spelling of every value; instants are UTC.
   The last line is `-- N rows` (`-- 1 row`), or when the cap cut the answer, `-- 200 rows shown; the query has more. Add a LIMIT, or narrow it.`.
   The agent therefore always sees a line, an empty result included, and cannot take a cut answer for a whole one.

4. **The cap truncates with the marker; it is not an error.**
   The default is 200 rows; the child fetches the cap plus one row and the host sends the cap.
   Fetching streams, so a query over a long table costs the cap, not the table; a sort over such a table is the timeout's case.
   An error would make `SELECT * FROM deltas` useless and teach nothing; the marker says what to do.

5. **Every query runs in a disposable child process under a timeout, a memory limit, and a thread limit; the server never opens the store.**
   `trajlab.store.query.run_query` starts `python -m trajlab.store.query_child STORE --cap N --memory-limit SIZE --threads N` with the SQL on stdin.
   The child opens the store with `connect_agent`, which now takes `memory_limit` (default `512MB`) and `threads` (default 2) into the instance's configuration before locking it, checks and runs the statement, and answers with one JSON object on stdout.
   The host kills the child at the timeout (default 10 s) and answers `stopped after 10 s; narrow the query`.
   The child is the only process that opens DuckDB in the serve path, so what runs past the lock can at worst break the child, which dies alone and whose state never outlives the query.

6. **The statement check is DuckDB's parser, a lexical rule, and a deny list; nothing runs before it passes.**
   `check_statement` requires: (a) `duckdb.extract_statements` parses exactly one statement; (b) its type is `SELECT` and, past leading whitespace and comments, the text starts with `SELECT` or `WITH`, which refuses `PRAGMA` (the spelling of the pragma functions that act past the lock) and the other forms the parser files under SELECT (`DESCRIBE`, `SHOW`, `SUMMARIZE`, `FROM`-first, `VALUES`, a parenthesized query); (c) none of the functions the statement calls, read by name from DuckDB's serialized syntax tree (`json_serialize_sql`) so that quoting and case cannot hide one, is in `DENIED_FUNCTIONS`.
   The denied functions are the table functions a SELECT can call that act on the instance or run SQL from a string: `enable_logging`, `disable_logging`, `truncate_duckdb_logs` (logging past the lock); `enable_profiling`, `disable_profiling` (profiling prints into the answer); `checkpoint`, `force_checkpoint` (write the file); `query`, `json_execute_serialized_sql` (run SQL given as a string, past this check).
   Every other internal table function of DuckDB 1.5.6 was read: the rest read catalog or instance state, or files, and files are closed by `enable_external_access` off.
   The audit is pinned to the release: a test fails on any other DuckDB version so the list is read again.
   A refusal's message starts with `refused: ` in the log and on the agent's stderr, so the log tells a refusal from a database error.

7. **`QueryLogEntry` is written by the host only, to the trial's `agent/query_log.jsonl`.**
   Its fields are `query_id` (the request's id), `trial_name` (the attempt: a running trial has no trial id, ADR-0006, and `trials.trial_name` matches it once the attempt is built into a store), `received_at`, `sql` as written, `rows` (null on error), `truncated`, `latency_ms` (from the request to its answer, the child's start included), and `error`.
   One entry per request, refusals and errors included.
   A request is answered once: a `.sql` beside a `.result` or `.err` is never run again, also after a server restart, and requests found unanswered at startup are answered then.

8. **Ingest and a running server do not share the file.**
   The server holds no connection between queries; a child holds a read-only one for the query's duration.
   DuckDB's file lock is exclusive across processes in both directions, so a `store build` into a chain store while a query runs fails with `IO Error: Could not set lock on file`, and a query during a build is answered with the same error.
   The retry loop stops an attempt's server before it ingests the attempt and starts the next attempt's server after; a build at any other moment finds the file free.
   Nothing is corrupted either way.

9. **What the agent is told stays step 5's** (the evidence sentence of ADR-0016 item 1; ADR-0018 item 6 is open).
   This ADR fixes only what that sentence must name: the command `/logs/agent/bin/query "SELECT ..."` and that it answers JSON lines.
   No prompt text is in this PR.

## Consequences

- A `record-engine` attempt's trial dir gains `agent/bin/query`, `agent/queries/` (every request and answer, kept as the raw record), and `agent/query_log.jsonl`; first-attempt trial dirs are unchanged, and no capture `corpus_id` changes.
  Step 5 records the cap, timeout, memory limit, and thread count in the Exp 2 manifest (spec §4.1).
- `connect_agent` gains two keyword arguments; the agent-visible store is unchanged, so `SCHEMA_VERSION` stays 2.
- The agent's queries are Bash steps of the attempt's trajectory, so arm 3's query log and arm 2's file-read log (spec §4.3) are derivable from ATIF alike, and the host's log holds the exact SQL and outcome of each.
- Each query costs one Python process (0.13 s warm) and up to 0.2 s of file polling; the latency numbers are in Validation.
- The lexical rule refuses `DESCRIBE`, `SHOW`, and `FROM`-first queries that are read-only; the prompt carries the schema, so the agent needs none of them, and a refusal costs one tool call.
  Relaxing it is an amendment here.
- The cap and the timeout are per query; a budget of queries per attempt, if wanted, is step 5's.
- Tests: `tests/test_store_query.py` (the check on 45 statement forms, the answer's encoding against the export's, order and cap, the timeout, the limits, the unreachable files, stale views, a missing store, the log entry) and `tests/test_store_serve.py` (the command under `/bin/sh` against a server on a temp job dir: answers, refusals, stdin, giving up without a host, startup replay, a trial dir appearing later, the command under SIGTERM).
  Docker checks are the dated script below.
- Glossary: query tool, query log, serve, refusal.

## Validation (2026-10-26)

- `make test`: 1133 passed, 2 skipped; `make lint` clean.
- `scripts/2026-10-26_query_tool_docker_check.py`, part 1 (no API), on the Mac (Docker Desktop 29.8, arm64): a store built from the fixture trial with `--no-images`, `trajlab store serve` in a thread, and `docker run -u 65534:65534 -v <agent dir>:/logs/agent -e CLAUDE_CONFIG_DIR=/logs/agent/sessions` on `ubuntu:24.04` (hello-world's base) and `alpine:3.20` (BusyBox sh), each running the command three times:

      uv run python scripts/2026-10-26_query_tool_docker_check.py

  On both images: `SELECT count(*) AS n FROM steps` printed `{"n":2}`, `-- 1 row`, exit 0; `DELETE FROM steps` printed `refused: only a SELECT or WITH query` on stderr, exit 1; `SELECT 1; SELECT 2` printed `refused: one statement at a time, not 2`, exit 1; `queries/` held three `.sql`, one `.result`, two `.err`; the log had three entries naming the trial dir, one with a row and two with errors; 145 to 197 ms per request, the container's user being `nobody` throughout.
- `scripts/2026-10-26_query_latency.py` on a copy of `corpus/stores/exp2-example-v2` (production-planning, 5 trials, 117 steps, 236 deltas) upgraded with `trajlab store derive`, 20 repeats, cap 200, each run through `run_query` with its own child process, on the Mac (macOS 15.1, arm64, Python 3.12, DuckDB 1.5.6):

      uv run python scripts/2026-10-26_query_latency.py <store copy> --repeat 20 --markdown

  Cold start (the first query of the run, child start included): 159 ms.

| Query | Rows | Cut | Median ms | p95 ms |
|---|---|---|---|---|
| `trials` | 5 | no | 160 | 173 |
| `steps` | 117 | no | 159 | 176 |
| `deltas` | 200 | yes | 159 | 189 |
| `step_flags` | 84 | no | 168 | 196 |
| `delta_flags` | 200 | yes | 173 | 225 |
| `path_versions` | 50 | no | 172 | 197 |
| `trial_summary` | 5 | no | 179 | 191 |
| `join` | 200 | yes | 160 | 186 |
| `like_hunks` | 0 | no | 159 | 185 |
| `last_hunks` | 20 | no | 175 | 209 |

  The child's start is nearly all of it: the store is 7 MB and every query here runs in a few ms once open.
  The same script runs on the `tb40-sonnet-v3` store with that one argument.
