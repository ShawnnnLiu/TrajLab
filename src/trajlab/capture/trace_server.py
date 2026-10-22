"""The `trajlab trace` site: a local web server for reading trials call by call and labeling
them (ADR-0015 §8).

It serves one page (`trace_site/`) and a small JSON API over a jobs dir:

    GET /api/meta                              the patterns and their definitions
    GET /api/jobs?labeler=                     the experiments (job dirs), with counts
    GET /api/jobs/<job>?labeler=               a job's trials, with the labeler's progress
    GET /api/trace/<job>/<trial>               one trial's trace (`trace.build_trace`)
    GET /api/transcript/<job>/<trial>          the whole run as plain text, nothing cut
    GET /api/raw/<job>/<trial>                 Harbor's agent/trajectory.json as it is
    GET /api/file/<job>/<trial>?path=&call=    a changed file as it was after a call (replay)
    GET /api/queue/<job>?labeler=              ADR-0015 §8's labeling queue, with progress
    GET /api/agreement/<job>?a=&b=             two labelers' kappa per pattern, disagreements
    GET /api/guide                             the labeling guide (Markdown), and its sections
    GET /api/labels/<job>/<trial>?labeler=     the labeler's saved labels, or null
    PUT /api/labels/<job>/<trial>?labeler=     save them: a `TrialLabels` document
    GET /api/export?format=csv|json&job=&trial=&labeler=   labels as a download

It reads the jobs dir and writes only label files. It listens on 127.0.0.1 by default; from a
laptop, reach it through `ssh -L`. Requests must name a local host, which keeps a DNS-rebinding
page from reading it, and a save must carry an `X-Trajlab` header, which another site's page
cannot send without a CORS approval this server never gives.
"""

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from harbor.models.trial.paths import TrialPaths
from pydantic import ValidationError

from trajlab.capture.labels import (
    PATTERN_HELP,
    LabelError,
    LabelStore,
    agreement,
    check_labeler,
    export_rows,
    guide_sections,
    labeling_queue,
    parse_labels,
    progress,
    rows_csv,
)
from trajlab.capture.replay import SnapshotReader, file_after
from trajlab.capture.trace import (
    TRAJECTORY_FILENAME,
    build_trace,
    find_jobs,
    full_transcript,
    job_summary,
    trial_dirs,
    trial_summary,
)
from trajlab.contracts import (
    CALLS_FILENAME,
    CHANGES_FILENAME,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    LABEL_PATTERNS,
    PROCESSES_FILENAME,
    TRAJLAB_DIRNAME,
    CallChanges,
)

logger = logging.getLogger(__name__)

SITE_DIR = "trace_site"
# The page's own scripts (ES modules) and styles: flat names in trace_site/.
_STATIC_NAME = re.compile(r"^[a-z0-9-]+\.(js|css|md)$")
_STATIC_TYPES = {"js": "text/javascript", "css": "text/css", "md": "text/markdown; charset=utf-8"}
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SAVE_HEADER = "x-trajlab"
MAX_BODY_BYTES = 8 * 1024 * 1024
CACHED_TRACES = 8
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()


def _json(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False, default=str).encode())


def _error(status: int, message: str) -> Response:
    return _json({"error": message}, status)


def _inline(body: bytes, media_type: str, filename: str) -> Response:
    """A file the browser shows in its own tab, and saves under `filename`."""
    disposition = (("Content-Disposition", f'inline; filename="{filename}"'),)
    return Response(200, body, f"{media_type}; charset=utf-8", disposition)


class _NotFound(Exception):
    pass


