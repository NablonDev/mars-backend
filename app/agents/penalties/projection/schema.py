"""Schema for the output of the penalty projection summary generation."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.agents.penalties._summary_output import PenaltySummaryOutputBase


class PenaltyProjectionSummaryOutput(PenaltySummaryOutputBase):
    """Output of projection-summary generation; no fields beyond the shared base."""


class ReasoningStepOutput(BaseModel):
    """One of the structured reasoning steps synthesized by the LLM agent."""

    step_number: int
    title: str
    content: str


class TimelineProjectionSummaryOutput(PenaltyProjectionSummaryOutput):
    """Output of event-driven fulfillment timeline penalty & mitigation LLM reasoning (v3)."""

    reasoning_steps: list[ReasoningStepOutput] = Field(default_factory=list)


def parse_reasoning_steps_from_markdown(text: str) -> list[ReasoningStepOutput]:
    """Parse a 4-step markdown response into structured `ReasoningStepOutput` objects.

    Matches headings like:
    - `### Step 1 — Fine Projection`
    - `### Step 2: Penalty Rule Extraction`
    - `### 1. Fine Projection`
    """
    if not text or not text.strip():
        return []

    # Regex matching step headings: ### Step 1 — Title or ### Step 1: Title or ## Step 1
    pattern = re.compile(
        r"^#{1,4}\s*(?:Step\s*)?(\d+)[\s.:—\-]+(.*?)$",
        re.MULTILINE,
    )

    matches = list(pattern.finditer(text))
    if not matches:
        return []

    steps: list[ReasoningStepOutput] = []
    for i, match in enumerate(matches):
        step_num = int(match.group(1))
        title = match.group(2).strip()
        start_idx = match.end()
        end_idx = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start_idx:end_idx].strip()
        steps.append(ReasoningStepOutput(step_number=step_num, title=title, content=content))

    return steps
