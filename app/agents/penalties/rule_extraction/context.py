"""Assembles the message list for each of the three penalty rule extraction LLM calls.

Contract text is retrieved, untrusted content, never a system-level instruction. Every
context payload here is serialized and wrapped in `<DATA>` tags before being handed to
the model, matching the pattern in `app/services/cmir/extractor.py`. The system prompt
itself is a caller-supplied string, read fresh from the agent registry at inference
time by `app.agents.penalties.rule_extraction.adapter`, not a module constant.
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from app.utils.json_helpers import wrap_data


class ScreeningUnitContext(BaseModel):
    """One screening call's worth of contract text, labeled by its heading breadcrumb."""

    label: str
    unit_text: str


class ClauseClassificationContext(BaseModel):
    """One verified clause, ready to be classified into a penalty rule."""

    section_title: str | None
    clause_text: str


class RuleFactContext(BaseModel):
    """One classified rule handed to the fact stage; category/calc_type are pipeline decisions, not retrieved text.

    `previous_issues` is set on a repair retry: the prior attempt's `consistency_issues`
    findings, fed back in as corrective context, never as a system-level instruction.
    """

    clause_text: str
    penalty_category: str
    calc_type: str
    previous_issues: list[str] | None = None


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
