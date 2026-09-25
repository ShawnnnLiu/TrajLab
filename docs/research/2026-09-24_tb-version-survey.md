# Terminal-Bench version survey: what authoritative sources actually run

Date: 2026-09-24.
Scope: which Terminal-Bench (TB) version authoritative papers and model reports evaluate on, plus the version, protocol, and citation facts needed to choose the task set for the trajlab corpus.
Method: four parallel research agents against primary sources: (A) Semantic Scholar citations of the TB paper (438 citing papers, all pages) plus arXiv full-text search (103 hits), with per-paper verification against arXiv HTML full texts; (B) US/EU lab model and system cards; (C) Chinese-lab and open-weights model reports; (D) tbench.ai live pages, Wayback snapshots, GitHub API, and the locally installed Harbor source.
This document records the facts only; the recommendation lives in `2026-09-24_tb-version-recommendation.md`.

Legend: [primary] = quoted from the primary document, [secondary] = primary is fetch-blocked, quote corroborated by two or more independent secondary sources, [inferred] = version inferred from date, harness, or task count as noted, [src] = read in installed source or repo files.

## Corrections to facts we previously held

1. **TB 2.1 is a released artifact.**
   Our earlier note "no released artifact: no tag, no registry entry" was wrong.
   tbench.ai announced it on 2026-05-06 (`tbench.ai/news/terminal-bench-2-1`): "A revision of Terminal-Bench 2.0 that fixes 28 tasks". [primary]
   It ships as Harbor Hub dataset `terminal-bench/terminal-bench-2-1` (version 6 referenced), has its own leaderboard at `tbench.ai/leaderboard/terminal-bench/2.1`, and a HuggingFace mirror `harborframework/terminal-bench-2.1` whose manifest lists one dataset, version 2.1.0, 89 tasks. [primary]
   There is no git tag, which is why a tag-only search missed it.
   Fix categories: external dependency drift, resource budget mismatches, instructions misaligned with tests (example given: `query-optimize` tests expected Spark SQL output while the instructions asked for PostgreSQL). [primary]
2. **The tbench.ai leaderboard is not strictly "4.0 only".**
   The default board is TB 4.0, but version-scoped boards for 1.0, 2.0, 2.1, and 3.0 remain live or archived, each with its own frozen entries. [primary]
3. **The TB 2.0 paper is now citable as ICLR 2026.**
   arXiv 2601.11868 (Merrill et al., submitted 2026-01-17) appears as "The Fourteenth International Conference on Learning Representations, 2026", `openreview.net/forum?id=a7Qa4CcHak`. [primary]
   It remains the only TB paper; no newer official TB paper was found.

## Version reference

| Version | Tasks | Released | Artifact | Notes |
|---|---|---|---|---|
| TB 1.0 | ~80 ("terminal-bench-core==0.1.1") | 2025-05-19 | repo `laude-institute/terminal-bench`, now redirects to `harbor-framework/terminal-bench-1` | old `tb` CLI + laude registry; Terminus agent era |
| TB 2.0 | 89 | 2025-11-07 | repo `laude-institute/terminal-bench-2` (redirects to `harbor-framework/terminal-bench-2`; created 2025-09-25, no tags, no releases; pinned commit `69671fb`); tasks mirrored in `harbor-framework/terminal-bench` under `archive/`; Hub ref `terminal-bench@2.0` | released with Harbor; the paper describes this version |
| TB 2.1 | 89 (28 fixed) | 2026-05-06 | Harbor Hub dataset `terminal-bench/terminal-bench-2-1` (v6); no git tag; HF mirror `harborframework/terminal-bench-2.1` | scores ~10+ points above 2.0-era numbers because broken tasks were repaired; 2.0 and 2.1 numbers are not comparable |
| TB 3.0 | 74, 7 domains | tag `v3.0.0` 2026-07-23T06:49Z, announced 2026-07-30 | `harbor-framework/terminal-bench` tag `v3.0.0` | "expands on Terminal-Bench 2.1"; internal name "Frontier-Bench 0.1" per the audit paper (arXiv 2609.26826) |
| TB 4.0 | 66 | tag `v4.0.0` 2026-08-26T04:38Z, announced 2026-08-28 | `harbor-framework/terminal-bench` tag `v4.0.0` (current main); CITATION.cff date-released 2026-08-26 | from 3.0: removed 8 tasks (saturation 2, refusals 2, public solutions 2, quality/platform 2), fixed 19; flat 8-hour agent timeout; requires GPU-capable sandboxes |

Roadmap per the 4.0 announcement: "Terminal-Bench 4.1: verifier improvements (e.g. tamper-resistant verifiers)" and "Terminal-Bench 5.0: new tasks". [primary]
Announced intent only; nothing released as of 2026-09-24.

