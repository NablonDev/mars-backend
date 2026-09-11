"""Regression test for scripts/seed/seed_agents.py's D3 interpolation-boundary
guard (see the approved Phase 1 plan's D3 and the module docstring).

The guard used to be a bare `assert`, silently stripped under `python -O`,
which would let a stray `{`-carrying prompt reach `process.agent.system_prompt`
-- a system-level instruction store, not somewhere interpolation-placeholder
text belongs. It now raises `ValueError` unconditionally instead.
"""

import pytest

from scripts.seed import seed_agents


def test_cmir_extractor_system_prompt_has_no_interpolation_placeholder():
    """The seeded, static portion of the CMIR extractor's prompt must not
    contain the trailing `Email:\\n\\n{body}` interpolation slot -- that
    slot carries user-controlled email content and must remain a per-call
    user-message interpolation, never stored system-prompt content."""
    assert "{" not in seed_agents.CMIR_EXTRACTOR_SYSTEM_PROMPT


def test_agent_seeds_cover_all_eight_rows():
    """Guards against a silent drop of one of the eight documented seed rows."""
    keys = {(seed.agent_code, seed.prompt_version) for seed in seed_agents.AGENT_SEEDS}
    assert keys == {
        ("penalty_projection_summary", "v1"),
        ("penalty_mitigation_summary", "v1"),
        ("penalty_rule_extractor", "v1"),
        ("penalty_rule_screening", "v1"),
        ("penalty_rule_classification", "v1"),
        ("penalty_rule_fact_extraction", "v1"),
        ("cmir_extractor", "v1"),
        ("po_validation", "v1"),
    }


@pytest.mark.parametrize(
    "agent_code",
    [
        "penalty_rule_extractor",
        "penalty_rule_screening",
        "penalty_rule_classification",
        "penalty_rule_fact_extraction",
    ],
)
def test_rule_extraction_seed_has_penalties_domain_and_a_nonempty_system_prompt(agent_code):
    """Each rule-extraction seed row must exist with a real prompt: `penalty_rule_extractor`
    for `process.agent_run.agent_id`'s FK, the other three for
    `app.agents.penalties.rule_extraction.adapter._active_prompt`'s per-stage lookup. Neither
    is registered on demand the way `PenaltyRuleExtractionService._ensure_registered` used
    to be the only site for `penalty_rule_extractor`."""
    seed = next(s for s in seed_agents.AGENT_SEEDS if s.agent_code == agent_code)
    assert seed.domain == "penalties"
    assert seed.system_prompt.strip() != ""


def test_exactly_one_active_row_per_agent_code():
    """`uq_agent_one_active_per_code` requires at most one is_active=True row
    per agent_code -- guard against a version bump leaving two seeds active
    (or none) for the same agent_code."""
    active_codes = [seed.agent_code for seed in seed_agents.AGENT_SEEDS if seed.is_active]
    assert sorted(active_codes) == sorted(set(active_codes))
    all_codes = {seed.agent_code for seed in seed_agents.AGENT_SEEDS}
    assert set(active_codes) == all_codes


def test_all_agent_seeds_use_a_valid_domain():
    """`process.agent.domain` is restricted by `ck_agent_domain` to
    ('cmir', 'penalties') -- a seed row with any other value would fail to
    apply against a real Postgres database."""
    assert {seed.domain for seed in seed_agents.AGENT_SEEDS} <= {"cmir", "penalties"}
