# Project brief: #16, Efficient Analysis of Agent Execution Histories

Verbatim from the COMS 6113 (Fall 2026) project list, kept here as the reference for what the course asks of us.
Sentences are split one per line; wording is otherwise unchanged.

## Motivation

AI agents generate large, heterogeneous execution histories spanning model interactions, tool calls, application logs, database operations, and changes to files, processes, and network state.
These records could reveal how agents approach tasks, where failures originate, whether expected procedures are followed, and which behaviors predict progress, success, or unsafe outcomes.
The broader goal is to make these histories useful for understanding and improving agent behavior.

## Problem

Answering questions about agent executions is slow and labor-intensive because execution data is fragmented across formats and software components.
The challenge is to substantially reduce the time and effort required to extract useful signals while preserving the accuracy and provenance of the resulting analysis.

## Proposed approach

Students can begin with agents performing tasks from Terminal-Bench, using StateFork and Waypoint to capture branchable environment state alongside agent trajectories.
They will assemble a corpus containing multiple successful and unsuccessful runs and define a benchmark of representative analysis questions.
These should examine recurring strategies and action sequences, where successful and failed runs diverge, whether agents conform to an expected process, which actions cause consequential environment changes, and how early success, failure, or a safety violation can be detected.
Students will then propose and evaluate a system for answering these questions against existing ad hoc workflows.
The semester objective is to answer them more quickly or with substantially less analyst effort while preserving analysis accuracy and provenance.

## Suggested reading

- TrajectoryDB: A New Database for Agent Trajectories - the closest systems vision, especially its treatment of trajectories as a data type combining hierarchy, text, temporal order, and lineage.
- Arming Data Agents with Tribal Knowledge - demonstrates how experience extracted from failures can improve future agent behavior.
- Agentic Data Environments - motivates branching, cross-component state management, provenance, and data-flow control.
- ATLAS: Discovering Agent Strategies through LLM-Guided Abstraction and Automata Learning

## Benchmark question classes, extracted

The five question classes named above, as a checklist for the corpus and the analysis phase:

1. Recurring strategies and action sequences.
2. Where successful and failed runs diverge.
3. Whether the agent conforms to an expected process.
4. Which actions cause consequential environment changes.
5. How early success, failure, or a safety violation can be detected.
