"""The plan as a work order: ordered steps, each with its edits, its claim and how to verify it.

HOST side (3.12+, pydantic). ``models`` holds the step models of plan schema 2, ``edits`` turns
one recipe's before/after text into hunks, an effect and anchors, and ``v1`` reads a schema-1
plan as one synthetic step. Design: ``docs/design/apply-work-order.md`` section 1.
"""
