"""Ground truth for error localization by verifier replay (ADR-0013).

The analysis phase's only package so far: it extracts each trial's graded artifacts at every
timeline point, reruns the task's verifier on them through Harbor's regrade, and turns the
results into ground-truth items. It imports only `trajlab.contracts` and Harbor.
"""
