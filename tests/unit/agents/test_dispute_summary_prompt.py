"""Golden test for the dispute-summary system prompt
(`app.agents.penalties.dispute.prompts.v2.SYSTEM_PROMPT`): the LLM must be
explicitly forbidden from stating or implying a verdict other than the one
the deterministic engine already persisted -- the verdict/computed_amount/
claimed_amount/delta_amount are narrated, never second-guessed. See
`app.services.penalties.dispute.engine`'s module docstring ("LLM never
decides pay/no-pay/how-much") and `DisputeSummaryService`'s own docstring.
"""

from app.agents.penalties.dispute.prompts.v2 import SYSTEM_PROMPT


def test_prompt_forbids_stating_a_different_verdict():
    lowered = SYSTEM_PROMPT.lower()
    assert "never decide, state, or imply a different verdict" in lowered


def test_prompt_names_the_persisted_fields_as_final():
    lowered = SYSTEM_PROMPT.lower()
    assert "final, already persisted, and not" in lowered
    assert "verdict" in lowered
    assert "computed_amount" in lowered
    assert "claimed_amount" in lowered
    assert "delta_amount" in lowered


def test_prompt_defines_all_three_verdicts_without_recompute_instruction():
    lowered = SYSTEM_PROMPT.lower()
    for verdict in ("no_pay", "pay_partial", "pay_full"):
        assert verdict in lowered
    assert "never recompute" in lowered
