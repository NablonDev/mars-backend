"""Build message lists for the penalty rule extraction LLM stages.

Retrieved contract content and reviewer feedback are serialized as data and
wrapped in `<DATA>` tags rather than treated as instructions. System prompts
are supplied by the caller.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from app.utils.json_helpers import wrap_data


class ScreeningUnitContext(BaseModel):
    """One screening call's worth of contract text, labeled by its heading breadcrumb."""

    label: str
    unit_text: str


class ClauseClassificationContext(BaseModel):
    """Verified clause and optional reviewer context for classification.

    Reviewer fields are present only during a revision and are passed as data,
    not as system-level instructions.
    """

    section_title: str | None
    clause_text: str
    reviewer_instruction: str | None = None
    current_rule: dict[str, Any] | None = None
    revision_history: list[dict[str, str]] | None = None


class RuleFactContext(BaseModel):
    """Classified rule and optional context for fact extraction.

    `previous_issues` is populated during consistency retries. Reviewer fields
    are populated during revisions. All are passed as data, not instructions.
    """

    clause_text: str
    penalty_category: str
    calc_type: str
    previous_issues: list[str] | None = None
    reviewer_instruction: str | None = None
    current_facts: list[dict[str, Any]] | None = None


def build_screening_messages(system_prompt: str, context: ScreeningUnitContext) -> list[BaseMessage]:
    """Message list for the screening stage: find every candidate clause in one unit."""
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=wrap_data(context.model_dump())),
    ]


def build_classification_messages(
    system_prompt: str, context: ClauseClassificationContext
) -> list[BaseMessage]:
    """Message list for the classification stage: turn one clause into a penalty rule."""
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=wrap_data(context.model_dump())),
    ]


def build_fact_extraction_messages(system_prompt: str, context: RuleFactContext) -> list[BaseMessage]:
    """Message list for the fact stage: extract the thresholds, rates and caps a rule depends on."""
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=wrap_data(context.model_dump())),
    ]