Citation practice: the repo README says to cite via the GitHub "Cite this repository" button / CITATION.cff.
CITATION.cff on main is type software, title "Terminal-Bench", version v4.0.0, DOI 10.5281/zenodo.22105680, ~100 authors ending Merrill, Konwinski, Schmidt. [src]
Neither the README nor CITATION.cff references the arXiv paper.
There is no CITATION.cff in `terminal-bench-1` or `terminal-bench-2` (raw fetches 404), so 1.0/2.0/2.1 have no official per-version citation.
Zero academic papers were found citing the Zenodo DOI; the software-citation route for 3.0/4.0 is effectively unused.
A sibling project exists: Terminal-Bench-Science (`harbor-framework/terminal-bench-science`, DOI 10.5281/zenodo.22110253).

## Headline counts

52 primary documents were verified to run a numbered TB version: 34 model reports/cards and 18 research papers.
Four documents run two versions (ROME and CLI-Gym run 1.0+2.0, Tmax runs 2.1+2.0, GLM-5.3 runs 2.1+3.0), so version calls sum to 56.
Three further documents run only derivatives (Cohere: AA "Terminal-Bench Hard"; MiniMax-M3: Long-Horizon-TB; the TB-3 audit paper), excluded from the counts below.

| Version | Model reports (34 docs) | Research papers (18 docs) | Total calls |
|---|---|---|---|
| TB 1.0 | 8 | 3 | 11 |
| TB 2.0 | 16 | 13 | 29 |
| TB 2.1 | 5 (4 primary-verified + 1 secondary-only) | 4 | 9 |
| TB 3.0 | 1 (GLM-5.3, alongside 2.1) | 0 | 1 |
| TB 4.0 | 3 | 0 | 3 |
| Version unresolvable | 2 | 1 | 3 |

Zero academic papers evaluate on TB 3.0 or 4.0.
TB 4.0 appears only in the three newest lab reports (all September 2026) and on leaderboards.
The 89-task 2.0/2.1 family accounts for 38 of 56 version calls.

## Table A: model reports and cards

