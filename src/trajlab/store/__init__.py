"""The Exp 2 store (ADR-0014): a trial's steps joined to its filesystem deltas, in DuckDB.

Analysis-side: imports `trajlab.contracts`, `trajlab.atif`, and Harbor only, never `capture`
or `checkpoint`.
"""
