"""Workflow execution state for the penalty rule extraction pipeline.

Carries one contract's markdown through segmentation, screening, excerpt resolution,
per-clause processing, staged persistence, review, and decision application. The two
`Send` fan-outs (`screen_unit`, `process_clause`) never see this state: each lane
receives only its own `ScreenUnitInput`/`ProcessClauseInput` dict, and every key a lane
writes back into is `Annotated[list, operator.add]` so parallel results merge instead of
overwriting each other.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict
from uuid import UUID


class RuleExtractionState(TypedDict, total=False):
    """Workflow execution state for one extraction run against one contract.

    `run_id` is read by `app.core.tracing.traced()` to key `process.agent_trace`
    rows, so it keeps that exact name rather than `agent_run_id`, mirroring
    `app.agents.cmir.state.GraphState`.
    """

    contract_id: UUID
    run_id: UUID
    contract_text: str
    screening_units: list[dict[str, Any]]
    screened_candidates: Annotated[list[dict[str, Any]], operator.add]
    candidate_clauses: list[dict[str, Any]]
    drafts: Annotated[list[dict[str, Any]], operator.add]
    extraction_errors: Annotated[list[dict[str, Any]], operator.add]
    staged_rule_ids: list[UUID]
    resume_signal: dict[str, Any]
    applied_rule_ids: list[str]


class ScreenUnitInput(TypedDict):
    """The entire input one `screen_unit` `Send` lane receives, nothing else from state."""

    unit: dict[str, Any]


class ProcessClauseInput(TypedDict):
    """The entire input one `process_clause` `Send` lane receives, nothing else from state."""

    clause: dict[str, Any]