| Report | Lab | Date | TB version | Evidence for the version call | Score | Protocol |
|---|---|---|---|---|---|---|
| Kimi-K2-Instruct HF card | Moonshot | 2025-07 | 1.0 [inferred: predates 2.0; Terminus harness] | "TerminalBench (Inhouse Framework)": 30.0; "(Terminus)": 25.0 | 30.0 / 25.0 | in-house framework vs Terminus |
| GLM-4.5 report (arXiv 2508.06471) | Zhipu | 2025-08 | 1.0 [inferred: date; "We use the Terminus framework"] | Table 5 | 37.5 | Terminus, function calling |
| DeepSeek-V3.1 HF card | DeepSeek | 2025-08 | 1.0 [primary: "Terminus 1 framework" label] | table row | 31.3 | Terminus 1 |
| DeepSeek-V3.2-Exp HF card | DeepSeek | 2025-09 | 1.0 [inferred: date; direct comparison to V3.1's TB1 score] | table row | 37.7 | Terminus lineage |
| Claude Sonnet 4.5 post | Anthropic | 2025-09-29 | 1.0 [inferred: predates 2.0; matches contemporaneous TB1 board] | footnote: "default agent framework (Terminus 2), with XML parser, averaging multiple runs" | 50.0 | Terminus 2 XML, multi-run mean |
| Claude Haiku 4.5 post | Anthropic | 2025-10-15 | 1.0 [inferred: footnote sources competitor scores from pre-2.0 leaderboard] | footnote: "averaging 11 runs ... n-attempts=1" | 40.21 / 41.75 | Terminus 2, 11 runs, n-attempts=1 |
| MiniMax-M2 HF card | MiniMax | 2025-10 | 1.0 [primary: pinned commit `94bf692` in the original repo; strongest evidence class] | footnote: "official claude-code from the original Terminal-Bench repository ... averaged over 8 runs" | 46.3 | Claude Code, mean of 8 runs |
| Kimi-K2-Thinking HF card | Moonshot | 2025-11-06 | 1.0 [inferred: unversioned label, released before 2.0] | footnote: Terminus-2 JSON, avg of 5 runs | 47.1 | Terminus-2 JSON, avg of 5 |
| Claude Opus 4.5 system card | Anthropic | 2025-11-24 | 2.0 [primary: explicit] | "We ran Terminal-Bench 2.0 using the Terminus-2 harness, in the Harbor scaffold ... 59.27%±1.34% with 1,335 trials" | 59.3 | Terminus-2 in Harbor, 1,335 trials, 2x resource bump on OOM |
| GPT-5.1-Codex-Max post | OpenAI | 2025-11-19 | 2.0 [secondary: openai.com blocks fetchers; quote via LessWrong; harness confirmed by Anthropic's card] | "58.1% on Terminal-Bench 2.0" | 58.1 | Codex CLI, self-reported |
| Gemini 3 Pro launch blog | Google DeepMind | 2025-11-18 | 2.0 [primary: explicit] | "scores 54.2% on Terminal-Bench 2.0" | 54.2 | Terminus-2 per Google's model page |
| GPT-5.2 / GPT-5.2-Codex posts | OpenAI | 2025-12-11 / 12-19 | 2.0 [secondary: triple-corroborated (Willison; Anthropic repro at 890 trials)] | "5.2 Codex scores 64% on ... Terminal-Bench 2.0 ... GPT-5.2 scored 62.2%" | 62.2 / 64.0 | Codex CLI, self-reported |
| GLM-4.7 docs | Zhipu | 2025-12 | 2.0 [primary: explicit] | "41% on Terminal Bench 2.0 (a 16.5% improvement)" | 41.0 | not stated |
| Kimi-K2.5 card / report (arXiv 2602.02276) | Moonshot | 2026-01-29 | 2.0 [primary: explicit] | "Terminal-Bench 2.0 scores ... (Terminus-2) ... non-thinking mode" | 50.8 | Terminus-2 JSON, non-thinking |
| Claude Opus 4.6 system card | Anthropic | 2026-02 | 2.0 [primary: explicit] | "Terminal-Bench 2.0 in the Harbor scaffold using the Terminus-2 harness ... 65.4% with max effort" | 65.4 / 61.1 / 55.1 by effort | Terminus-2 in Harbor, GKE n2-standard-32; also reproduced Gemini 3 Pro 56.2% (445 trials) |
| GPT-5.3-Codex post | OpenAI | 2026-02-05 | 2.0 [secondary: corroborated by Google's model page "self-reported harness 77.3% (Codex)"] | "77.3% on Terminal-Bench 2.0" | 77.3 | Codex, self-reported |
| Gemini 3.1 Pro model page | Google DeepMind | 2026-02-19 | 2.0 [primary: explicit table with harness column] | "Terminal-Bench 2.0 ... Terminus-2 harness: 68.5 [3.1 Pro], 56.9 [3 Pro], 59.1 [Sonnet 4.6], 65.4 [Opus 4.6], 54.0 [GPT-5.2]" | 68.5 | Google-run Terminus-2 for competitors; separate self-reported row for OpenAI |
| Cursor Composer 1.5 blog | Cursor | 2026-02-09 | 2.0 [primary: explicit] | "official Harbor evaluation framework (the designated harness for Terminal-Bench 2.0) with default benchmark settings ... 2 iterations per model-agent pair" | 47.9 | Harbor defaults, mean of 2 |
| Qwen3-Coder-Next report (arXiv 2603.00729) | Alibaba | 2026-03-03 | 2.0 [primary: explicit] | "multiple TerminalBench 2.0 environments, including XML- and JSON-based tool schemas" | 36.2 / 30.9 / 25.8 by scaffold | Terminus-2 XML / JSON / Claude Code; max 300 turns |
| Claude Opus 4.7 post | Anthropic | 2026-04-16 | 2.0 [primary: explicit footnote] | "Terminus-2 harness with thinking disabled ... averaged over five attempts per task" | 69.4 | Terminus-2, thinking disabled, mean of 5 |
| GPT-5.5 post | OpenAI | 2026-04-23 | 2.0 [secondary: via Wikipedia citing the post; the GPT-5.5 system card contains no TB mention] | "82.7% on Terminal-Bench 2.0" | 82.7 | not stated |
| DeepSeek-V4-Pro HF card | DeepSeek | 2026-04 | 2.0 [primary: explicit row] | "Terminal Bench 2.0 (Acc)" across reasoning modes | 67.9 max mode | reasoning-effort sweep; harness not stated |
| MiniMax-M2.7 HF card | MiniMax | 2026-04 | 2.x unresolvable ["Terminal Bench 2", minor revision unstated] | "On Terminal Bench 2 (57.0%)" | 57.0 | not stated |
| MAI-Code-1-Flash post / MAI-Thinking-1 | Microsoft AI | 2026-06-02 | 2.x unresolvable ["Terminal Bench 2" on their own harness] | "using the same production harness" | 46.0 (MAI-Thinking-1, secondary) | Microsoft production harness |
| Claude Fable 5 / Mythos 5 system card, §8.3 | Anthropic | 2026-06 | 2.1 [primary: card PDF read 2026-09-24] | "We've decided to switch to a new harness, mini-SWE-agent, which is more robust to timeouts compared to the Terminus-2 harness that we've previously reported ... Claude Fable 5: achieved 84.3% mean reward - with 20.9% of trials hitting a safety refusal and falling back to Claude Opus 4.8 for the rest of the trajectory, at high effort" | Fable 5: 84.3; Mythos 5: 88; Opus 4.8: 82.7 | mini-SWE-agent harness, GKE, 1x timeout rate, 3x memory ceiling, high effort (not max), 5 attempts x 89 tasks = 445 trials |
| Grok 4.5 post | xAI | 2026-07-16 | 2.1 [primary: explicit table] | "Terminal Bench 2.1: Fable (max) 84.3, GPT 5.5 (xhigh) 83.4, Grok 4.5 83.3, Opus 4.8 (max) 78.9" | 83.3 | not stated |
| Poolside Laguna S 2.1 | Poolside | 2026 | 2.1 [secondary only: Interconnects roundup; unverified against primary] | "scores 70.2% on Terminal-Bench 2.1" | 70.2 | not stated |
| GLM-5.3 HF card (cites arXiv 2602.15763) | Zhipu | 2026 | 2.1 AND 3.0 [primary: explicit rows for both] | 2.1: "Claude Code 2.1.207 ... 6h timeout"; 3.0: "Claude Code 2.1.207 harness ... avg@3 over three rollouts" | 88.2 (2.1); 28.3 (3.0) | Claude Code 2.1.207; avg@3 on 3.0 |
| Tencent Hy4-preview HF card | Tencent | ~2026-09 | 2.1 [primary: model-index metadata names dataset `harborframework/terminal-bench-2.1`] | HF eval widget from the card's own metadata | 85.4 (Hy3 70.8 per launch coverage) | not stated |
| Kimi-K2.6 HF card | Moonshot | ~2026-09 | 2.0 [primary: explicit] | "Terminal-Bench 2.0 (Terminus-2): 66.7 ... averaged over 10 independent runs" | 66.7 | Terminus-2 JSON, preserve-thinking, avg of 10 |
| Claude Fable 5.1 / Mythos 5.1 system card | Anthropic | 2026-09-01 | 4.0 [primary: explicit, 66 tasks named; also TB-Science 0.1] | "a set of 66 tasks ... Claude Code in --bare mode and maximum thinking effort ... public leaderboard reports Opus 5 at 51.8% and Fable 5 at 44.5% (five trials, Claude Code harness)" | Fable 5.1 55.8, Mythos 5.1 60.9, Opus 5 52.3, Fable 5 42.0; TB-Science 52.6 | Claude Code --bare, max thinking, 10-15 trials/task |
| Claude Opus 5.5 post | Anthropic | 2026-09-22 | 4.0 [primary: explicit] | "66.4%" on "Terminal-Bench 4.0"; leaderboard methodology footnote: Claude Code, 5 trials/task; SE ±2.6 | 66.4 | xhigh effort |
| Grok 4.7 post | xAI | 2026-09-21 | 4.0 [primary: explicit table] | "Multi-hour terminal work - Terminal-Bench 4.0: 37.6% (Grok 4.7 xHigh) vs 20.3% (Grok 4.6 High)" | 37.6 | xHigh; harness not stated |
| Cohere Command A+ | Cohere | 2026 | derivative only: "Terminal-Bench Hard" (Artificial Analysis index subset, not a numbered version) [secondary] | via Interconnects | 25 | AA Intelligence Index protocol |
| MiniMax-M3 HF card | MiniMax | ~2026-09 | derivative only: Long-Horizon-Terminal-Bench (46 tasks, partial credit; arXiv 2607.08964) [primary] | "mean reward x100 over 46 tasks ... official LHTB Harbor harness" | 38.5 mean reward; solved@0.95 = 3/46 | LHTB Harbor harness |

Frontier models checked with no first-party TB number: GPT-5 (system card has no TB), GPT-5.1, GPT-5.5 system card (announcement only), Grok 4 / 4 Fast / 4.1 / 4.1 Fast, Claude Opus 4.1 (TB row exists only in an image table, no extractable score), Claude Sonnet 5 announcement, Claude Opus 5 announcement (uses "Frontier-Bench v0.1", 43.3%; its TB 4.0 number appears only in the later Fable 5.1 card), Fable 5 / Mythos 5 announcement page (TB lives in the system card only), Meta Llama 4.x / Behemoth, Mistral Large 3 / Devstral / Codestral (the circulating Devstral 43.75% is third-party vals.ai), Amazon Nova, Cognition/Windsurf SWE-1.5, Reflection, Magic.dev, GLM-4.6 (secondary launch-chart figure ~40.5 on TB 1.0, unverified), official DeepSeek-V3.2 (Dec 2025), MiniMax-M1, original Qwen3-Coder (Jul 2025), original Qwen3-Max.
Secondary-only, not verified against primary (qwen.ai blocks fetchers): Qwen3.7 / Qwen3.8-Max TB 2.1 = 86.6; GLM-5.2 TB 2.1 = 81.0 (also visible as a column in GLM-5.3's own table).
Trap: HuggingFace auto eval-widgets show post-hoc third-party TB 2.0 leaderboard numbers on cards (GLM-4.6 24.5, Qwen3-Coder-480B 23.9); these are not self-reported results.

## Table B: research papers

| Paper (arXiv) | Type | Date | TB version | Evidence for the version call | Score | Protocol |
|---|---|---|---|---|---|---|
| SkyRL-Agent (2511.16108) | RL training | 2025-11-20 | 1.0 [primary] | "Terminal-Bench (version 0.1.1) ... contains 80 tasks"; laude registry OpenHands path | 16.25 | OpenHands agent, accuracy on 80 tasks |
| ROME (2512.24873, iFlow/Alibaba) | RL + ecosystem | 2025-12-31 | 1.0 + 2.0 [primary] | "Terminal Bench 1.0 contains only 80 tasks, and Terminal Bench 2.0 expands this marginally to 89" | 41.50 (1.0), 24.72 (2.0) | avg@3, iFlow CLI; introduces Terminal Bench Pro (400 tasks) |
| Scaling Agent Systems (2512.08296, Google Research/MIT) | benchmark study | 2025-12-09 | unresolvable [primary: "86 instances, first 20 instances used" matches no release size (80/89/74/66)] | Data Availability + App. E.4 | subset-only, e.g. Sonnet 4.5 single-agent 75 [55,95] | 20 tasks, 8 models x 5 architectures, bootstrap CIs |
| CLI-Gym / LiberCoder (2602.10999) | RL/SFT data | 2026-02-11 | 1.0 + 2.0 [primary] | "80 in v1.0 and 89 in v2.0"; "a modified Terminal-Bench harness" | 46.1 / 31.0 (235B) | OpenHands, pass@1 and pass@3 |
| Nemotron-Terminal (2602.21193, NVIDIA) | RL/SFT data engineering | 2026-02-24 | 2.0 [primary] | "Terminal-Bench 2.0 as our primary evaluation benchmark ... Terminus 2 ... We utilize Harbor" | 13.0 / 20.2 / 27.4 (8B/14B/32B) | Terminus 2 + Harbor, pass@1 ± across runs, 14-gram decontamination |
| Meta-Harness (2603.28052, Stanford) | agent framework | 2026-03-30 | 2.0 [primary; artifact shows `--n-attempts 5`] | "TerminalBench-2 evaluates ... 89 challenging tasks" | 76.4 (Opus 4.6), 37.6 (Haiku 4.5) | full 89, vs official leaderboard |
| NL Agent Harnesses (2603.25723) | framework study | 2026-03-26 | 2.0 [primary] | "Terminal-Bench 2.0 (TB2) evaluates long-horizon command-line tasks" | 45.05 / 13.18 / 22.82 | gpt-5.4-mini xhigh, one attempt |
| Agentic Skills in the Wild (2604.04323) | benchmark study | 2026-04-06 | 2.0 [primary] | "We use all 89 tasks ... using the Harbor framework. Each task is run 3 times" | Opus 4.6 57.7 to 65.5 with skills | Harbor, 3 runs/task, native harness per model |
| AHE (2604.25850, Fudan) | agent framework | 2026-04-28 | 2.0 [primary] | "the full 89 tasks of Terminal-Bench 2 ... 4 easy, 55 medium, and 30 hard" | 69.7 to 77.0 pass@1 (GPT-5.4) | Harbor + E2B, infra failures count as fail per leaderboard rules |
| SkillsVote (2605.18401) | agent framework | 2026-05-18 | 2.0 [primary] | "evaluate with Harbor on Terminal-Bench 2.0 ... avg@5 ... following their leaderboard protocols" | 51.1 baseline +2.6pp | Harbor, avg@5, Codex harness |
| Terminal-World (2605.20876) | RL data synthesis | 2026-05-20 | 2.0 [primary] | "We evaluate on its 89 tasks ... under the Terminus2 Agent scaffolding" | 31.5 pass@1, 43.8 pass@3 | Terminus2, 3 runs |
| Self-Harness (2606.09498, Shanghai AI Lab) | agent framework | 2026-06-08 | 2.0, 64-task subset [primary] | "a fixed 64-case subset, excluding tasks that depend on unreliable resources"; Harbor; 2 MB/s bandwidth cap | e.g. GLM-5 46.1 to 57.0 | 64-task subset, 2 attempts |
| Arbor (2606.11926) | agent framework | 2026-06-10 | 2.0, 36/53 dev/test split [primary] | "the official Harbor evaluation harness distributed with Terminal-Bench 2.0" | 77.36 held-out (GPT-5.5) | Harbor, pass rate |
| Tmax (2606.23321) | RL training | 2026-06-22 | 2.1 primary + 2.0 for comparability [primary: Hub refs `terminal-bench@2.0` and `openthoughts-tblite@2.0` named] | "primarily evaluate on Terminal-Bench 2.1 ... and Terminal-Bench Lite ... For final evaluations we use Terminal-Bench 2.0 ... to compare directly with past work" | 27.2 (2.0), 28.8±1.4 (2.1), 57.2±2.5 (TB-Lite) | mean±stderr of 3-5 runs, Daytona backend; notes verifier-tampering reward hacking |
| Agent Optimizers (2607.14004, RELAI) | benchmark study | 2026-07-15 | 2.0, 22-hard-task subset [primary] | "30 of the 89 tasks are annotated hard. T1 is exactly the 12 hard tasks with a 900-second agent timeout, and T2 ... 10 ... with 1800-second" | lifelong avg 76.4 | GPT-5.5, Harbor Terminus2 extension, full task IDs listed |
| Rethinking Harness Evolution (2607.12227) | methodology | 2026-07-14 | 2.1 [primary] | "Terminal-Bench 2.1 ... a verified revision of Terminal-Bench 2.0. It repairs 28 of the 89 tasks ... while keeping the suite at 89 terminal tasks" | e.g. 76.0 pass@1 (Opus 4.6) | pass@1 and pass@5, controlled compute; 45/10/34 split |
| StateM (2608.15089) | agent framework | 2026-08-15 | 2.1 [primary: title/abstract; 445 trials = 5x89] | "On Terminal-Bench 2.1, StateM raises GPT-5.5 xhigh to 92.1% ... GPT-5.6 Sol xhigh ... 95.3% raw accuracy across 445 trials" | 95.3 | 5 trials/task |
| T1 (2609.11042) | RL training | 2026-09-10 | 2.1 [primary; Harbor v0.7.0 + Daytona v0.168.0 pinned] | "Terminal-Bench 2.1 is our primary held-out benchmark ... It supersedes Terminal-Bench 2.0, whose instabilities hindered reproducible evaluation" | 64.0 (T1-122B); Opus 4.6 63.8 same harness | Terminus-2 via Harbor, max 60 turns, thinking disabled; also LHTB (27.9) and "Terminal-Bench Hard" 100-task (38.0) |
| TB task-hardness audit (2609.26826, incl. TB co-author Bercovich) | audit, not a model eval | 2026-09-20 | TB 3 production record (not the released 74-task set) | "a frozen Terminal-Bench 3 / Frontier-Bench 0.1 production record with 1,081 pull requests, 639 scored tasks, 28,801 trials, and $105,933 in logged agent spend" | n/a (78 of 125 all-fail tasks "certified-unsolved") | item-validity audit with reference/empty-solution controls |

Verified cite-only (mention TB but do not run it): SkillsBench (2602.12670, builds its own 87-task suite on the Harbor task format), the harness-disclosure position paper (2605.23950, runs SWE-bench Verified subsets), SWE-Master (2602.03411), CoEvoSkills (2604.01687, cites TB as ICLR 2026), TCOD (2604.24005), Claw-Eval (2604.06132), Harness-Bench (2605.27922), RExBench, plus dozens of Sept-2026 preprints not individually verified.

## Leaderboard snapshots and protocol

| Era | Source | Version | Top entries (agent / model / score) |
|---|---|---|---|
| 2025-06-09 | Wayback 20250609012106 | TB 1.0 | Claude Code / claude-3-7-sonnet / 35.2±1.3; Terminus / claude-3-7-sonnet / 30.6; Terminus / gpt-4.1 / 30.3; Terminus / o3 / 30.2 |
| 2025-11-18 | Wayback 20251118181017 (/leaderboard/terminal-bench/2.0, 57 entries) | TB 2.0 | Codex CLI / GPT-5.1-Codex / 57.8±2.9; Terminus 2 / Gemini 3 Pro / 54.2; Warp / multiple / 50.1; Codex CLI / GPT-5 / 49.6; Terminus 2 / Sonnet 4.5 / 42.8 |
| 2026-02-05 | Wayback 20260205195452 (2.0 board, 92 entries) | TB 2.0 | Droid / Opus 4.6 / 69.9±2.5; Droid / GPT-5.2 / 64.9; Ante / Gemini 3 Pro / 64.7; Junie CLI / Gemini 3 Flash / 64.3 |
| 2026-06-16 | Wayback 20260616153334 (index) + 20260616153359 (2.1 board) | TB 2.1 active; 3.0 board existed but empty | Codex CLI / GPT-5.5 / 83.4±2.2; Claude Code / Opus 4.8 / 78.9±2.5; Terminus 2 / GPT-5.5 / 78.2; Terminus 2 / Opus 4.8 / 74.6; Gemini CLI / Gemini 3.1 Pro / 70.7 |
| 2026-09-24 (live) | tbench.ai/leaderboard (leaderboard `4-0-0`, created 2026-08-27) | TB 4.0, 27 entries | Codex / GPT-6 Astra max / 58.2±2.8 ($3.3k); Claude Code / Fable 5.1 max / 57.9±3.8 ($6.2k); Fable 5.1 xhigh / 57.9; Opus 5 xhigh / 53.9; Fable 5 max / 44.5; GLM-5.3 max / 41.8; Grok Build / Grok 4.7 xhigh / 37.6; Codex / GPT-5.6 Sol max / 37.3; Opus 4.8 max / 23.6; mini-SWE-agent / Gemini 3.8 Flash / 19.1; Claude Code / Sonnet 5 max / 12.4 ($29.10/trial) |

Protocol facts:

- Leaderboard protocol from 2.0 onward: k=5 attempts per task via Harbor; the score is the mean resolution rate over all trials, not best-of; 95% CI whiskers.
  Verified on the live 4.0 board: every entry has n_trials = 330 = 66x5 and accuracy = successes/trials exactly (192/330 = 58.18%); pass@2..pass@5 are stored per entry but ranking is by mean accuracy. [primary]
  Submit command shown on the 2.0 board: `harbor run -d terminal-bench@2.0 -a "agent" -m "model" -k 5`; on the current /run page: `harbor run -d terminal-bench/terminal-bench@4.0.0 -e modal -a claude-code -m anthropic/claude-sonnet-5 -k 5`.
- Harbor's own default differs: `n_attempts: int = Field(default=1, ge=1)` (`harbor/models/job/config.py:403` in our pinned install), so an unflagged `harbor run` is one attempt per task, not the leaderboard protocol. [src]
- Constraint rules: "submissions may not modify timeouts or resources" (2.0/2.1 boards); TB 4.0 sets a flat 8-hour agent timeout and requires GPU-capable sandboxes. [primary]
- Integrity policy (2026-04-19, `tbench.ai/news/leaderboard-integrity-update`): "ATIF trajectories are required for all passing trials"; an agent judge reviews all passing trials; "Reward hacking will result in a reward of 0 for a trial". [primary]
  Post-April-2026 leaderboard numbers are therefore judge-filtered; self-reported paper numbers are not.
  Directly relevant to trajlab: the leaderboard mandates the same ATIF format our capture is built on.
- The TB paper prescribes no evaluation protocol in its abstract; its internal trial count is unverified (HTML fetch truncated). The contemporaneous 2.0 leaderboard protocol was k=5 mean.
- Score-era guide for matching a paper's baselines to a leaderboard snapshot: SOTA ~35% = TB 1.0 (mid-2025); ~58% = TB 2.0 (Nov 2025); ~70% = TB 2.0 (Feb 2026); ~83% = TB 2.1 (May-Jun 2026); ~58% again = TB 4.0 (Aug 2026 onward). Raw percentages alone are ambiguous between the Nov-2025 2.0 era and the current 4.0 era.

## Comparability facts

- Harness choice is worth roughly 6-10 points at fixed model and task set:
  T1 measured Opus 4.6 at 70.1 under Claude Code vs 63.8 under Terminus-2 [primary];
  Anthropic reproduced GPT-5.2-Codex at 57.5 on Terminus-2 vs OpenAI's 64.7 on Codex CLI (890 trials) [primary];
  Qwen3-Coder-Next scored 36.2 under Terminus2-XML vs 25.8 under Claude Code on the same model [primary];
  Kimi K2 scored 30.0 in-house vs 25.0 under Terminus [primary].
- Anthropic abandoned Terminus-2 for TB 2.1: the Fable 5 card (§8.3) switched to mini-SWE-agent because "at xhigh effort, Terminus-2 experiences 2.7x more timeouts than mini-SWE-agent, due to the way it waits for commands execution through a tmux session; this makes final scores noisier and less legible". [primary]
  Same section's harness cross-check on GPT-5.5: Harbor externally reproduced 81% mean reward on mini-SWE-agent at xhigh; Anthropic internally got 83% on the same configuration; the Codex-harness figure is 83.4%.
- The Fable 5.1 card (§8.6) states that TB 4.0's increased timeouts and adaptive RAM/CPU bumps "reduced the confounding role of various harnesses (e.g., CLI memory footprint, compaction strategy, and container communication protocol)", and shows its internal Claude Code `--bare` numbers within noise of the leaderboard's Claude Code entries (Fable 5: 42.0 internal vs 44.5 leaderboard; Opus 5: 52.3 vs 51.8). [primary]
- The 2.1 revision inflates scores ~10+ points over 2.0-era numbers despite an identical task count, because 28 broken tasks were repaired.
- Cross-lab tables silently re-version competitors' numbers: xAI lists GPT-5.5 at 83.4% on "Terminal Bench 2.1" while OpenAI's own claim is 82.7% on TB 2.0.
- Attempt protocols in the wild vary: n-attempts=1 (Haiku 4.5), mean of 2 (Cursor), avg@3, avg/mean of 5 (leaderboard, Opus 4.7, Kimi), 8 (MiniMax-M2), 10 (Kimi K2.6), 11 (Haiku 4.5 runs), pass@3/pass@5 (several papers), 10-15 trials/task (Fable 5.1 card).
- Version-labeling hygiene: everything before Nov 2025 is unversioned "Terminal-Bench" (TB 1.0, inferable from date/harness/commit); from Dec 2025 on, labs print explicit version strings.
  MiniMax-M2's pinned commit is the only registry-grade citation in the pre-2.0 group.

## Subsets and derivative benchmarks

Official or semi-official:

- Difficulty annotation on TB 2.0: the official leaderboard partitions the 89 tasks into 4 easy, 55 medium, 30 hard; tasks also carry agent timeouts (900 s and 1800 s tiers among the hard tasks).
  There is no official "core" or "hard" split product for 2.0+.
- Terminal-Bench Challenges (2026-06-18): "long-horizon, token-intensive, single-task benchmarks".
- Terminal-Bench-Science 0.1 (2026-08-27): 70 tasks, Stanford-led; its leaderboard runs 3 trials/task with Claude Code; debuted in the Fable 5.1 card (52.6%).
- Harbor-Index (2026-06-29): 82 tasks distilled from 6,627 candidates across 54 benchmarks; only 3 of TB 2.0's 89 tasks made the cut; a separate meta-benchmark, not a TB subset.

De facto subsets used by papers: Terminal-Bench Lite (OpenThoughts, Hub ref `openthoughts-tblite@2.0`) as a cheap validation set; RELAI's 22-hard-task split; Self-Harness's 64-task network-stable subset; Arbor's 36/53 dev/test split; Google/MIT's first-20-of-86 snapshot.

Third-party derivatives: Terminal Bench Pro (Alibaba, 400 tasks, 200 public / 200 private, `github.com/alibaba/terminal-bench-pro`); Long-Horizon-Terminal-Bench (arXiv 2607.08964, 46 tasks, dense partial-credit rewards, official Harbor harness); "Terminal-Bench Hard" the 100-task benchmark (arXiv 2608.05466) and, separately, "Terminal-Bench Hard" the Artificial Analysis index subset (same name, different things); Hack-Verifiable Terminal Bench (2608.22103); Terminal-Bench-LILT (2608.28641); TUA-Bench; TerminalWorld.

## Evidence-quality caveats

- All four OpenAI version calls rest on corroborated secondary quotes because openai.com 403-blocks automated fetchers; each is cross-confirmed by at least two of: LessWrong, Simon Willison, Wikipedia, Anthropic's system cards, Google's model page.
- The Fable 5 / Mythos 5 card was initially secondary-sourced; on 2026-09-24 the PDF was downloaded (linked from `anthropic.com/claude-fable-5-mythos-5-system-card`) and §8.3 read directly, upgrading that row to [primary].
  Two secondary claims were corrected in the process: the 84.3% run was at high effort, not max, and the harness is mini-SWE-agent, not Terminus-2 or Claude Code.
- Poolside (70.2 on 2.1), MAI-Thinking-1 (46.0), Qwen3.7/3.8-Max (86.6 on 2.1), and GLM-5.2 (81.0 on 2.1) are secondary-only.
- Everything else in Tables A and B was verified against the primary document, with the quoted sentence or table cell recorded.
- Wayback coverage gaps: no /leaderboard snapshot exists at exactly 2025-11-01 or 2026-02-01; the nearest usable captures are listed above.
