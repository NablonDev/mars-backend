"""Unit tests for the event-driven fulfillment timeline penalty mitigation prompt (v3)."""

from app.agents.penalties.mitigation.prompts.v3 import (
    PROMPT_VERSION as MITIGATION_V3_PROMPT_VERSION,
)
from app.agents.penalties.mitigation.prompts.v3 import (
    SYSTEM_PROMPT as MITIGATION_V3_SYSTEM_PROMPT,
)


def test_mitigation_v3_prompt_metadata_and_invariants():
    """Verify v3 mitigation prompt version and deterministic instructions."""
    assert MITIGATION_V3_PROMPT_VERSION == "v3"
    assert "Data Trust Rules" in MITIGATION_V3_SYSTEM_PROMPT
    assert "Never invent numbers, costs, dates, or options" in MITIGATION_V3_SYSTEM_PROMPT
    assert "no probabilistic risk-adjustment" in MITIGATION_V3_SYSTEM_PROMPT
    assert "Optimal Decision & Financial Impact" in MITIGATION_V3_SYSTEM_PROMPT
    assert "Action Breakdown & Trade-Offs" in MITIGATION_V3_SYSTEM_PROMPT
