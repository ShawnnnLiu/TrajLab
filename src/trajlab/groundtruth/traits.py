"""What each TB 4.0 task's verifier is like, for scheduling replays and reading results.

Checked on 2026-10-07 against the task packages and the recorded verifier output of
`tb40-sonnet-v2` (the ADR-0013 design critique):

- `artifact_class`: what the graded files are. `source`: code the verifier imports, builds, or
  runs. `output`: result files the agent's code wrote (a fix may have to change the code that
  wrote them). `generated`: files built from code by a toolchain or a run (compiled `.so`,
  FreeCAD documents, model weights), which a diff of the graded files cannot change.
- `quiet`: some checks race wall-clock limits (vf2 `test_speed` needs a 5000x speedup;
  interleaved-vigenere kills its cracker after 30 s; bun allows 60 s; vba has 8 to 45 s
  deadlines), so replays run beside little other load (`trajlab.groundtruth.admission`).
- `network`: the verifier installs packages at grading time (vba pip and npm; cargo apt and
  uv when the agent's lists are non-empty), so a replay may resolve different versions.
- `message_blind`: the task's conftest replaces every failure message.
- `timing_checks`, `aggregate_checks` (a threshold on a continuous score), `self_test_checks`
  (checks of the verifier's own harness), `unread_artifacts` (declared, never read).
"""

from dataclasses import dataclass, field
from typing import Literal

ArtifactClass = Literal["source", "output", "generated"]


@dataclass(frozen=True)
class TaskTraits:
    artifact_class: ArtifactClass = "source"
    quiet: bool = False
    network: bool = False
    message_blind: bool = False
    timing_checks: frozenset[str] = field(default_factory=frozenset)
    aggregate_checks: frozenset[str] = field(default_factory=frozenset)
    self_test_checks: frozenset[str] = field(default_factory=frozenset)
    unread_artifacts: frozenset[str] = field(default_factory=frozenset)


_FREECAD = TaskTraits(
    "generated",
    aggregate_checks=frozenset({"cad:score"}),
    unread_artifacts=frozenset({"/app/answer.py"}),
)
_LAYOUT = TaskTraits(
    "output", aggregate_checks=frozenset({"pytest:test_state.py::test_pixel_similarity"})
)
_VF2_SPEED = "pytest:test_outputs.py::TestSpeedBenchmark::test_speed"

TASKS: dict[str, TaskTraits] = {
    "terminal-bench/bun-sourcemap-leak": TaskTraits("source", quiet=True),
    "terminal-bench/cad-model": TaskTraits("output"),
    "terminal-bench/cargo-flight-dispatch": TaskTraits("source", network=True),
    "terminal-bench/foodstuff-beta-activity": TaskTraits("output"),
    "terminal-bench/freecad-impeller": _FREECAD,
    "terminal-bench/freecad-platform-drawing": TaskTraits(
        "generated", aggregate_checks=frozenset({"cad:score"})
    ),
    "terminal-bench/freecad-spring-clip": _FREECAD,
    "terminal-bench/gsea-proteomics": TaskTraits("output"),
    "terminal-bench/interleaved-vigenere": TaskTraits(
        "source",
        quiet=True,
        timing_checks=frozenset(
            {
                "pytest:test_outputs.py::test_decryption_accuracy",
                "pytest:test_outputs.py::test_output_same_length_as_input",
            }
        ),
    ),
    "terminal-bench/layout-config-recreation": _LAYOUT,
    "terminal-bench/layout-config-recreation2": _LAYOUT,
    "terminal-bench/mvcc-lsm-compaction": TaskTraits("source"),
    "terminal-bench/pretrain-shard-corruption": TaskTraits("generated"),
    "terminal-bench/production-planning": TaskTraits("output"),
    "terminal-bench/protein-autointerp-disulfide": TaskTraits("output"),
    "terminal-bench/risk-scorer-replay": TaskTraits("source"),
    "terminal-bench/roy-polymorph-cn": TaskTraits("output"),
    "terminal-bench/sglang-qwen-burst": TaskTraits("source", message_blind=True),
    "terminal-bench/sound-change-cascade": TaskTraits("output"),
    "terminal-bench/vba-userform-port": TaskTraits(
        "source",
        quiet=True,
        network=True,
        self_test_checks=frozenset(
            {
                "pytest:test_verifier_hygiene.py::test_app_base_path_exposes_verifier_backend_venv",
                "pytest:test_verifier_hygiene.py::"
                "test_harness_starts_chatty_app_without_stdout_deadlock",
                "pytest:test_verifier_hygiene.py::"
                "test_harness_reports_startup_output_when_frontend_exits",
            }
        ),
    ),
    "terminal-bench/vf2-speedup-networkx": TaskTraits(
        "generated",
        quiet=True,
        message_blind=True,
        timing_checks=frozenset({_VF2_SPEED}),
        aggregate_checks=frozenset({_VF2_SPEED}),
    ),
    "terminal-bench/vllm-deepseek-streaming": TaskTraits("source"),
    "terminal-bench/vpp-loss-divergence": TaskTraits("source"),
}


def traits(task_name: str) -> TaskTraits:
    """A task's traits; an unknown task gets the defaults."""
    return TASKS.get(task_name, TaskTraits())
