"""Pins the fact-extraction prompt's stated `branch_no` convention to the value
`PenaltyRulePublisher` actually looks up for a non-tiered (single-branch) rule.

`prompts/v1.py` and `publisher.py` are edited independently (different agents,
different files), so nothing else re-checks that they still agree. Before this fix the
prompt told the model a single-branch rule uses `branch_no = 1` and called `0` a
modeling error, while the publisher and the schema default both treat `branch_no = 0`
as the single, rule-wide branch. This test would fail again if either side drifts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.agents.penalties.rule_extraction.prompts.v1 import (
    PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT,
    PROMPT_VERSION,
)
from app.services.penalties.rule_extraction.publisher import PenaltyRulePublisher
from app.services.penalties.rule_extraction.types import PublishedRule, StagedFact, StagedRule


def test_prompt_version_is_still_v1():
    assert PROMPT_VERSION == "v1"


def test_fact_extraction_prompt_explains_what_previous_issues_means():
    # `RuleFactContext.previous_issues` is threaded into a repair retry's data payload;
    # nothing told the model what that field means or that it should act on it, so the
    # extra retry calls were likely inert at temperature=0.0.
    assert "previous_issues" in PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT
    assert "prior extraction attempt" in PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT


def test_prompt_teaches_branch_no_zero_as_the_single_branch_convention():
    assert "`branch_no = 0` is rule-wide" in PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT
    # The old, contradictory instruction must be gone, not just superseded.
    assert "still uses `branch_no = 1`, never 0" not in PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT
    assert "branch_no = 0` with nothing at 1" not in PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT


def test_publisher_prices_a_single_branch_rule_from_branch_no_zero_exactly_as_the_prompt_teaches():
    """The value this test pins (`branch_no=0`) must match whatever `PenaltyRulePublisher`
    actually looks up for a non-tiered rule; if the publisher's lookup branch ever
    changes, this fails alongside the prompt-text assertions above."""
    staged = StagedRule(
        id="rule-1",
        retailer_agreement_id="contract-1",
        clause_fingerprint="a" * 32,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        po_shortage_flag=True,
        po_delay_flag=False,
        pricing_readiness="READY",
        status="APPROVED",
        facts=[
            StagedFact(
                branch_no=0,
                attribute_role="RATE",
                basis_type="UNIT_COST",
                value=Decimal(50),
                value_status="PRESENT",
            )
        ],
    )

    result = PenaltyRulePublisher().publish(staged, "WMT", date(2026, 1, 1))

    assert isinstance(result, PublishedRule)
    assert result.rate == Decimal(50)
