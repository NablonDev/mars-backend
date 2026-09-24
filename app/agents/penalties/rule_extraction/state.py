"""State and lane inputs for the penalty rule extraction workflow.

The workflow carries a contract through screening, candidate resolution,
per-clause extraction, and persistence.

Fan-out nodes receive dedicated lane inputs rather than the full workflow
state. Results from parallel lanes use ``operator.add`` reducers so each
lane appends its results instead of overwriting results produced by other
lanes.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict
from uuid import UUID


class RuleExtractionState(TypedDict, total=False):
    """Execution state for a single contract extraction run.

    `run_id` is consumed by `app.core.tracing.traced` when recording
    `process.agent_trace` rows. It intentionally matches the naming used
    by `app.agents.cmir.state.GraphState`.
    """

    retailer_agreement_id: UUID
    run_id: UUID
    retailer_agreement_text: str
    screening_units: list[dict[str, Any]]
    screened_candidates: Annotated[list[dict[str, Any]], operator.add]
    candidate_clauses: list[dict[str, Any]]
    drafts: Annotated[list[dict[str, Any]], operator.add]
    extraction_errors: Annotated[list[dict[str, Any]], operator.add]
    staged_rule_ids: list[UUID]


class ScreenUnitInput(TypedDict):
    """The entire input one `screen_unit` `Send` lane receives, nothing else from state."""

    unit: dict[str, Any]


class ProcessClauseInput(TypedDict):
    """The entire input one `process_clause` `Send` lane receives, nothing else from state."""

    clause: dict[str, Any]