class TraceSite:
    """The site's routes as a plain function of a request, so tests need no sockets."""

    def __init__(
        self,
        root: Path,
        labels: LabelStore,
        extra_hosts: frozenset[str] = frozenset(),
        guide: Path | None = None,
        snapshots: SnapshotReader | None = None,
    ) -> None:
        self.root = root
        self.labels = labels
        self.hosts = LOCAL_HOSTS | extra_hosts
        self.guide = guide
        self.snapshots = snapshots or SnapshotReader()
        self._traces: OrderedDict[Path, tuple[tuple[Any, ...], dict[str, Any]]] = OrderedDict()
        self._diffs: OrderedDict[Path, tuple[Any, dict[str, dict[str, str]]]] = OrderedDict()
        self._lock = threading.Lock()

    def handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes = b""
    ) -> Response:
        """`headers` with lowercase names."""
        if not self._host_allowed(headers.get("host", "")):
            return _error(403, "this server answers only requests to localhost")
        url = urlsplit(target)
        parts = [unquote(p) for p in url.path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if method == "PUT" and parts[:1] == ["api"]:
                if headers.get(SAVE_HEADER) != "1":
                    return _error(403, f"a save needs the {SAVE_HEADER} header")
                return self._put(parts[1:], query, body)
            if method != "GET":
                return _error(405, f"{method} is not supported")
            if not parts:
                return self._static("index.html", "text/html; charset=utf-8")
            if parts == ["favicon.ico"]:
                return Response(204, b"", "text/plain")
            if parts[0] == "static" and len(parts) == 2 and _STATIC_NAME.match(parts[1]):
                return self._static(parts[1], _STATIC_TYPES[parts[1].rsplit(".", 1)[1]])
            if parts[0] == "api":
                return self._get(parts[1:], query)
        except _NotFound as error:
            return _error(404, str(error))
        except (LabelError, ValidationError) as error:
            return _error(400, str(error))
        return _error(404, "no such page")

    def _host_allowed(self, host: str) -> bool:
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host[1:].split("]")[0]
        return name in self.hosts

    def _static(self, name: str, content_type: str) -> Response:
        resource = files(__package__).joinpath(SITE_DIR, name)
        if not resource.is_file():
            raise _NotFound(f"no file {name}")
        body = resource.read_bytes()
        headers = (("Content-Security-Policy", _CSP),) if name == "index.html" else ()
        return Response(200, body, content_type, headers)

    def _jobs(self) -> dict[str, Path]:
        return find_jobs(self.root) if self.root.is_dir() else {}

    def _job(self, name: str) -> Path:
        if name == self.root.name and trial_dirs(self.root):
            return self.root
        job = self.root / name
        if "/" in name or name in ("", ".", "..") or not job.is_dir() or not trial_dirs(job):
            raise _NotFound(f"no experiment {name!r} under {self.root}")
        return job

    def _trial(self, job_name: str, trial_name: str) -> Path:
        for trial in trial_dirs(self._job(job_name)):
            if trial.name == trial_name:
                return trial
        raise _NotFound(f"no trial {trial_name!r} in {job_name!r}")

    def _get(self, parts: list[str], query: dict[str, str]) -> Response:
        labeler = query.get("labeler") or None
        if labeler is not None:
            check_labeler(labeler)
        match parts:
            case ["meta"]:
                return _json(
                    {
                        "root": str(self.root),
                        "labels_dir": str(self.labels.root),
                        "patterns": [
                            {
                                "key": key,
                                "title": PATTERN_HELP[key][0],
                                "help": PATTERN_HELP[key][1],
                            }
                            for key in LABEL_PATTERNS
                        ],
                    }
                )
            case ["jobs"]:
                return _json(
                    {
                        "root": str(self.root),
                        "jobs": [self._job_row(n, j, labeler) for n, j in self._jobs().items()],
                    }
                )
            case ["jobs", job_name]:
                job = self._job(job_name)
                rows = []
                for trial in trial_dirs(job):
                    row = trial_summary(trial)
                    labels = self.labels.load(job_name, labeler, trial.name) if labeler else None
                    row["progress"] = progress(labels) if labeler else None
                    rows.append(row)
                return _json(
                    {"job": job_name, "labelers": self.labels.labelers(job_name), "trials": rows}
                )
            case ["trace", job_name, trial_name]:
                return _json(self._trace(self._trial(job_name, trial_name)))
            case ["transcript", job_name, trial_name]:
                trial = self._trial(job_name, trial_name)
                events = self._trace(trial)["events"]
                numbers = {e["id"]: e["n"] for e in events if e["kind"] == "call"}
                try:
                    text = full_transcript(trial, numbers)
                except FileNotFoundError as error:
                    raise _NotFound(f"{trial_name} has no agent/trajectory.json yet") from error
                return _inline(text.encode(), "text/plain", f"{trial_name}.transcript.txt")
            case ["raw", job_name, trial_name]:
                path = TrialPaths(self._trial(job_name, trial_name)).agent_dir / TRAJECTORY_FILENAME
                if not path.is_file():
                    raise _NotFound(f"{trial_name} has no agent/trajectory.json yet")
                return _inline(
                    path.read_bytes(), "application/json", f"{trial_name}.trajectory.json"
                )
            case ["labels", job_name, trial_name]:
                self._trial(job_name, trial_name)
                if labeler is None:
                    raise LabelError("name the labeler: ?labeler=<name>")
                labels = self.labels.load(job_name, labeler, trial_name)
                return _json(labels.model_dump(mode="json") if labels else None)
            case ["export"]:
                return self._export(query)
            case ["file", job_name, trial_name]:
                trial = self._trial(job_name, trial_name)
                return _json(self._file(trial, query.get("path", ""), query.get("call", "")))
            case ["queue", job_name]:
                return _json(self._queue(job_name, labeler))
            case ["agreement", job_name]:
                return _json(self._agreement(job_name, query.get("a"), query.get("b")))
            case ["guide"]:
                return _json(self._guide())
        raise _NotFound("no such API route")

    def _diffs_of(self, trial: Path) -> dict[str, dict[str, str]]:
        """The full recorded diffs of a trial, by call id and path (the trace cuts long ones)."""
        source = trial / TRAJLAB_DIRNAME / CHANGES_FILENAME
        key = (source.stat().st_mtime_ns, source.stat().st_size) if source.is_file() else None
        with self._lock:
            cached = self._diffs.get(trial)
            if cached is not None and cached[0] == key:
                return cached[1]
        diffs: dict[str, dict[str, str]] = {}
        if source.is_file():
            for line in source.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    change = CallChanges.model_validate_json(line)
                except ValidationError:
                    continue
                # The first line per call, as in `build_trace`.
                diffs.setdefault(
                    change.tool_call_id, {f.path: f.diff for f in change.files if f.diff}
                )
        with self._lock:
            self._diffs[trial] = (key, diffs)
            while len(self._diffs) > CACHED_TRACES:
                self._diffs.popitem(last=False)
        return diffs

    def _file(self, trial: Path, path: str, call: str) -> dict[str, Any]:
        """A changed path after a call: from the diffs when they rebuild it, else from the
        checkpoint image that holds the state after the call."""
        trace = self._trace(trial)
        versions = trace["files"].get(path)
        if versions is None:
            raise _NotFound("no call in this trial changed that path")
        index = next(
            (i for i, e in enumerate(trace["events"]) if e.get("id") == call and "state_of" in e),
            None,
        )
        if index is None:
            raise _NotFound("no such call in this trial")
        diffs = {
            call_id: by_path[path]
            for call_id, by_path in self._diffs_of(trial).items()
            if path in by_path
        }
        view = file_after(versions, diffs, index) | {"path": path, "call": call, "source": None}
        if view["text"] is not None and view["verified"]:
            view["source"] = "diffs"
            return view
        if view["exists"] is False:
            return view
        seq = trace["events"][index]["state_of"]
        image = trace["checkpoint_images"].get(str(seq)) if seq is not None else None
        if image is None:
            view["note"] += " No checkpoint holds this state, so there is no snapshot to read."
            return view
        data, problem = self.snapshots.read(image, path)
        if data is None:
            view["note"] += f" Reading checkpoint #{seq} failed: {problem}"
            return view
        try:
            text = data.decode("utf-8")
            binary = "\0" in text
        except UnicodeDecodeError:
            text, binary = None, True
        view |= {
            "exists": True,
            "source": "snapshot",
            "checkpoint": seq,
            "size": len(data),
            "binary": binary,
            "text": None if binary else text,
            "verified": hashlib.sha256(data).hexdigest() == view.get("sha256"),
            "note": f"Read from checkpoint #{seq}." + (" (binary)" if binary else ""),
        }
        return view

    def _queue(self, job_name: str, labeler: str | None) -> dict[str, Any]:
        rows = {}
        for trial in trial_dirs(self._job(job_name)):
            row = trial_summary(trial)
            labels = self.labels.load(job_name, labeler, trial.name) if labeler else None
            row["progress"] = progress(labels) if labeler else None
            rows[trial.name] = row
        queue = labeling_queue(job_name, list(rows.values()))
        for section in queue["sections"]:
            section["trials"] = [rows[name] for name in section["trials"]]
        return queue

    def _agreement(self, job_name: str, a: str | None, b: str | None) -> dict[str, Any]:
        self._job(job_name)
        labelers = self.labels.labelers(job_name)
        a = a or (labelers[0] if labelers else None)
        b = b or next((x for x in labelers if x != a), None)
        if a is None or b is None or a == b:
            return {"labelers": labelers, "a": a, "b": b, "result": None}
        result = agreement(
            list(self.labels.iter_labels(job=job_name, labeler=a)),
            list(self.labels.iter_labels(job=job_name, labeler=b)),
            self._context,
        )
        return {"labelers": labelers, "a": a, "b": b, "result": result}

    def _guide(self) -> dict[str, Any]:
        if self.guide is None or not self.guide.is_file():
            return {"path": str(self.guide) if self.guide else None, "text": None, "sections": {}}
        text = self.guide.read_text()
        return {"path": str(self.guide), "text": text, "sections": guide_sections(text)}

    def _job_row(self, name: str, job: Path, labeler: str | None) -> dict[str, Any]:
        done = 0
        if labeler:
            done = sum(1 for x in self.labels.iter_labels(job=name, labeler=labeler) if x.done)
        return {
            "name": name,
            **job_summary(job),
            "labelers": self.labels.labelers(name),
            "done": done,
        }

    def _trace(self, trial: Path) -> dict[str, Any]:
        """The trial's trace, rebuilt when any file it reads changes (a running trial grows)."""
        paths = TrialPaths(trial)
        checkpoints = paths.agent_dir / CHECKPOINTS_DIRNAME
        sources = (
            paths.agent_dir / TRAJECTORY_FILENAME,
            paths.result_path,
            checkpoints / CALLS_FILENAME,
            checkpoints / CHECKPOINT_RECORDS_FILENAME,
            trial / TRAJLAB_DIRNAME / CHANGES_FILENAME,
            trial / TRAJLAB_DIRNAME / PROCESSES_FILENAME,
            paths.verifier_dir / "ctrf.json",
        )
        key = tuple(
            (s.stat().st_mtime_ns, s.stat().st_size) if s.is_file() else None for s in sources
        )
        with self._lock:
            cached = self._traces.get(trial)
            if cached is not None and cached[0] == key:
                self._traces.move_to_end(trial)
                return cached[1]
        trace = build_trace(trial)
        with self._lock:
            self._traces[trial] = (key, trace)
            self._traces.move_to_end(trial)
            while len(self._traces) > CACHED_TRACES:
                self._traces.popitem(last=False)
        return trace

    def _context(self, job_name: str, trial_name: str) -> dict[str, Any]:
        """What an export row says about a trial and its steps, from the trace."""
        try:
            trace = self._trace(self._trial(job_name, trial_name))
        except (_NotFound, OSError, ValueError):
            return {}
        steps = {
            e["id"]: {"call": e.get("n"), "tool": e["tool"], "summary": e["summary"]}
            for e in trace["events"]
            if e["kind"] in ("call", "stop")
        }
        return {"task": trace["trial"]["task"], "reward": trace["trial"]["reward"], "steps": steps}

    def _export(self, query: dict[str, str]) -> Response:
        job, trial, labeler = (
            query.get("job") or None,
            query.get("trial") or None,
            query.get("labeler") or None,
        )
        if trial and not job:
            raise LabelError("a trial export names its job: &job=<name>")
        labels = list(self.labels.iter_labels(job=job, trial=trial, labeler=labeler))
        rows = export_rows(labels, self._context)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        name = "-".join(["trajlab-labels", *(x for x in (job, trial, labeler) if x), stamp])
        if query.get("format", "csv") == "json":
            body = json.dumps(
                {
                    "exported_at": datetime.now(UTC).isoformat(),
                    "patterns": list(LABEL_PATTERNS),
                    "rows": rows,
                    "labels": [label.model_dump(mode="json") for label in labels],
                },
                ensure_ascii=False,
                indent=1,
            ).encode()
            content_type, extension = "application/json; charset=utf-8", "json"
        else:
            body = rows_csv(rows).encode()
            content_type, extension = "text/csv; charset=utf-8", "csv"
        disposition = f'attachment; filename="{name}.{extension}"'
        return Response(200, body, content_type, (("Content-Disposition", disposition),))

    def _put(self, parts: list[str], query: dict[str, str], body: bytes) -> Response:
        match parts:
            case ["labels", job_name, trial_name]:
                self._trial(job_name, trial_name)
                labeler = check_labeler(query.get("labeler", ""))
                labels = parse_labels(body)
                if (labels.job_name, labels.trial_name, labels.labeler) != (
                    job_name,
                    trial_name,
                    labeler,
                ):
                    raise LabelError("the labels name another job, trial, or labeler than the URL")
                labels = labels.model_copy(update={"updated_at": datetime.now(UTC)})
                self.labels.save(labels)
                return _json(labels.model_dump(mode="json"))
        raise _NotFound("no such API route")


class _Handler(BaseHTTPRequestHandler):
    site: TraceSite
    server_version = "trajlab-trace"

    def do_GET(self) -> None:
        self._serve("GET")

    def do_PUT(self) -> None:
        self._serve("PUT")

    def do_POST(self) -> None:
        self._serve("POST")

    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            response = _error(413, "request body too large")
        else:
            body = self.rfile.read(length) if length else b""
            headers = {name.lower(): value for name, value in self.headers.items()}
            try:
                response = self.site.handle(method, self.path, headers, body)
            except Exception:
                logger.exception("trace site: %s %s failed", method, self.path)
                response = _error(500, "server error; see the trajlab trace log")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s " + format, self.address_string(), *args)


def serve(site: TraceSite, host: str, port: int) -> None:
    """Serve the site until interrupted."""
    handler = type("TraceHandler", (_Handler,), {"site": site})
    with ThreadingHTTPServer((host, port), handler) as server:
        server.daemon_threads = True
        server.serve_forever()
